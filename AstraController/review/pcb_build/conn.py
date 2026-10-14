"""Connectivity (union-find) of pads, tracks, vias and plane islands, per net."""
import math
from field import F, B, LAYER_NAMES

TOL = 0.005


def pip(pt, poly):
    x, y = pt
    inside = False
    n = len(poly)
    for k in range(n):
        ax, ay = poly[k]
        bx, by = poly[(k + 1) % n]
        if (ay > y) != (by > y):
            if x < ax + (y - ay) * (bx - ax) / (by - ay):
                inside = not inside
    return inside


def seg_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 < 1e-12 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)


def seg_seg_dist(a, b, c, d):
    return min(seg_dist(a, c, d), seg_dist(b, c, d), seg_dist(c, a, b), seg_dist(d, a, b))


class UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            self.p[b] = a


def pad_hit(pad, pt, layer, r=0.0):
    if layer not in pad.layers:
        return False
    x0, y0, x1, y1 = pad.bb
    if pt[0] < x0 - r or pt[0] > x1 + r or pt[1] < y0 - r or pt[1] > y1 + r:
        return False
    for poly in pad.polys:
        if pip(pt, poly):
            return True
        if r > 0:
            n = len(poly)
            if min(seg_dist(pt, poly[k], poly[(k + 1) % n]) for k in range(n)) <= r:
                return True
    return False


def seg_hits_pad(pad, t, layer):
    """Track segment overlaps the pad (end inside, or passes through it)."""
    if layer not in pad.layers:
        return False
    (ax, ay), (bx, by) = t['a'], t['b']
    x0, y0, x1, y1 = pad.bb
    if max(ax, bx) < x0 or min(ax, bx) > x1 or max(ay, by) < y0 or min(ay, by) > y1:
        return False
    n = max(1, int(math.hypot(bx - ax, by - ay) / 0.04))
    for s in range(n + 1):
        pt = (ax + (bx - ax) * s / n, ay + (by - ay) * s / n)
        if x0 - TOL <= pt[0] <= x1 + TOL and y0 - TOL <= pt[1] <= y1 + TOL and pad_hit(pad, pt, layer, 0.01):
            return True
    return False


def components(ctx, net, plane_ok=None):
    """Return list of components; each is dict(pads=[], tracks=[], vias=[], plane=bool).

    plane_ok(u, v) -> True if a via / THT pad at that point connects to this net's plane.
    """
    pads = ctx.pads_of(net)
    tracks = [t for t in ctx.tracks if t['net'] == net]
    vias = [v for v in ctx.vias if v['net'] == net]
    items = [('pad', p) for p in pads] + [('trk', t) for t in tracks] + [('via', v) for v in vias]
    n = len(items)
    PLANE = n
    uf = UF(n + 1)
    first = {}
    for k, p in enumerate(pads):          # multi-part pads (EP squares + via pads) are one pad
        if p.name in first:
            uf.union(first[p.name], k)
        else:
            first[p.name] = k
    tl = [LAYER_NAMES.index(t['layer']) for t in tracks]
    np_, nt = len(pads), len(tracks)
    for i, t in enumerate(tracks):
        ti = np_ + i
        L = tl[i]
        hw = t['w'] / 2
        for k, p in enumerate(pads):      # track end inside the pad, or pad centre inside the track
            if (pad_hit(p, t['a'], L, 0.01) or pad_hit(p, t['b'], L, 0.01)
                    or (L in p.layers and seg_dist(p.pos, t['a'], t['b']) <= hw + TOL)):
                uf.union(ti, k)
        for k, v in enumerate(vias):        # track end inside the via, or via centre inside the track
            if (math.dist(t['a'], v['p']) <= v['d'] / 2 + TOL or math.dist(t['b'], v['p']) <= v['d'] / 2 + TOL
                    or seg_dist(v['p'], t['a'], t['b']) <= hw + TOL):
                uf.union(ti, np_ + nt + k)
        for j in range(i + 1, len(tracks)):
            if tl[j] != L:
                continue
            o = tracks[j]
            m = max(t['w'], o['w']) / 2 + TOL
            if (max(t['a'][0], t['b'][0]) + m < min(o['a'][0], o['b'][0]) or max(o['a'][0], o['b'][0]) + m < min(t['a'][0], t['b'][0]) or
                    max(t['a'][1], t['b'][1]) + m < min(o['a'][1], o['b'][1]) or max(o['a'][1], o['b'][1]) + m < min(t['a'][1], t['b'][1])):
                continue
            if (seg_dist(t['a'], o['a'], o['b']) <= o['w'] / 2 + TOL or seg_dist(t['b'], o['a'], o['b']) <= o['w'] / 2 + TOL
                    or seg_dist(o['a'], t['a'], t['b']) <= hw + TOL or seg_dist(o['b'], t['a'], t['b']) <= hw + TOL):
                uf.union(ti, np_ + j)
    for k, v in enumerate(vias):
        vi = np_ + nt + k
        for m, p in enumerate(pads):
            if pad_hit(p, v['p'], F, 0.01) or (p.tht and pad_hit(p, v['p'], B, 0.01)):
                uf.union(vi, m)
        if plane_ok and plane_ok(*v['p']):
            uf.union(vi, PLANE)
    if plane_ok:
        for m, p in enumerate(pads):
            if p.tht and plane_ok(*p.pos):
                uf.union(m, PLANE)
    groups = {}
    for idx in range(n):
        groups.setdefault(uf.find(idx), []).append(idx)
    proot = uf.find(PLANE) if plane_ok else None
    out = []
    for root, members in groups.items():
        c = dict(pads=[], tracks=[], vias=[], plane=(root == proot))
        for idx in members:
            kind, obj = items[idx]
            c[{'pad': 'pads', 'trk': 'tracks', 'via': 'vias'}[kind]].append(obj)
        out.append(c)
    # pads-only components are what still needs connecting; plane component first
    out.sort(key=lambda c: (not c['plane'], -len(c['pads'])))
    return out
