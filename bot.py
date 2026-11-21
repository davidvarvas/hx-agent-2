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
        self.reach = cfg["loot"]["pickup_radius"]
        self.nav = None
        if nav is not None:
            try:
                self.nav = nav.NavMap(hello["map"]["dir"], self.mech["height"], self.mech["step_up"])
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
        self.want_jump = False
        self.farm_kind = None
        t = self.t = obs["t"]
        me = self.me = obs["self"]
        x, y, z = self.pos = me["pos"]
        self.hp_frac = me["hp"] / max(1.0, me["max_hp"])
        st = me["stats"]
        self.muzzle = st.get("muzzle_height", 7.0)
        self.eye = st.get("eye_height", 8.0)
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
        target = self._choose_target(enemies)
        fight = target is not None and self._should_fight(target, enemies)
        self.target_los = target is not None and self._has_los(target)

        move = (0.0, 0.0)
        boost = False
        label = ""
        aim_point = None   # world point to aim at (non-combat)

        # ---------------- movement decision
        retreat = target is not None and self._should_retreat(target, enemies)
        if zone_urgent:
            move = self._navigate(zone_goal)
            boost = not me["in_zone"] or self._outside_soon(obs)
            label = "zone"
        elif retreat and not (target["d"] < 38 or t - self.last_dmg_t < 0.6 and self.target_los):
            move, label = self._retreat(enemies, zone_goal, obs)
            boost = me["energy"] > 30 and t - self.last_dmg_t < 1.5
        elif target is not None and (fight or target["d"] < 70) and self._outnumbered(enemies):
            move, label = self._retreat(enemies, zone_goal, obs, fight_target=target)
            boost = me["energy"] > 45 and t - self.last_dmg_t < 1.0
        elif target is not None and (fight or target["d"] < 70):
            move = self._fight_move(target, enemies, zone_goal, obs)
            boost = me["energy"] > 45 and t - self.last_dmg_t < 1.0
            label = f"fight {target['id']}"
        else:
            move, aim_point, label = self._peaceful(obs, npcs, zone_goal)

        if self.escape is not None and t < self.escape_until:
            move = self.escape
        move = self._dodge(obs, move)
        move = self._keep_in_zone(obs, move)

        # ---------------- aim + fire
        fire = [False, False]
        aim = None
        if target is not None:
            aim = self._lead_aim(target)
            if fight or t - self.attackers.get(target["id"], -99) < 4:
                fire = self._fire_control(target, aim, enemies, obs)
        if aim is None and aim_point is not None:
            aim = self._aim_at_point(aim_point)
            fire = self._fire_at_object(aim, aim_point, obs)
        if aim is None:
            aim = self._idle_aim(obs, move)
        act["aim"] = [wrap(aim[0]), max(self.mech["min_pitch"], min(self.mech["max_pitch"], aim[1]))]

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
        act["move"] = [round(mvx, 3), round(mvy, 3)]
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
        speed = self.me["stats"].get("walk_speed", 11.7)
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
    def _choose_target(self, enemies):
        best, bs = None, 1e9
        t = self.t
        for e in enemies:
            if e["d"] > 260:
                continue
            hpf = e["hp"] / max(1.0, e.get("max_hp", 1400))
            s = e["d"] / 90.0 + hpf * 1.6
            if t - self.attackers.get(e["id"], -99) < 3:
                s -= 1.0
            if e.get("charging"):
                s -= 0.4
            if s < bs:
                best, bs = e, s
        return best

    def _should_fight(self, tgt, enemies):
        t = self.t
        if t - self.attackers.get(tgt["id"], -99) < 6:
            return True
        hpf = tgt["hp"] / max(1.0, tgt.get("max_hp", 1400))
        if tgt["d"] < 110:
            return True
        if hpf < 0.45 and tgt["d"] < 200:
            return True
        if self.hp_frac > 0.75 and tgt["d"] < 160:
            return True
        return False

    def _should_retreat(self, tgt, enemies):
        my = self.me["hp"]
        th = tgt["hp"]
        close = [e for e in enemies if e["d"] < 120]
        if self.hp_frac < 0.3 and th > my * 0.8:
            return True
        if self.hp_frac < 0.55 and len(close) >= 2:
            return True
        if len(close) >= 3 and self.hp_frac < 0.85:
            return True
        return False

    def _outnumbered(self, enemies):
        close = [e for e in enemies if e["d"] < 170]
        return len(close) >= 2

    def _has_los(self, e):
        if self.nav is None:
            return True
        x, y, z = self.pos
        ex, ey, ez = e["pos"]
        sc = 1 + 0.05 * e.get("cubes", 0)
        return self.nav.los((x, y, z + self.muzzle), (ex, ey, ez + 4.5 * sc))

    # ================================================================== aiming
    def _lead_aim(self, e):
        x, y, z = self.pos
        mz = z + self.muzzle
        ex, ey, ez = e["pos"]
        vx, vy, vz = e.get("vel", (0, 0, 0))
        sc = 1 + 0.05 * e.get("cubes", 0)
        cz = ez + (4.5 * sc if e["kind"] == "mech" else 1.75)
        wk = self._weapons()
        speed = 252.0
        if "gun" in wk:
            speed = wk["gun"][1]["stats"].get("speed", 252.0)
        if not self.target_los and "mortar" in wk:
            return self._mortar_aim(e, wk["mortar"][1])
        if "gun" not in wk or wk["gun"][1].get("ammo", 1) == 0 or wk["gun"][1].get("reload", 0) > 0:
            speed = 1e9  # hitscan weapons: no lead
        if "laser" in wk:
            lw = wk["laser"][1]
            if (lw.get("charge") or 0) >= lw["stats"].get("charge", 0.75) * 0.5:
                speed = 1e9  # laser about to fire: aim straight at it
        tt = 0.0
        g = self.mech["gravity"]
        airborne = vz != 0
        for _ in range(3):
            px = ex + vx * tt
            py = ey + vy * tt
            pz = cz + vz * tt - (0.5 * g * tt * tt if airborne else 0)
            d = math.dist((px, py, pz), (x, y, mz))
            tt = min(1.2, d / speed)
        dx, dy = px - x, py - y
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(pz - mz, math.hypot(dx, dy))
        self.aim_pred = (px, py, pz)
        return (yaw, pitch)

    def _mortar_aim(self, e, w):
        x, y, z = self.pos
        mz = z + st_h(self.me) + 0.5
        ex, ey, ez = e["pos"]
        vx, vy, _ = e.get("vel", (0, 0, 0))
        v = w["stats"].get("speed", 72.0)
        g = w["stats"].get("gravity", 22.0)
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
            if th > self.mech["max_pitch"]:
                th = math.atan((v * v - math.sqrt(disc)) / (g * d))
            sol = (math.atan2(py - y, px - x), th)
            tt = d / max(1e-3, v * math.cos(th))
        self.aim_pred = (ex, ey, ez)
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
        sc = 1 + 0.05 * e.get("cubes", 0)
        rad = 2.5 * sc
        eyaw, epitch = self._aim_err(aim)
        tol = math.atan2(rad * 0.85, max(1.0, d3)) + 0.004
        ptol = math.atan2(4.0 * sc, max(1.0, d3)) + 0.01
        los = self.target_los
        for i, w in enumerate(me["weapons"]):
            if not w:
                continue
            k = w["kind"]
            s = w.get("stats", {})
            if k == "gun":
                tspeed = math.hypot(*e.get("vel", (0, 0, 0))[:2])
                max_d = 125.0 if tspeed > 4 else 190.0
                if t_attacked(self, e) and d3 < 160:
                    max_d = 160.0
                if los and d3 < min(max_d, s.get("range", 230) * 0.98) and w.get("ammo", 1) > 0 and w.get("reload", 0) == 0 \
                        and eyaw < tol and epitch < ptol:
                    fire[i] = True
            elif k == "lightning":
                rng = s.get("range", 45)
                cone = math.radians(s.get("cone_deg", 30)) / 2
                cost = s.get("energy", 3)
                if me["energy"] > cost + 8:
                    for o in enemies:
                        if o["d"] < rng * 0.95:
                            b = math.atan2(o["pos"][1] - self.pos[1], o["pos"][0] - self.pos[0])
                            if abs(wrap(b - me["aim"][0])) < cone * 0.85 and (o is not e or los):
                                fire[i] = True
                                break
            elif k == "laser":
                charge = w.get("charge", 0) or 0
                full = s.get("charge", 0.75)
                shorted = any(o["d"] < 50 and "lightning" in (o.get("weapons") or []) for o in enemies)
                if charge > 0:
                    # holding: release when aimed (beam is hitscan) or keep holding
                    ready = charge >= full * 0.97
                    on = los and eyaw < tol * 0.9 and epitch < ptol
                    fire[i] = not (ready and on) and charge < full + s.get("hold_s", 2) - 0.15
                    if not los and charge >= full:
                        fire[i] = True  # keep it charged while target is behind cover
                elif w.get("cooldown", 0) <= 0.05 and los and d3 < s.get("range", 280) * 0.95 and not shorted \
                        and eyaw < 0.5:
                    fire[i] = True
            elif k == "mortar":
                sol = getattr(self, "mortar_sol", None)
                if w.get("cooldown", 0) <= 0 and 22 < d3 < 230 and sol is not None and not los and \
                        eyaw < 0.03 and epitch < 0.03 and self._mortar_path_ok(sol):
                    fire[i] = True
                elif w.get("cooldown", 0) <= 0 and los and 25 < d3 < 140 and "gun" not in self._weapons():
                    fire[i] = eyaw < tol and epitch < ptol
        return fire

    def _mortar_path_ok(self, sol):
        if self.nav is None:
            return True
        x, y, z = self.pos
        mz = z + st_h(self.me) + 0.5
        v = 72.0
        yaw, th = sol
        pts = []
        for k in range(1, 7):
            tt = k * 0.25
            h = v * math.cos(th) * tt
            pts.append((x + math.cos(yaw) * h, y + math.sin(yaw) * h, mz + v * math.sin(th) * tt - 11 * tt * tt))
        A = [(x, y, mz)] + pts[:-1]
        return bool(self.nav.los_many(A, pts).all())

    def _fire_at_object(self, aim, p, obs):
        """Shoot crates/NPCs we chose to farm."""
        if getattr(self, "farm_kind", None) is None:
            return [False, False]
        fire = [False, False]
        d = math.dist(self.pos, p)
        eyaw, epitch = self._aim_err(aim)
        rad = 1.4 if self.farm_kind == "crate" else 1.8
        tol = math.atan2(rad * 0.8, max(1.0, d)) + 0.004
        x, y, z = self.pos
        if self.nav is not None and not self.nav.los((x, y, z + self.muzzle), p):
            return fire
        for i, w in enumerate(self.me["weapons"]):
            if not w:
                continue
            if w["kind"] == "gun" and w.get("ammo", 1) > 0 and w.get("reload", 0) == 0 and eyaw < tol and epitch < tol * 2 + 0.01 and d < 200:
                fire[i] = True
            if w["kind"] == "lightning" and d < w["stats"].get("range", 45) * 0.9 and eyaw < 0.2 and self.me["energy"] > 40:
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
        mag = w["stats"].get("mag", 12)
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
            self.path = n.astar((x, y), (gx, gy), max_expand=3000)
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
        return not self.heavy_used and (time.perf_counter() - self.t0) < 0.012

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
        hpf_t = tgt["hp"] / max(1.0, tgt.get("max_hp", 1400))
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
                A.append((ex, ey, ez + 7.5))
                B.append((px, py, gz + 5.0))
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
        g = self.W["mortar"].get("gravity", 22.0)
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
            if dd < 11:
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
    def _peaceful(self, obs, npcs, zone_goal):
        x, y, z = self.pos
        t = self.t
        me = self.me
        self.farm_kind = None
        wk = self._weapons()
        ok = lambda p: self._in_zone_later(obs, p, 10.0, 10.0)
        hurt = self.hp_frac < 0.65

        # supply drop
        for dr in obs.get("drops") or []:
            dp = dr["pos"]
            dd = math.dist((x, y), dp[:2])
            if dd < 170 and dr.get("lands_in", 0) < dd / 11.7 + 3 and ok(dp[:2]):
                if dd > dr.get("radius", 8) * 0.5:
                    return self._navigate(dp[:2]), None, "drop"
                return (0.0, 0.0), None, "capturing"

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
            p = (cp[0], cp[1], cp[2] + 1.2)
            mv = (0.0, 0.0) if dd < 35 else self._navigate(cp[:2])
            return mv, p, "crate"

        # NPC drones: two cubes each, shoot from outside their aggro range
        if self.hp_frac > 0.6 and me["cubes"] < 10 and "gun" in wk:
            best = None
            for nn in npcs:
                dd = math.dist((x, y), nn["pos"][:2])
                if dd < 140 and ok(nn["pos"][:2]) and (best is None or dd < best[0]):
                    best = (dd, nn)
            if best:
                dd, nn = best
                self.farm_kind = "npc"
                p = nn["pos"]
                aimp = (p[0] + nn["vel"][0] * dd / 252, p[1] + nn["vel"][1] * dd / 252, p[2] + 1.75)
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
            return mx * 0.4, my * 0.4  # 0.4 * 11.7 < 5 m/s: no footstep sound
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


def t_attacked(bot, e):
    return bot.t - bot.attackers.get(e["id"], -99) < 3


def st_h(me):
    return me["stats"].get("height", 9.0)


mf.run(Bot)
