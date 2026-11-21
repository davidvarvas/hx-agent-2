"""Static map model: a 2 m grid of ground height and building solids, line of sight, and A* paths.

Built once at hello from hello.map.dir (v2/world.json + v2/terrain.i16). Everything is numpy so that
batches of line-of-sight rays (cover search) stay cheap inside the 50 ms budget.
"""
import heapq
import json
import math
import os
import time

import numpy as np

CELL = 2.0
NEG = -1e4


class NavMap:
    def __init__(self, map_dir, mech):
        mech_height = mech["height"]
        step_up = mech["step_up"]
        required_clearance = math.ceil(mech["radius"] / CELL)
        with open(os.path.join(map_dir, "v2", "world.json")) as f:
            w = json.load(f)
        t = w["terrain"]
        raw = np.fromfile(os.path.join(map_dir, "v2", t["file"]), dtype="<i2").astype(np.float32)
        tnx, tny = t["nx"], t["ny"]
        terr = raw.reshape(tny, tnx) * float(t.get("scale", 0.01))
        self.minx, self.miny = float(t["minX"]), float(t["minY"])
        self.nx = int((tnx - 1) * t["step"] / CELL)
        self.ny = int((tny - 1) * t["step"] / CELL)
        nx, ny = self.nx, self.ny
        # cell (i, j) center = min + (i + 0.5) * CELL; sample terrain at the nearest vertex
        ti = np.clip(((np.arange(nx) + 0.5) * CELL / t["step"]).round().astype(int), 0, tnx - 1)
        tj = np.clip(((np.arange(ny) + 0.5) * CELL / t["step"]).round().astype(int), 0, tny - 1)
        self.ground = terr[np.ix_(tj, ti)].astype(np.float32)  # [ny, nx]
        self.top = np.full((ny, nx), NEG, np.float32)   # top of building/stadium solid in the cell
        self.base = np.full((ny, nx), 1e4, np.float32)
        self.btop = np.full((ny, nx), NEG, np.float32)  # bridges
        self.bbase = np.full((ny, nx), 1e4, np.float32)
        for p in w["prisms"]:
            mask = self._raster(p["ring"], p.get("holes") or [])
            if mask is None:
                continue
            (j0, j1, i0, i1), m = mask
            if p["kind"] == "bridge":
                sl = (slice(j0, j1), slice(i0, i1))
                self.btop[sl] = np.where(m, np.maximum(self.btop[sl], p["top"]), self.btop[sl])
                self.bbase[sl] = np.where(m, np.minimum(self.bbase[sl], p["base"]), self.bbase[sl])
            else:
                sl = (slice(j0, j1), slice(i0, i1))
                self.top[sl] = np.where(m, np.maximum(self.top[sl], p["top"]), self.top[sl])
                self.base[sl] = np.where(m, np.minimum(self.base[sl], p["base"]), self.base[sl])
        g = self.ground
        # a cell blocks ground movement if a solid rises more than a step above the ground there and its
        # underside is below head height
        solid = (self.top > g + step_up) & (self.base < g + mech_height)
        bridge_low = (self.btop > g + step_up) & (self.bbase < g + mech_height) & (self.bbase > g + 0.5)
        # steep terrain
        dzx = np.abs(np.diff(g, axis=1))
        dzy = np.abs(np.diff(g, axis=0))
        steep = np.zeros_like(solid)
        lim = CELL * math.tan(math.radians(mech["max_slope_deg"])) * 0.95
        steep[:, 1:] |= dzx > lim
        steep[:, :-1] |= dzx > lim
        steep[1:, :] |= dzy > lim
        steep[:-1, :] |= dzy > lim
        blocked = solid | bridge_low | steep
        blocked[0, :] = blocked[-1, :] = blocked[:, 0] = blocked[:, -1] = True
        self.blocked = blocked
        # height of whatever is solid in the cell (for LOS against roofs, and for "standing on a roof")
        self.solid_top = np.where(self.top > g, self.top, g)
        self.clear = self._clearance(blocked)  # distance to nearest blocked cell, in cells
        self.free_flat = (self.clear >= required_clearance).ravel().tolist()
        self.free_tight = (self.clear >= max(1, required_clearance - 1)).ravel().tolist()
        self.cost_flat = np.where(self.clear >= 4, 1.0, np.where(self.clear >= 3, 1.3, 2.0)).ravel().tolist()
        # coarse 4 m grid for path search: a coarse cell is free if any of its 2x2 fine cells is comfortable
        cny, cnx = ny // 2, nx // 2
        blk = self.clear[:cny * 2, :cnx * 2].reshape(cny, 2, cnx, 2)
        c = blk.max(axis=(1, 3))
        cmin = blk.min(axis=(1, 3))
        self.cnx, self.cny = cnx, cny
        self.cfree = ((c >= required_clearance) & (cmin >= 1)).ravel().tolist()
        self.ccost = np.where(c >= 5, 1.0, np.where(c >= 3, 1.25, 1.8)).ravel().tolist()

    # ------------------------------------------------------------------ build helpers
    def _raster(self, ring, holes):
        xs = ring[0::2]
        ys = ring[1::2]
        if len(xs) < 3:
            return None
        i0 = max(0, int((min(xs) - self.minx) / CELL) - 1)
        i1 = min(self.nx, int((max(xs) - self.minx) / CELL) + 2)
        j0 = max(0, int((min(ys) - self.miny) / CELL) - 1)
        j1 = min(self.ny, int((max(ys) - self.miny) / CELL) + 2)
        if i1 <= i0 or j1 <= j0:
            return None
        cx = self.minx + (np.arange(i0, i1) + 0.5) * CELL
        cy = self.miny + (np.arange(j0, j1) + 0.5) * CELL
        X, Y = np.meshgrid(cx, cy)
        m = _pip(X, Y, xs, ys)
        for h in holes:
            if len(h) >= 6:
                m &= ~_pip(X, Y, h[0::2], h[1::2])
        return (j0, j1, i0, i1), m

    @staticmethod
    def _clearance(blocked):
        ny, nx = blocked.shape
        INF = 10 ** 6
        d = np.where(blocked, 0, INF).astype(np.int32)
        # two-pass chamfer (8-neighbour, unit cost) distance transform, row-vectorised
        for j in range(ny):
            row = d[j]
            if j > 0:
                prev = d[j - 1]
                row = np.minimum(row, prev + 1)
                row[1:] = np.minimum(row[1:], prev[:-1] + 1)
                row[:-1] = np.minimum(row[:-1], prev[1:] + 1)
            acc = np.minimum.accumulate(row - np.arange(nx)) + np.arange(nx)
            row = np.minimum(row, acc)
            acc = (np.minimum.accumulate((row + np.arange(nx))[::-1]))[::-1] - np.arange(nx)
            d[j] = np.minimum(row, acc)
        for j in range(ny - 2, -1, -1):
            nxt = d[j + 1]
            row = np.minimum(d[j], nxt + 1)
            row[1:] = np.minimum(row[1:], nxt[:-1] + 1)
            row[:-1] = np.minimum(row[:-1], nxt[1:] + 1)
            acc = np.minimum.accumulate(row - np.arange(nx)) + np.arange(nx)
            row = np.minimum(row, acc)
            acc = (np.minimum.accumulate((row + np.arange(nx))[::-1]))[::-1] - np.arange(nx)
            d[j] = np.minimum(row, acc)
        return d

    # ------------------------------------------------------------------ queries
    def cell(self, x, y):
        i = int((x - self.minx) / CELL)
        j = int((y - self.miny) / CELL)
        if i < 0:
            i = 0
        elif i >= self.nx:
            i = self.nx - 1
        if j < 0:
            j = 0
        elif j >= self.ny:
            j = self.ny - 1
        return i, j

    def center(self, i, j):
        return self.minx + (i + 0.5) * CELL, self.miny + (j + 0.5) * CELL

    def ground_at(self, x, y):
        i, j = self.cell(x, y)
        return float(self.ground[j, i])

    def is_free(self, x, y, tight=False):
        i, j = self.cell(x, y)
        return (self.free_tight if tight else self.free_flat)[j * self.nx + i]

    def los(self, a, b, step=1.6):
        """True if the straight segment a->b (3D) clears buildings, bridges and terrain."""
        return bool(self.los_many(np.array([a], np.float32), np.array([b], np.float32), step)[0])

    def los_many(self, A, B, step=1.6):
        """Vectorised LOS for N segments A[i] -> B[i] (N x 3 arrays). Returns a bool array."""
        A = np.asarray(A, np.float32)
        B = np.asarray(B, np.float32)
        if len(A) == 0:
            return np.zeros(0, bool)
        D = B - A
        L = np.sqrt((D[:, :2] ** 2).sum(1))
        n = int(max(2, min(260, L.max() / step + 2)))
        s = np.linspace(0.02, 0.98, n, dtype=np.float32)
        P = A[:, None, :] + D[:, None, :] * s[None, :, None]   # N x n x 3
        i = ((P[..., 0] - self.minx) / CELL).astype(np.int32).clip(0, self.nx - 1)
        j = ((P[..., 1] - self.miny) / CELL).astype(np.int32).clip(0, self.ny - 1)
        z = P[..., 2]
        hit = (z < self.top[j, i]) & (z > self.base[j, i])
        hit |= (z < self.btop[j, i]) & (z > self.bbase[j, i])
        hit |= z < self.ground[j, i] - 0.3
        return ~hit.any(1)

    # ------------------------------------------------------------------ path finding
    def nearest_free(self, x, y, tight=False, max_r=12):
        i, j = self.cell(x, y)
        free = self.free_tight if tight else self.free_flat
        nx = self.nx
        if free[j * nx + i]:
            return i, j
        for r in range(1, max_r + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in (-r, r) if abs(dj) != r else range(-r, r + 1):
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < self.ny and free[jj * nx + ii]:
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, ii, jj)
            if best:
                return best[1], best[2]
        return None

    def _cnear(self, x, y, max_r=8):
        ci = min(self.cnx - 1, max(0, int((x - self.minx) / (2 * CELL))))
        cj = min(self.cny - 1, max(0, int((y - self.miny) / (2 * CELL))))
        free = self.cfree
        nx = self.cnx
        if free[cj * nx + ci]:
            return ci, cj
        for r in range(1, max_r + 1):
            best = None
            for dj in range(-r, r + 1):
                for di in ((-r, r) if abs(dj) != r else range(-r, r + 1)):
                    ii, jj = ci + di, cj + dj
                    if 0 <= ii < nx and 0 <= jj < self.cny and free[jj * nx + ii]:
                        d = di * di + dj * dj
                        if best is None or d < best[0]:
                            best = (d, ii, jj)
            if best:
                return best[1], best[2]
        return None

    def astar(self, start, goal, max_expand=3000, weight=1.5, deadline=math.inf):
        """Weighted A* on the 4 m coarse grid between world points. Returns world (x, y) waypoints
        (string-pulled on the fine grid), or a partial path toward the goal, or None."""
        s = self._cnear(*start)
        g = self._cnear(*goal, max_r=15)
        if s is None or g is None:
            return None
        nx = self.cnx
        ny = self.cny
        free = self.cfree
        cost = self.ccost
        si = s[1] * nx + s[0]
        gi = g[1] * nx + g[0]
        gx, gy = g
        cs = 2 * CELL
        center = lambda c: (self.minx + (c % nx + 0.5) * cs, self.miny + (c // nx + 0.5) * cs)
        if si == gi:
            return [tuple(goal)]
        openh = [(0.0, si, 0.0)]
        gs = {si: 0.0}
        came = {}
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
                (1, 1, 1.4142), (1, -1, 1.4142), (-1, 1, 1.4142), (-1, -1, 1.4142))
        exp = 0
        best = (1e18, si)
        push = heapq.heappush
        pop = heapq.heappop
        while openh and exp < max_expand:
            if exp % 32 == 0 and time.perf_counter() >= deadline:
                break
            _, cur, gc = pop(openh)
            if gc != gs[cur]:
                continue
            if cur == gi:
                best = (0, cur)
                break
            exp += 1
            cj, ci = divmod(cur, nx)
            hcur = abs(ci - gx) + abs(cj - gy)
            if hcur < best[0]:
                best = (hcur, cur)
            for di, dj, dc in nbrs:
                ii = ci + di
                jj = cj + dj
                if ii < 0 or jj < 0 or ii >= nx or jj >= ny:
                    continue
                n = jj * nx + ii
                if not free[n]:
                    continue
                if di and dj and not (free[cj * nx + ii] and free[jj * nx + ci]):
                    continue
                ng = gc + dc * cost[n]
                if ng < gs.get(n, 1e18):
                    gs[n] = ng
                    came[n] = cur
                    adx = abs(ii - gx)
                    ady = abs(jj - gy)
                    h = (adx + ady) - 0.5858 * (adx if adx < ady else ady)
                    push(openh, (ng + weight * h, n, ng))
        end = best[1]
        cells = [end]
        while cells[-1] in came:
            cells.append(came[cells[-1]])
        cells.reverse()
        pts = [center(c) for c in cells]
        if end == gi:
            pts[-1] = tuple(goal)
        return self._smooth(pts, deadline)

    def _smooth(self, pts, deadline):
        if len(pts) <= 2:
            return pts[1:] or pts
        out = []
        k = 0
        while k < len(pts) - 1:
            if time.perf_counter() >= deadline:
                out.extend(pts[k + 1:])
                break
            far = k + 1
            # jump as far ahead as a straight walk stays on free cells
            hi = min(len(pts) - 1, k + 40)
            for m in range(hi, k + 1, -1):
                if self.walk_clear(pts[k], pts[m]):
                    far = m
                    break
            out.append(pts[far])
            k = far
        return out

    def walk_clear(self, a, b):
        ax, ay = a
        bx, by = b
        d = math.hypot(bx - ax, by - ay)
        n = int(d / (CELL * 0.5)) + 1
        free = self.free_flat
        nx = self.nx
        for k in range(n + 1):
            t = k / n
            i, j = self.cell(ax + (bx - ax) * t, ay + (by - ay) * t)
            if not free[j * nx + i]:
                return False
        return True


def _pip(X, Y, xs, ys):
    """Even-odd point-in-polygon for grids X, Y against polygon (xs, ys)."""
    inside = np.zeros(X.shape, bool)
    n = len(xs)
    j = n - 1
    for i in range(n):
        xi, yi, xj, yj = xs[i], ys[i], xs[j], ys[j]
        if yi != yj:
            cond = (yi > Y) != (yj > Y)
            xint = (xj - xi) * (Y - yi) / (yj - yi) + xi
            inside ^= cond & (X < xint)
        j = i
    return inside
