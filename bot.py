"""Survivor: a mechfight/1 bot that plays for placement.

Priorities, in order: stay in the zone, never show its back, fight with lead-aimed fire while strafing,
break line of sight and regenerate when losing, and farm cubes/weapons/drops when nobody is around.
"""
import gc
import math
import random
import time

import mechfight as mf

try:
    import nav
except Exception:  # numpy missing: run without a map
    nav = None

TAU = 2 * math.pi
DEBUG = bool(__import__('os').environ.get('BOTDEBUG'))

WEAPON_VALUE = {"lightning": 3.0, "laser": 2.7, "mortar": 1.6, "gun": 0.0}
UPGRADE_ORDER = [
    "plating", "nanites", "armor", "gun_damage", "gun_rate", "lightning_efficiency", "laser_charge",
    "laser_damage", "servos", "gyros", "gun_mag", "lightning_range", "sensors", "gun_velocity",
    "lightning_chain", "mortar_reload", "reactor", "capacitor", "jets", "mortar_velocity", "mortar_splash",
    "laser_pierce",
]


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def norm2(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-6 else (0.0, 0.0)


class Bot:
    name = "survivor"

    def __init__(self, hello):
        self.cfg = cfg = hello["config"]
        self.rng = random.Random(hello.get("seed", 1))
        self.mech = cfg["mech"]
        self.W = cfg["weapons"]
        self.cube_growth = cfg["cubes"]["size_per_cube"]
        self.sim_step = 1 / cfg["match"]["tick_hz"]
        self.decision_span = self.sim_step * cfg["match"]["decide_every"]
        self.search_budget = min(0.012, cfg["bot"]["tick_budget_ms"] * 0.00024)
        self.reach = cfg["loot"]["pickup_radius"]
        self.nav = None
        if nav is not None:
            try:
                self.nav = nav.NavMap(hello["map"]["dir"], self.mech)
            except Exception as e:  # keep playing without the map
                mf.log("map load failed:", repr(e))
        self.t = 0.0
        self.attackers = {}        # id -> last time it damaged us
        self.threats = []          # (bearing, time, weight)
        self.last_dmg_t = -99.0
        self.last_hit_t = -99.0
        self.strafe = 1
        self.strafe_until = 0.0
        self.path = None
        self.path_goal = None
        self.path_t = -99.0
        self.hist = []             # (t, x, y)
        self.stuck_until = 0.0
        self.stuck_n = 0
        self.escape = None
        self.escape_until = 0.0
        self.jet_until = 0.0
        self.bad_pickups = {}
        self.last_seen = {}        # id -> (pos, time)
        self.cover = None
        self.cover_t = -99.0
        self.mode = "start"
        self.prev_hp = None
        self.scan_phase = self.rng.random() * TAU
        self.heavy_used = False
        self.last_heavy_t = -99.0
        gc.collect()
        gc.freeze()

    # ================================================================== main
    def act(self, obs):
        t0 = time.perf_counter()
        self.t0 = t0
        self.heavy_used = False
        self.mortar_sol = None
        self.gun_solution = None
        self.direct_aim = None
        self.want_jump = False
        self.farm_kind = None
        t = self.t = obs["t"]
        me = self.me = obs["self"]
        x, y, z = self.pos = me["pos"]
        self.hp_frac = me["hp"] / max(1.0, me["max_hp"])
        st = me["stats"]
        self.muzzle = st["muzzle_height"]
        act = {"tick": obs["tick"]}
        self._events(obs)
        if obs.get("offer"):
            act["pick"] = self._pick_upgrade(obs["offer"])

        self.hist.append((t, x, y))
        while self.hist and self.hist[0][0] < t - 1.5:
            self.hist.pop(0)

        enemies = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for e in enemies:
            e["d"] = math.dist(e["pos"][:2], (x, y))
            self.last_seen[e["id"]] = (e["pos"], t)
        enemies.sort(key=lambda e: e["d"])

        zone_goal, zone_urgent = self._zone_goal(obs)
        self._measure_threats(enemies)
        target = self._choose_target(enemies)
        self.target_los = target is not None and target["clear"]
        fight = target is not None and self._should_fight(target, obs)

        move = (0.0, 0.0)
        boost = False
        label = ""
        aim_point = None   # world point to aim at (non-combat)

        # ---------------- movement decision
        retreat = target is not None and self._should_retreat(target, enemies)
        final_move = self._final_approach(obs["zone"], fight)
        if final_move is not None:
            move = final_move
            label = "final center"
        elif zone_urgent:
            move = self._navigate(zone_goal)
            boost = not me["in_zone"] or self._outside_soon(obs)
            label = "zone"
        elif (supply := self._supply_move(obs, enemies)) is not None:
            move, label = supply
        elif retreat and not (target["d"] < 38 or t - self.last_dmg_t < 0.6 and self.target_los):
            move, label = self._retreat(enemies, zone_goal, obs)
            boost = me["energy"] > 30 and t - self.last_dmg_t < 1.5
        elif fight and self._outnumbered(enemies):
            move, label = self._retreat(enemies, zone_goal, obs, fight_target=target)
            boost = me["energy"] > 45 and t - self.last_dmg_t < 1.0
        elif fight:
            move = self._fight_move(target, enemies, zone_goal, obs)
            boost = me["energy"] > 45 and t - self.last_dmg_t < 1.0
            label = f"fight {target['id']}"
        else:
            move, aim_point, label = self._peaceful(obs, npcs, zone_goal)

        if label.startswith("fight "):
            loot = self._combat_loot(obs, enemies)
            if loot is not None:
                move, label = loot

        if final_move is None:
            if self.escape is not None and t < self.escape_until:
                move = self.escape
            move = self._dodge(obs, move)
            move = self._keep_in_zone(obs, move)

        # ---------------- aim + fire
        fire = [False, False]
        aim = None
        if fight or (target is not None and label in ("cover", "to cover", "kite")):
            aim = self._lead_aim(target)
            if fight:
                fire = self._fire_control(target, aim, enemies, obs)
        if aim is None and aim_point is not None:
            aim = self._aim_at_point(aim_point)
            fire = self._fire_at_object(aim, aim_point, obs)
        if aim is None:
            aim = self._idle_aim(obs, move)
        act["aim"] = [wrap(aim[0]), max(st["min_pitch"], min(st["max_pitch"], aim[1]))]

        # laser charge needs fire held across ticks; lightning/gun fire are one-shot holds
        act["fire"] = fire
        self._reload(act, target)

        # ---------------- stuck / climbing
        jump, jet = self._unstick(move)
        if jet:
            boost = True
        if jump or (self.want_jump and me["grounded"]):
            act["jump"] = True
        if boost and me["grounded"] and (me["energy"] < 12 or (math.hypot(*me["vel"][:2]) < 3 and self.hist and t - self.hist[0][0] > 0.5)):
            boost = False

        # ---------------- loot
        tk = self._take(obs)
        if tk:
            act.update(tk)

        mvx, mvy = move
        precision = 7 if final_move is not None else 3
        act["move"] = [round(mvx, precision), round(mvy, precision)]
        act["boost"] = bool(boost)
        if DEBUG and obs["tick"] % 10 == 0:
            g = self._weapons().get("gun")
            mf.log(f"t={t:.1f} {label:12s} hp={me['hp']:.0f} en={me['energy']:.0f} ammo={g[1].get('ammo') if g else '-'} "
                   f"tgt={target['id'] if target else '-'}@{target['d'] if target else 0:.0f} los={self.target_los} "
                   f"n={len(enemies)} fire={act['fire']} aimerr={self._aim_err(aim)[0]:.3f} mv={act.get('move')}")
        el = (time.perf_counter() - t0) * 1000
        if el > 8:
            mf.log(f"slow tick t={t:.1f} {el:.1f}ms mode={label} heavy={self.heavy_used}")
        mf.draw.label(f"{label} hp{int(me['hp'])} {int(el)}ms")
        self.prev_hp = me["hp"]
        return act

    # ================================================================== bookkeeping
    def _events(self, obs):
        t = self.t
        for ev in obs.get("events") or []:
            et = ev.get("type")
            if et == "damaged":
                if ev.get("weapon") != "zone":
                    self.last_dmg_t = t
                    if ev.get("by") is not None:
                        self.attackers[ev["by"]] = t
                    if ev.get("bearing") is not None:
                        self.threats.append((ev["bearing"], t, 3.0 if ev.get("rear") else 2.0))
            elif et == "hit":
                self.last_hit_t = t
            elif et == "take_failed":
                self.bad_pickups[ev.get("id")] = t
            elif et == "took":
                self.bad_pickups.pop(ev.get("id"), None)
        for s in obs.get("sounds") or []:
            k = s.get("kind")
            if k in ("gun", "laser", "laser_charge", "lightning", "mortar", "step", "boost", "jet", "reload"):
                w = {"near": 1.5, "mid": 1.0, "far": 0.4}.get(s.get("dist"), 0.5)
                if k in ("step", "boost", "jet", "reload"):
                    w *= 1.3  # always close
                self.threats.append((s["bearing"], t - s.get("age", 0), w))
        self.threats = [th for th in self.threats if t - th[1] < 4.0][-30:]

    def _pick_upgrade(self, offer):
        ids = [c["id"] for c in offer["choices"]]
        best, bi = 1e9, 0
        for i, u in enumerate(ids):
            r = UPGRADE_ORDER.index(u) if u in UPGRADE_ORDER else 50
            if r < best:
                best, bi = r, i
        return bi

    def _weapons(self):
        out = {}
        for i, w in enumerate(self.me["weapons"]):
            if w:
                out[w["kind"]] = (i, w)
        return out

    # ================================================================== zone
    def _zone_circle_at(self, z, t):
        if t <= z["shrink_start"]:
            return z["center"], z["radius"]
        if t >= z["shrink_end"]:
            return z["next_center"], z["next_radius"]
        f = (t - z["shrink_start"]) / max(1e-6, z["shrink_end"] - z["shrink_start"])
        c = z["center"]
        n = z["next_center"]
        return [c[0] + (n[0] - c[0]) * f, c[1] + (n[1] - c[1]) * f], z["radius"] + (z["next_radius"] - z["radius"]) * f

    def _zone_goal(self, obs):
        """Point to head for and whether it overrides everything else."""
        z = obs["zone"]
        x, y = self.pos[0], self.pos[1]
        t = self.t
        nc, nr = z["next_center"], z["next_radius"]
        speed = self.me["stats"]["walk_speed"]
        margin = max(6.0, min(30.0, nr * 0.25))
        dn = math.dist((x, y), nc)
        target_r = max(0.0, nr - margin)
        if dn > target_r:
            ux, uy = norm2(x - nc[0], y - nc[1])
            goal = (nc[0] + ux * target_r, nc[1] + uy * target_r)
        else:
            goal = (x, y)
        if nr < 1:
            goal = tuple(nc)
        # urgent if we would not make it into the next circle in time, or we're out / about to be out
        need = max(0.0, dn - target_r) / speed
        time_left = z["shrink_end"] - t
        cc, cr = self._zone_circle_at(z, t + 4.0)
        dcur = math.dist((x, y), cc)
        urgent = (not self.me["in_zone"]) or dcur > cr - 10
        if dn > nr and z["shrink_start"] - t < need * 1.5 + 10:
            urgent = True  # rotate before the shrink starts
        del time_left
        if nr < 30 and t > z["shrink_start"] - 5 and dn > max(4.0, nr * 0.5):
            urgent = True
        if self.me["in_zone"] and math.dist((x, y), goal) < 3.0:
            urgent = False
        self.zone_goal = goal
        return goal, urgent

    def _final_approach(self, zone, fighting):
        if zone["next_radius"] > 0 or self.t < zone["shrink_start"] - 4:
            return None
        x, y, z = self.pos
        gx, gy = zone["next_center"]
        stats = self.me["stats"]
        speed = stats["walk_speed"]
        distance = math.hypot(gx - x, gy - y)
        if fighting and zone["radius"] > speed:
            return None
        if distance > speed * 0.85:
            return None
        if self.nav and distance > 1:
            clearance = stats["step_up"] + 0.25
            if not self.nav.los((x, y, z + clearance), (gx, gy, z + clearance)):
                return None
        # Near the point, request only the speed needed to reach it during the next decision.
        # Slowing early gives the legs time to brake while the ring is still wide.
        acceleration = stats["ground_accel"] if self.me["grounded"] else stats["air_accel"]
        pace = min(speed, math.sqrt(acceleration * distance), distance / self.decision_span)
        ux, uy = norm2(gx - x, gy - y)
        return ux * pace / speed, uy * pace / speed

    def _outside_soon(self, obs):
        z = obs["zone"]
        cc, cr = self._zone_circle_at(z, self.t + 4.0)
        return math.dist(self.pos[:2], cc) > cr - 5

    def _in_zone_later(self, obs, p, dt=6.0, margin=6.0):
        cc, cr = self._zone_circle_at(obs["zone"], self.t + dt)
        return math.dist(p, cc) < cr - margin

    def _keep_in_zone(self, obs, move):
        """Never let a move take us across the closing edge."""
        x, y = self.pos[0], self.pos[1]
        cc, cr = self._zone_circle_at(obs["zone"], self.t + 2.0)
        d = math.dist((x, y), cc)
        if d < cr - 15:
            return move
        ix, iy = norm2(cc[0] - x, cc[1] - y)
        mx, my = move
        outward = -(mx * ix + my * iy)
        if outward > 0:
            mx += ix * outward * 1.2
            my += iy * outward * 1.2
        if d > cr - 6:
            mx += ix * 0.8
            my += iy * 0.8
        return self._clamp(mx, my)

    @staticmethod
    def _clamp(mx, my):
        d = math.hypot(mx, my)
        if d > 1:
            return mx / d, my / d
        return mx, my

    # ================================================================== targets
    def _measure_threats(self, enemies):
        if not enemies:
            return
        x, y, z = self.pos
        source = [(x, y, z + self.muzzle)] * len(enemies)
        target = [(ex, ey, ez + self.mech["height"] * self._size(enemy) / 2)
                  for enemy in enemies for ex, ey, ez in (enemy["pos"],)]
        exposed = self.nav.los_many(source, target) if self.nav else [True] * len(enemies)
        since_attack = self.me["stats"]["regen_delay"] * 1.5
        for enemy, clear in zip(enemies, exposed):
            ex, ey, _ = enemy["pos"]
            distance = max(1, enemy["d"])
            dx, dy = x - ex, y - ey
            facing_error = abs(wrap(math.atan2(dy, dx) - enemy["aim"][0]))
            evx, evy, _ = enemy["vel"]
            closing_speed = (evx * dx + evy * dy) / distance
            close = distance < self.cfg["vision"]["near_range"]
            aiming_here = facing_error < math.atan2(self.mech["radius"] * 3, distance) + 0.12
            moving_here = close and facing_error < 0.75 and closing_speed > self.mech["walk_speed"] * 0.3
            enemy["clear"] = bool(clear)
            enemy["aggressor"] = (self.t - self.attackers.get(enemy["id"], -math.inf) < since_attack
                                  or (clear and (aiming_here or moving_here)))
            enemy["rear_angle"] = facing_error

    def _choose_target(self, enemies):
        if not enemies:
            return None
        reach = max((w["stats"].get("range", 0) for w in self.me["weapons"] if w), default=0)
        best, best_score = None, math.inf
        clear_threat, threat_score = None, math.inf
        for enemy in enemies:
            if enemy["d"] > self.W["laser"]["range"]:
                continue
            score = enemy["d"] / 90 + 1.6 * self._enemy_health(enemy)
            score += 0.7 if not enemy["clear"] else 0.0
            score -= 1.1 if enemy["aggressor"] else 0.0
            score -= 0.25 if enemy["rear_angle"] > math.radians(self.mech["front_arc_deg"]) / 2 else 0.0
            if score < best_score:
                best, best_score = enemy, score
            if enemy["clear"] and enemy["aggressor"] and enemy["d"] <= reach and score < threat_score:
                clear_threat, threat_score = enemy, score
        if best is not None and not best["clear"] and clear_threat is not None:
            return clear_threat
        return best

    def _should_fight(self, target, obs):
        if target["aggressor"]:
            return True
        if not self.target_los:
            return False
        weapons = self._weapons()
        gun_range = self.W["gun"]["range"]
        if "gun" in weapons:
            gun_range = weapons["gun"][1]["stats"]["range"]
        health = self._enemy_health(target)
        if health < 0.43 and target["d"] < gun_range * 0.85:
            return True
        if obs["alive"] <= 3 and target["d"] < gun_range * 0.65:
            return self.hp_frac > 0.5 or health < self.hp_frac
        early = self.t < self.cfg["zone"]["phases"][0]["hold_s"]
        advantage = self.hp_frac > 0.78 and (len(weapons) > len(target.get("weapons", ())) or health < 0.65)
        return not early and advantage and target["d"] < gun_range * 0.5

    def _should_retreat(self, tgt, enemies):
        my = self.me["hp"]
        th = tgt.get("hp", self.mech["hp"])
        close = [e for e in enemies if e["d"] < 120]
        if self.hp_frac < 0.3 and th > my * 0.8:
            return True
        if self.hp_frac < 0.55 and len(close) >= 2:
            return True
        if len(close) >= 3 and self.hp_frac < 0.85:
            return True
        return False

    def _outnumbered(self, enemies):
        return sum(e["d"] < 170 and (e["clear"] or e["aggressor"]) for e in enemies) >= 2

    def _enemy_health(self, actor):
        maximum = actor.get("max_hp", self.mech["hp"])
        return actor.get("hp", maximum) / max(1, maximum)

    def _size(self, actor):
        return 1 + self.cube_growth * actor.get("cubes", 0)

    def _has_los(self, e):
        if self.nav is None:
            return True
        x, y, z = self.pos
        ex, ey, ez = e["pos"]
        center = ez + self.mech["height"] * self._size(e) / 2
        return self.nav.los((x, y, z + self.muzzle), (ex, ey, center))

    # ================================================================== aiming
    def _lead_aim(self, e):
        weapons = self._weapons()
        _, gun = weapons.get("gun", (None, None))
        _, laser = weapons.get("laser", (None, None))
        _, mortar = weapons.get("mortar", (None, None))
        if not self.target_los and mortar:
            return self._mortar_aim(e, mortar)

        self.direct_aim, _, _ = self._intercept(e, self.sim_step)
        self.gun_solution = None
        if gun and gun.get("ammo", 0) and not gun.get("reload", 0):
            delay = self._launch_delay(gun)
            self.gun_solution = self._intercept(e, delay, gun["stats"]["speed"])
        if laser and laser.get("charge", 0) >= laser["stats"]["charge"] - self.decision_span:
            return self.direct_aim
        return self.gun_solution[0] if self.gun_solution else self.direct_aim

    def _launch_delay(self, weapon):
        tick = math.ceil((weapon["cooldown"] - 1e-8) / self.sim_step)
        return max(1, tick) * self.sim_step

    def _intercept(self, enemy, delay, speed=None):
        x, y, z = self.pos
        vx, vy, vz = self.me["vel"]
        origin = (x + vx * delay, y + vy * delay, z + self.muzzle + vz * delay)
        ex, ey, ez = enemy["pos"]
        evx, evy, evz = enemy["vel"]
        scale = self._size(enemy)
        ez += self.mech["height"] * scale / 2
        airborne = abs(evz) > 0.05
        gravity = self.mech["gravity"] if airborne else 0
        muzzle_offset = self.me["stats"]["radius"] + 0.5
        travel = 0.0
        for _ in range(3 if speed else 1):
            # A shot starts past the barrel and crosses part of its path on the launch step.
            time_to_hit = delay + max(0, travel - self.sim_step * 0.5)
            point = (ex + evx * time_to_hit, ey + evy * time_to_hit,
                     ez + evz * time_to_hit - gravity * time_to_hit ** 2 * 0.5)
            if speed:
                travel = max(0.0, math.dist(origin, point) - muzzle_offset) / speed
        dx, dy, dz = (b - a for a, b in zip(origin, point))
        return (math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy))), origin, point

    def _aim_at_launch(self, command, delay):
        stats = self.me["stats"]
        yaw, pitch = self.me["aim"]
        dyaw = wrap(command[0] - yaw)
        dpitch = max(stats["min_pitch"], min(stats["max_pitch"], command[1])) - pitch
        turn = stats["turn_rate"] * delay
        laser = self._weapons().get("laser")
        if laser and laser[1].get("charge", 0):
            turn *= laser[1]["stats"]["charge_turn_mult"]
        tilt = stats["pitch_rate"] * delay
        return yaw + max(-turn, min(turn, dyaw)), pitch + max(-tilt, min(tilt, dpitch))

    def _mortar_aim(self, e, w):
        x, y, z = self.pos
        mz = z + self.me["stats"]["height"] + 0.5
        ex, ey, ez = e["pos"]
        vx, vy, _ = e.get("vel", (0, 0, 0))
        v = w["stats"]["speed"]
        g = w["stats"]["gravity"]
        tt = 0.0
        sol = None
        for _ in range(3):
            px, py = ex + vx * tt, ey + vy * tt
            d = math.hypot(px - x, py - y)
            dz = ez + 1.0 - mz
            disc = v ** 4 - g * (g * d * d + 2 * dz * v * v)
            if disc < 0 or d < 1:
                break
            th = math.atan((v * v + math.sqrt(disc)) / (g * d))  # high arc: clears buildings
            if th > self.me["stats"]["max_pitch"]:
                th = math.atan((v * v - math.sqrt(disc)) / (g * d))
            sol = (math.atan2(py - y, px - x), th)
            tt = d / max(1e-3, v * math.cos(th))
        self.mortar_sol = sol
        if sol is None:
            return (math.atan2(ey - y, ex - x), 0.0)
        return sol

    def _aim_at_point(self, p):
        x, y, z = self.pos
        mz = z + self.muzzle
        return (math.atan2(p[1] - y, p[0] - x), math.atan2(p[2] - mz, max(0.1, math.hypot(p[0] - x, p[1] - y))))

    def _idle_aim(self, obs, move):
        t = self.t
        # most recent weighted threat bearing
        if self.threats:
            sx = sy = 0.0
            for b, tt, w in self.threats:
                k = w * max(0.0, 1.0 - (t - tt) / 4.0)
                sx += math.cos(b) * k
                sy += math.sin(b) * k
            if math.hypot(sx, sy) > 0.3:
                return (math.atan2(sy, sx), 0.0)
        # sweep around the direction of travel (or the zone centre when standing still)
        if abs(move[0]) + abs(move[1]) > 0.2:
            base = math.atan2(move[1], move[0])
        else:
            zc = obs["zone"]["next_center"]
            base = math.atan2(zc[1] - self.pos[1], zc[0] - self.pos[0]) + math.pi
        sweep = math.sin(t * 0.9 + self.scan_phase) * 1.3
        return (base + sweep, 0.0)

    # ================================================================== firing
    def _aim_err(self, desired):
        ay, ap = self.me["aim"]
        return abs(wrap(desired[0] - ay)), abs(desired[1] - ap)

    def _fire_control(self, e, aim, enemies, obs):
        fire = [False, False]
        me = self.me
        d3 = math.dist(self.pos, e["pos"])
        sc = self._size(e)
        rad = self.mech["radius"] * sc
        eyaw, epitch = self._aim_err(aim)
        tol = math.atan2(rad * 0.8, max(1.0, d3))
        ptol = math.atan2(self.mech["height"] * sc * 0.42, max(1.0, d3))
        los = self.target_los
        for i, w in enumerate(me["weapons"]):
            if not w:
                continue
            k = w["kind"]
            s = w["stats"]
            if k == "gun":
                delay = self._launch_delay(w)
                if not los or self.gun_solution is None or delay > self.decision_span or d3 >= s["range"]:
                    continue
                shot, origin, point = self.gun_solution
                yaw, pitch = self._aim_at_launch(aim, delay)
                if abs(wrap(yaw - shot[0])) > tol or abs(pitch - shot[1]) > ptol:
                    continue
                fire[i] = self.nav is None or self.nav.los(origin, point)
            elif k == "lightning":
                rng = s["range"]
                cone = math.radians(s["cone_deg"]) / 2
                cost = s["energy"]
                if me["energy"] > cost + 8:
                    for o in enemies:
                        if o["d"] < rng * 0.95:
                            b = math.atan2(o["pos"][1] - self.pos[1], o["pos"][0] - self.pos[0])
                            if abs(wrap(b - me["aim"][0])) < cone * 0.85 and (o is not e or los):
                                fire[i] = True
                                break
            elif k == "laser":
                charge = w.get("charge", 0) or 0
                full = s["charge"]
                shorted = any(o["d"] < self.W["lightning"]["range"] * 1.1 and "lightning" in o.get("weapons", ())
                              for o in enemies)
                if charge > 0:
                    # holding: release when aimed (beam is hitscan) or keep holding
                    ready = charge >= full * 0.97
                    yaw, pitch = self._aim_at_launch(aim, self.sim_step)
                    on = (los and self.direct_aim is not None and d3 <= s["range"] and abs(wrap(yaw - self.direct_aim[0])) < tol
                          and abs(pitch - self.direct_aim[1]) < ptol)
                    fire[i] = not (ready and on) and charge < full + s["hold_s"] - 0.15
                    if not los and charge >= full:
                        fire[i] = True  # keep it charged while target is behind cover
                elif w["cooldown"] <= self.sim_step and los and d3 < s["range"] * 0.95 and not shorted \
                        and eyaw < 0.5:
                    fire[i] = True
            elif k == "mortar":
                sol = getattr(self, "mortar_sol", None)
                if w.get("cooldown", 0) <= 0 and d3 > 3 * s["splash_radius"] and sol is not None and not los and \
                        eyaw < 0.03 and epitch < 0.03 and self._mortar_path_ok(sol, s):
                    fire[i] = True
                elif w.get("cooldown", 0) <= 0 and los and 25 < d3 < 140 and "gun" not in self._weapons():
                    fire[i] = eyaw < tol and epitch < ptol
        return fire

    def _mortar_path_ok(self, sol, stats):
        if self.nav is None:
            return True
        x, y, z = self.pos
        mz = z + self.me["stats"]["height"] + 0.5
        v = stats["speed"]
        g = stats["gravity"]
        yaw, th = sol
        pts = []
        for k in range(1, 7):
            tt = k * 0.25
            h = v * math.cos(th) * tt
            pts.append((x + math.cos(yaw) * h, y + math.sin(yaw) * h, mz + v * math.sin(th) * tt - 0.5 * g * tt * tt))
        A = [(x, y, mz)] + pts[:-1]
        return bool(self.nav.los_many(A, pts).all())

    def _fire_at_object(self, aim, p, obs):
        """Shoot crates/NPCs we chose to farm."""
        if getattr(self, "farm_kind", None) is None:
            return [False, False]
        fire = [False, False]
        d = math.dist(self.pos, p)
        eyaw, epitch = self._aim_err(aim)
        rad = self.cfg["crates" if self.farm_kind == "crate" else "npc"]["radius"]
        tol = math.atan2(rad * 0.8, max(1.0, d)) + 0.004
        x, y, z = self.pos
        if self.nav is not None and not self.nav.los((x, y, z + self.muzzle), p):
            return fire
        for i, w in enumerate(self.me["weapons"]):
            if not w:
                continue
            if w["kind"] == "gun" and w.get("ammo", 1) > 0 and w.get("reload", 0) == 0 and eyaw < tol and epitch < tol * 2 + 0.01 and d < w["stats"]["range"] * 0.87:
                fire[i] = True
            if w["kind"] == "lightning" and d < w["stats"]["range"] * 0.9 and eyaw < 0.2 and self.me["energy"] > 40:
                fire[i] = True
        return fire

    def _reload(self, act, target):
        wk = self._weapons()
        if "gun" not in wk:
            return
        i, w = wk["gun"]
        if w.get("reload", 0) > 0:
            return
        ammo = w.get("ammo", 0)
        mag = w["stats"]["mag"]
        if ammo == 0 or (ammo < mag * 0.5 and (target is None or not self.target_los) and self.t - self.last_dmg_t > 1.5):
            act["reload"] = True
            act["fire"][i] = False

    # ================================================================== movement
    def _navigate(self, goal):
        x, y = self.pos[0], self.pos[1]
        gx, gy = goal
        d = math.hypot(gx - x, gy - y)
        if d < 1.5:
            return (0.0, 0.0)
        n = self.nav
        if n is None:
            return norm2(gx - x, gy - y)
        if d < 160 and n.walk_clear((x, y), (gx, gy)):
            self.path = None
            return norm2(gx - x, gy - y)
        need = (self.path is None or self.path_goal is None or math.dist(self.path_goal, goal) > 15
                or self.t - self.path_t > 4.0) and self.t - self.path_t > 0.8
        if need and self._heavy_ok():
            self.path = n.astar((x, y), (gx, gy), deadline=self.t0 + self.search_budget)
            self.path_goal = goal
            self.path_t = self.t
            self.heavy_used = True
        if self.path:
            while len(self.path) > 1 and math.hypot(self.path[0][0] - x, self.path[0][1] - y) < 4.0:
                self.path.pop(0)
            wx, wy = self.path[0]
            return norm2(wx - x, wy - y)
        return norm2(gx - x, gy - y)

    def _heavy_ok(self):
        return not self.heavy_used and (time.perf_counter() - self.t0) < self.search_budget

    def _steer_free(self, mx, my, probe=7.0):
        """Rotate a desired move away from walls (ground grid)."""
        n = self.nav
        if n is None or (mx == 0 and my == 0):
            return mx, my
        x, y = self.pos[0], self.pos[1]
        if n.is_free(x, y) is False and n.is_free(x, y, tight=True) is False:
            return mx, my  # on a roof or wedged: let the server slide us
        base = math.atan2(my, mx)
        mag = min(1.0, math.hypot(mx, my))
        for off in (0, 0.4, -0.4, 0.8, -0.8, 1.2, -1.2, 1.7, -1.7, 2.3, -2.3):
            a = base + off
            if n.is_free(x + math.cos(a) * probe, y + math.sin(a) * probe, tight=True) and \
                    n.is_free(x + math.cos(a) * probe * 0.5, y + math.sin(a) * probe * 0.5, tight=True):
                return math.cos(a) * mag, math.sin(a) * mag
        return mx, my

    def _fight_move(self, tgt, enemies, zone_goal, obs):
        x, y = self.pos[0], self.pos[1]
        t = self.t
        wk = self._weapons()
        ex, ey = tgt["pos"][0], tgt["pos"][1]
        d = tgt["d"]
        their = tgt.get("weapons") or []
        if "lightning" in wk and self.me["energy"] > 25:
            want = 26.0
        elif "lightning" in their:
            want = 75.0
        elif "laser" in wk and "gun" not in wk:
            want = 110.0
        else:
            want = 60.0
        hpf_t = self._enemy_health(tgt)
        if hpf_t < 0.25 and self.hp_frac > 0.4:
            want *= 0.7
        ux, uy = norm2(ex - x, ey - y)
        radial = max(-1.0, min(1.0, (d - want) / max(15.0, want * 0.5)))
        if t > self.strafe_until:
            if self.rng.random() < 0.55:
                self.strafe = -self.strafe
            self.strafe_until = t + 0.45 + self.rng.random() * 0.9
        lx, ly = -uy * self.strafe, ux * self.strafe
        mx = ux * radial * 0.8 + lx
        my = uy * radial * 0.8 + ly
        # stay near the safe circle while fighting
        if not self._in_zone_later(obs, (x + mx * 10, y + my * 10), 6.0, 12.0):
            gx, gy = norm2(zone_goal[0] - x, zone_goal[1] - y)
            mx += gx
            my += gy
        mx, my = self._clamp(mx, my)
        mx2, my2 = self._steer_free(mx, my)
        if (mx2, my2) != (mx, my) and abs(wrap(math.atan2(my2, mx2) - math.atan2(my, mx))) > 1.0:
            self.strafe = -self.strafe  # wall on that side: strafe the other way
        return mx2, my2

    def _retreat(self, enemies, zone_goal, obs, fight_target=None):
        """Move to the nearby spot the fewest enemies can see (keeping the target in view when fighting)."""
        x, y, z = self.pos
        t = self.t
        n = self.nav
        threats = [e for e in enemies if e["d"] < 200][:5]
        if n is not None and threats and (self.cover is None or t - self.cover_t > 0.7) and self._heavy_ok():
            self.heavy_used = True
            self.cover_t = t
            self.cover = self._find_spot(threats, obs, fight_target)
        if self.cover is not None:
            cx, cy = self.cover
            if math.hypot(cx - x, cy - y) < 2.5:
                if fight_target is not None:
                    # small jukes on the spot
                    if t > self.strafe_until:
                        self.strafe = -self.strafe
                        self.strafe_until = t + 0.4 + self.rng.random() * 0.6
                    ex, ey = fight_target["pos"][0], fight_target["pos"][1]
                    ux, uy = norm2(ex - x, ey - y)
                    return self._steer_free(-uy * self.strafe * 0.5, ux * self.strafe * 0.5), "peek"
                return (0.0, 0.0), "cover"
            return self._navigate((cx, cy)), ("reposition" if fight_target else "to cover")
        # no spot: back away from the threat centroid in a zigzag
        sx = sum(e["pos"][0] for e in threats) / max(1, len(threats))
        sy = sum(e["pos"][1] for e in threats) / max(1, len(threats))
        ax, ay = norm2(x - sx, y - sy)
        gx, gy = norm2(zone_goal[0] - x, zone_goal[1] - y)
        if t > self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < 0.6 else self.strafe
            self.strafe_until = t + 0.35 + self.rng.random() * 0.6
        mx = ax * 0.8 + 0.4 * gx - ay * self.strafe
        my = ay * 0.8 + 0.4 * gy + ax * self.strafe
        return self._steer_free(*self._clamp(mx, my)), "kite"

    def _find_spot(self, threats, obs, fight_target):
        import numpy as np
        n = self.nav
        x, y, z = self.pos
        cands = [(x, y)]
        for r in (7.0, 14.0, 22.0, 32.0):
            for k in range(12):
                a = k * TAU / 12 + r * 0.13
                px, py = x + math.cos(a) * r, y + math.sin(a) * r
                if n.is_free(px, py) and self._in_zone_later(obs, (px, py), 8.0, 8.0):
                    cands.append((px, py))
        A, B = [], []
        for (px, py) in cands:
            gz = n.ground_at(px, py) if (px, py) != (x, y) else z
            for e in threats:
                ex, ey, ez = e["pos"]
                A.append((ex, ey, ez + self.mech["muzzle_height"] * self._size(e)))
                B.append((px, py, gz + self.me["stats"]["height"] * 0.55))
        vis = n.los_many(np.array(A, np.float32), np.array(B, np.float32), step=2.0)
        k = len(threats)
        best, bs = None, 1e9
        for ci, (px, py) in enumerate(cands):
            s = 0.0
            for ei, e in enumerate(threats):
                if vis[ci * k + ei]:
                    w = 1.0 + (0.6 if self.t - self.attackers.get(e["id"], -99) < 3 else 0.0)
                    w *= max(0.3, min(1.0, 140.0 / max(20.0, e["d"])))
                    if e is fight_target:
                        w -= 1.25  # we want this one in view
                    s += w
            d = math.hypot(px - x, py - y)
            s += d * 0.025
            if fight_target is not None:
                fd = math.hypot(px - fight_target["pos"][0], py - fight_target["pos"][1])
                if fd < 18:
                    s += 0.8
            if s < bs:
                best, bs = (px, py), s
        if best == (x, y):
            return best
        return best

    def _dodge(self, obs, move):
        """Step away from incoming mortar shells' landing points and laser lines aimed at us."""
        x, y, z = self.pos
        mx, my = move
        mortar = self.W["mortar"]
        g = mortar["gravity"]
        for p in obs.get("projectiles") or []:
            if p["kind"] != "mortar":
                continue
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            # time to fall to our feet height
            a, b, c = -0.5 * g, vz, pz - z
            disc = b * b - 4 * a * c
            if disc < 0:
                continue
            tt = (-b - math.sqrt(disc)) / (2 * a)
            if tt < 0 or tt > 4:
                continue
            lx, ly = px + vx * tt, py + vy * tt
            dd = math.hypot(lx - x, ly - y)
            if dd < mortar["splash_radius"] + self.me["stats"]["radius"] + 1.5:
                ax, ay = norm2(x - lx, y - ly) if dd > 0.5 else (math.cos(self.t), math.sin(self.t))
                mx += ax * 2.0
                my += ay * 2.0
        for e in obs["mechs"]:
            if e.get("charging") and e["kind"] == "mech":
                ex, ey = e["pos"][0], e["pos"][1]
                b = math.atan2(y - ey, x - ex)
                d = math.hypot(x - ex, y - ey)
                if abs(wrap(b - e["aim"][0])) < math.atan2(6, max(1, d)) + 0.05:
                    ux, uy = norm2(x - ex, y - ey)
                    mx += -uy * self.strafe * 1.5
                    my += ux * self.strafe * 1.5
        return self._clamp(mx, my)

    def _unstick(self, move):
        t = self.t
        me = self.me
        jump = False
        jet = False
        if t < self.jet_until:
            if not me["grounded"] and me["energy"] > 8:
                jet = True
            return jump, jet
        want = math.hypot(*move) > 0.5
        if want and len(self.hist) > 5 and t - self.hist[0][0] > 1.0:
            moved = math.dist(self.hist[0][1:], (self.pos[0], self.pos[1]))
            if moved < 2.0:
                self.path = None  # replan next tick
                self.hist.clear()
                self.strafe = -self.strafe
                self.stuck_n += 1
                if me["grounded"] and me["energy"] > 18:
                    jump = True
                    self.jet_until = t + 1.6
                else:
                    # sidestep along the wall for a moment
                    a = math.atan2(move[1], move[0]) + (1.6 if self.stuck_n % 2 else -1.6)
                    self.escape = (math.cos(a), math.sin(a))
                    self.escape_until = t + 0.8
        return jump, jet

    # ================================================================== no enemy in sight
    def _combat_loot(self, obs, enemies):
        """Collect accessible loot during a duel while keeping the torso on the fight."""
        if self.hp_frac < 0.42:
            return None
        here = tuple(self.pos[:2])
        weapons = self._weapons()
        nearby = [enemy for enemy in enemies if enemy["d"] < 90 and self._has_los(enemy)]
        if len(nearby) > 1:
            return None
        under_fire = self.t - self.last_dmg_t < 1.0
        cube_cap = self.cfg["cubes"]["stat_cap"]
        candidates = []
        for label, items, limit, credit in (
            ("combat cube", obs["cubes"], 20 if under_fire else 32, 15 if self.me["cubes"] < cube_cap else 5),
            ("combat weapon", obs["pickups"], 28 if under_fire else 48, 25 if len(weapons) == 1 else 8),
        ):
            for item in items:
                if label == "combat weapon" and not self._want_weapon(item["kind"], weapons):
                    continue
                point = item["pos"]
                xy = tuple(point[:2])
                distance = math.dist(here, xy)
                if abs(point[2] - self.pos[2]) > 4 or distance > limit or not self._in_zone_later(obs, xy):
                    continue
                if any(math.dist(enemy["pos"][:2], xy) < min(enemy["d"] * 0.8, distance + 14)
                       for enemy in nearby):
                    continue
                candidates.append((distance - credit, item["id"], xy, label))
        for _, _, xy, label in sorted(candidates)[:3]:
            if self.nav is None or self.nav.walk_clear(here, xy):
                return norm2(xy[0] - here[0], xy[1] - here[1]), label
        return None

    def _supply_move(self, obs, enemies):
        if not obs["drops"] or "drops" not in self.cfg:
            return None
        stats = self.me["stats"]
        speed = stats["walk_speed"]
        info = self.cfg["drops"]
        cubes = self.cfg["cubes"]
        missing_cubes = max(0, cubes["stat_cap"] - self.me["cubes"])
        gain = (1 - self.hp_frac) * info["repair"] + min(missing_cubes, info["cubes"]) * cubes["hp_per_cube"] * 2
        if gain < 0.05:
            return None

        x, y, _ = self.pos
        best = None
        for drop in obs["drops"]:
            dx, dy, dz = drop["pos"]
            radius = drop["radius"]
            distance = math.hypot(dx - x, dy - y)
            eta = max(0, distance - radius * 0.5) / speed
            progress = drop["progress"] if drop["holder"] == self.me["id"] else 0
            capture = drop["capture_s"] - progress
            time_needed = max(eta, drop["lands_in"]) + capture
            if time_needed > 9 + gain * 12 or drop["lands_in"] > eta + 5:
                continue
            if not self._in_zone_later(obs, (dx, dy), time_needed + 1):
                continue

            contested = False
            lanes = 0
            for enemy in enemies:
                ex, ey, ez = enemy["pos"]
                enemy_distance = math.hypot(dx - ex, dy - ey)
                if enemy_distance < radius * 1.25 and self._enemy_health(enemy) > 0.2:
                    contested = True
                    break
                if not enemy["aggressor"] or enemy_distance > self.W["gun"]["range"] * 0.75:
                    continue
                if self.nav is None or self.nav.los(
                    (ex, ey, ez + self.mech["muzzle_height"] * self._size(enemy)),
                    (dx, dy, dz + stats["height"] / 2),
                ):
                    lanes += 1
            if contested or lanes > 1 or (lanes and time_needed > 7 + gain * 4):
                continue
            score = (gain + progress / drop["capture_s"]) / (1 + time_needed + 2 * lanes)
            if best is None or score > best[0]:
                best = score, drop, distance, lanes

        if best is None:
            return None
        _, drop, distance, lanes = best
        center = drop["pos"][:2]
        if distance > drop["radius"] * 0.6:
            return self._navigate(center), "drop"
        if lanes:
            # Keep moving inside the ring while another mech has a firing lane.
            angle = self.t * 1.7 + self.scan_phase
            inner = drop["radius"] * 0.38
            vx = (center[0] + math.cos(angle) * inner - x) * 2 / speed
            vy = (center[1] + math.sin(angle) * inner - y) * 2 / speed
            return self._clamp(vx, vy), "capturing under fire"
        return (0.0, 0.0), "capturing"

    def _peaceful(self, obs, npcs, zone_goal):
        x, y, z = self.pos
        t = self.t
        me = self.me
        self.farm_kind = None
        wk = self._weapons()
        ok = lambda p: self._in_zone_later(obs, p, 10.0, 10.0)
        hurt = self.hp_frac < 0.65

        if hurt:
            return self._heal(obs, zone_goal)

        # cubes
        best = None
        for c in obs.get("cubes") or []:
            cp = c["pos"]
            if abs(cp[2] - z) > 3.5 and abs(cp[2] - self._ground(cp)) > 1:
                continue
            dd = math.dist((x, y), cp[:2])
            if dd < 90 and ok(cp[:2]) and (best is None or dd < best[0]):
                best = (dd, cp)
        if best:
            return self._navigate(best[1][:2]), None, "cube"

        # weapons worth taking
        best = None
        for p in obs.get("pickups") or []:
            if p["id"] in self.bad_pickups and t - self.bad_pickups[p["id"]] < 20:
                continue
            if not self._want_weapon(p["kind"], wk):
                continue
            pp = p["pos"]
            if pp[2] - self._ground(pp) > 2.0 and pp[2] - z > 2.0:
                if me["energy"] < 70:
                    continue  # on a roof: only when we can jet up
            dd = math.dist((x, y), pp[:2])
            if dd < 110 and ok(pp[:2]) and (best is None or dd < best[0]):
                best = (dd, pp)
        if best:
            dd, pp = best
            mv = self._navigate(pp[:2])
            if pp[2] - z > 2.0 and dd < 12:
                self.jet_until = max(self.jet_until, t + 1.2)
                if me["grounded"]:
                    self.want_jump = True
            return mv, None, "weapon"

        # crates: shoot from range
        best = None
        for c in obs.get("crates") or []:
            cp = c["pos"]
            dd = math.dist((x, y), cp[:2])
            if dd < 70 and ok(cp[:2]) and (best is None or dd < best[0]):
                best = (dd, cp)
        if best:
            dd, cp = best
            self.farm_kind = "crate"
            p = (cp[0], cp[1], cp[2] + self.cfg["crates"]["height"] / 2)
            mv = (0.0, 0.0) if dd < 35 else self._navigate(cp[:2])
            return mv, p, "crate"

        # NPC drones: two cubes each, shoot from outside their aggro range
        if self.hp_frac > 0.6 and me["cubes"] < self.cfg["cubes"]["stat_cap"] and "gun" in wk:
            best = None
            for nn in npcs:
                dd = math.dist((x, y), nn["pos"][:2])
                if dd < 140 and ok(nn["pos"][:2]) and (best is None or dd < best[0]):
                    best = (dd, nn)
            if best:
                dd, nn = best
                self.farm_kind = "npc"
                p = nn["pos"]
                travel = dd / wk["gun"][1]["stats"]["speed"]
                aimp = (p[0] + nn["vel"][0] * travel, p[1] + nn["vel"][1] * travel,
                        p[2] + self.cfg["npc"]["height"] / 2)
                if dd > 85:
                    mv = self._navigate(p[:2])
                elif dd < 55:
                    ux, uy = norm2(x - p[0], y - p[1])
                    mv = self._steer_free(ux, uy)
                else:
                    ux, uy = norm2(p[0] - x, p[1] - y)
                    mv = self._steer_free(-uy * self.strafe * 0.6, ux * self.strafe * 0.6)
                    if t > self.strafe_until:
                        self.strafe = -self.strafe
                        self.strafe_until = t + 1.0 + self.rng.random()
                return mv, aimp, "npc"

        # nothing to do: move to the safe circle, then hold near cover
        if math.dist((x, y), zone_goal) > 3:
            return self._navigate(zone_goal), None, "rotate"
        return self._hold(obs), None, "hold"

    def _heal(self, obs, zone_goal):
        """Hurt and nobody in sight: grab very close cubes, then hide from where enemies were and keep still
        (standing still is silent) until regeneration brings us back up."""
        x, y, z = self.pos
        t = self.t
        for c in obs.get("cubes") or []:
            cp = c["pos"]
            if math.dist((x, y), cp[:2]) < 25 and abs(cp[2] - z) < 3.5:
                return self._navigate(cp[:2]), None, "heal cube"
        recent = [(p, tt) for p, tt in self.last_seen.values() if t - tt < 25]
        if self.nav is not None and recent:
            if (self.cover is None or t - self.cover_t > 2.0) and self._heavy_ok():
                self.heavy_used = True
                self.cover_t = t
                ghosts = [{"id": -1 - k, "pos": p, "d": math.dist(p[:2], (x, y))} for k, (p, _) in enumerate(recent[:4])]
                self.cover = self._find_spot(ghosts, obs, None)
            if self.cover is not None and math.dist(self.cover, (x, y)) > 2.5:
                return self._navigate(self.cover), None, "heal hide"
        if math.dist((x, y), zone_goal) > 3 and not self._in_zone_later(obs, (x, y), 15.0, 10.0):
            return self._navigate(zone_goal), None, "heal rotate"
        return (0.0, 0.0), None, "heal"

    def _hold(self, obs):
        """Drift gently toward the centre of the next circle, staying quiet (slow walking is silent)."""
        x, y = self.pos[0], self.pos[1]
        nc = obs["zone"]["next_center"]
        nr = obs["zone"]["next_radius"]
        d = math.dist((x, y), nc)
        if d > max(3.0, nr * 0.35):
            mx, my = self._navigate(nc)
            move = min(0.4, 0.98 * self.cfg["sound"]["step_speed"] / self.me["stats"]["walk_speed"])
            return mx * move, my * move
        return (0.0, 0.0)

    def _ground(self, p):
        if self.nav is None:
            return p[2]
        return self.nav.ground_at(p[0], p[1])

    def _want_weapon(self, kind, wk):
        if kind in wk or kind == "gun":
            return False
        if self.me["weapons"][1] is None or self.me["weapons"][0] is None:
            return True
        # both full: swap the worse special for a better one
        specials = [k for k in wk if k != "gun"]
        if not specials:
            return False
        worst = min(specials, key=lambda k: WEAPON_VALUE.get(k, 0))
        return WEAPON_VALUE.get(kind, 0) > WEAPON_VALUE.get(worst, 0) + 0.25

    def _take(self, obs):
        x, y, z = self.pos
        wk = self._weapons()
        me = self.me
        for p in obs.get("pickups") or []:
            pp = p["pos"]
            if math.hypot(pp[0] - x, pp[1] - y) > self.reach * 0.95 or abs(pp[2] - z) > 3.8:
                continue
            if not self._want_weapon(p["kind"], wk):
                continue
            out = {"take": p["id"]}
            if me["weapons"][0] is not None and me["weapons"][1] is not None:
                specials = [(k, i) for k, (i, w) in wk.items() if k != "gun"]
                k, i = min(specials, key=lambda s: WEAPON_VALUE.get(s[0], 0))
                w = me["weapons"][i]
                if w.get("kind") == "laser" and (w.get("charge") or 0) > 0:
                    continue
                out["drop"] = i
            return out
        return None


mf.run(Bot)
