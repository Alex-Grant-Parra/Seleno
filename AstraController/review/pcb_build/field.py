"""Exact clearance fields on a 0.1 mm node grid (board-local mm, u right, v down).

For each routing layer we keep, per node, the distance to the nearest copper and the net it belongs to,
plus the distance to the nearest copper of any *other* net (d1/n1/d2). A second set (e1/m1/e2) stores
distance minus that obstacle's own clearance. Together they give an exact "is a track of net N with
half-width hw legal here" test for KiCad's max(clrA, clrB) rule:  d_other >= hw + clrN  and  e_other >= hw.
"""
import numpy as np

RES = 0.1
BIG = 9.0
R_INFL = 1.0          # obstacles only influence nodes this far away (max hw + clr + margin)
MARG = 0.022          # covers the sampling dip between grid nodes (<0.01 at 0.1 mm) and polygon approximation
F, B = 0, 1
LAYER_NAMES = ('F.Cu', 'B.Cu')


class Grid:
    def __init__(self, bw, bh):
        self.nx = int(round(bw / RES)) + 1
        self.ny = int(round(bh / RES)) + 1
        self.bw, self.bh = bw, bh

    def idx(self, u, v):
        return int(round(v / RES)) * self.nx + int(round(u / RES))

    def uv(self, k):
        j, i = divmod(k, self.nx)
        return i * RES, j * RES

    def window(self, u0, v0, u1, v1):
        i0 = max(0, int(np.floor(u0 / RES)))
        i1 = min(self.nx - 1, int(np.ceil(u1 / RES)))
        j0 = max(0, int(np.floor(v0 / RES)))
        j1 = min(self.ny - 1, int(np.ceil(v1 / RES)))
        if i1 < i0 or j1 < j0:
            return None
        X, Y = np.meshgrid(np.arange(i0, i1 + 1) * RES, np.arange(j0, j1 + 1) * RES)
        return (slice(j0, j1 + 1), slice(i0, i1 + 1)), X, Y


# ------------------------------------------------------------------ shapes
def seg_dist(X, Y, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return np.hypot(X - ax, Y - ay)
    t = np.clip(((X - ax) * dx + (Y - ay) * dy) / L2, 0.0, 1.0)
    return np.hypot(X - (ax + t * dx), Y - (ay + t * dy))


def poly_dist(X, Y, pts):
    """Distance to a closed polygon, 0 inside."""
    d = np.full(X.shape, BIG)
    inside = np.zeros(X.shape, bool)
    n = len(pts)
    for k in range(n):
        ax, ay = pts[k]
        bx, by = pts[(k + 1) % n]
        d = np.minimum(d, seg_dist(X, Y, ax, ay, bx, by))
        cond = (ay > Y) != (by > Y)
        with np.errstate(divide='ignore', invalid='ignore'):
            xint = ax + (Y - ay) * (bx - ax) / np.where(by == ay, 1e-12, by - ay)
        inside ^= cond & (X < xint)
    d[inside] = 0.0
    return d


class Shape:
    def bbox(self):
        raise NotImplementedError

    def dist(self, X, Y):
        raise NotImplementedError


class Seg(Shape):
    def __init__(self, a, b, hw):
        self.a, self.b, self.hw = tuple(a), tuple(b), hw

    def bbox(self):
        return (min(self.a[0], self.b[0]) - self.hw, min(self.a[1], self.b[1]) - self.hw,
                max(self.a[0], self.b[0]) + self.hw, max(self.a[1], self.b[1]) + self.hw)

    def dist(self, X, Y):
        return np.maximum(seg_dist(X, Y, *self.a, *self.b) - self.hw, 0.0)


class Circle(Shape):
    def __init__(self, c, r):
        self.c, self.r = tuple(c), r

    def bbox(self):
        return (self.c[0] - self.r, self.c[1] - self.r, self.c[0] + self.r, self.c[1] + self.r)

    def dist(self, X, Y):
        return np.maximum(np.hypot(X - self.c[0], Y - self.c[1]) - self.r, 0.0)


class Poly(Shape):
    def __init__(self, pts):
        self.pts = [tuple(p) for p in pts]

    def bbox(self):
        us = [p[0] for p in self.pts]
        vs = [p[1] for p in self.pts]
        return min(us), min(vs), max(us), max(vs)

    def dist(self, X, Y):
        return poly_dist(X, Y, self.pts)

    def contains(self, u, v):
        X = np.array([[u]]); Y = np.array([[v]])
        return poly_dist(X, Y, self.pts)[0, 0] == 0.0


# ------------------------------------------------------------------ fields
class Fields:
    """Min-distance fields for one layer."""

    def __init__(self, grid):
        sh = (grid.ny, grid.nx)
        self.g = grid
        self.d1 = np.full(sh, BIG, np.float32); self.n1 = np.zeros(sh, np.int32); self.d2 = np.full(sh, BIG, np.float32)
        self.e1 = np.full(sh, BIG, np.float32); self.m1 = np.zeros(sh, np.int32); self.e2 = np.full(sh, BIG, np.float32)

    def copy(self):
        o = Fields.__new__(Fields)
        o.g = self.g
        for k in ('d1', 'n1', 'd2', 'e1', 'm1', 'e2'):
            setattr(o, k, getattr(self, k).copy())
        return o

    @staticmethod
    def _upd(A1, N1, A2, D, M):
        same = N1 == M
        closer = (~same) & (D < A1)
        newA2 = np.where(same, A2, np.where(closer, A1, np.minimum(A2, D)))
        newA1 = np.where(same | closer, np.minimum(A1, D), A1)
        N1[closer] = M
        A1[...] = newA1
        A2[...] = newA2

    def add(self, shape, net, clr):
        u0, v0, u1, v1 = shape.bbox()
        w = self.g.window(u0 - R_INFL, v0 - R_INFL, u1 + R_INFL, v1 + R_INFL)
        if w is None:
            return
        sl, X, Y = w
        D = np.minimum(shape.dist(X, Y), BIG).astype(np.float32)
        self._upd(self.d1[sl], self.n1[sl], self.d2[sl], D, net)
        self._upd(self.e1[sl], self.m1[sl], self.e2[sl], D - np.float32(clr), net)

    def ok(self, net, hw, clr, sl=None):
        """Bool mask: a track/via of `net` with half-width hw and clearance clr may be centred here."""
        if sl is None:
            sl = (slice(None), slice(None))
        d = np.where(self.n1[sl] == net, self.d2[sl], self.d1[sl])
        e = np.where(self.m1[sl] == net, self.e2[sl], self.e1[sl])
        return (d >= hw + clr + MARG) & (e >= hw + MARG)

    def margin(self, net, hw, clr):
        """How much room is left beyond the required clearance (mm, may be negative)."""
        d = np.where(self.n1 == net, self.d2, self.d1)
        e = np.where(self.m1 == net, self.e2, self.e1)
        return np.minimum(d - hw - clr, e - hw)


class Scalar:
    """Plain min-distance field (e.g. to drill holes or to SMD pads), net-agnostic."""

    def __init__(self, grid):
        self.g = grid
        self.d = np.full((grid.ny, grid.nx), BIG, np.float32)

    def copy(self):
        o = Scalar.__new__(Scalar)
        o.g, o.d = self.g, self.d.copy()
        return o

    def add(self, shape):
        u0, v0, u1, v1 = shape.bbox()
        w = self.g.window(u0 - R_INFL, v0 - R_INFL, u1 + R_INFL, v1 + R_INFL)
        if w is None:
            return
        sl, X, Y = w
        self.d[sl] = np.minimum(self.d[sl], shape.dist(X, Y).astype(np.float32))
