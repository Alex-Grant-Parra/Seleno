"""Routing context: pads + edge + holes as obstacles, plus the tracks/vias placed so far."""
import json, math
import numpy as np
import rules
from field import Grid, Fields, Scalar, Seg, Circle, Poly, poly_dist, seg_dist, F, B, LAYER_NAMES, RES

EDGE_CLR = rules.MIN['edge']
HOLE_KEEPOUT = 1.0       # extra copper keep-out around mounting holes (screw head)


class Pad:
    def __init__(self, ref, d, netid):
        self.ref, self.num, self.net = ref, d['num'], d['net']
        self.name = f'{ref}.{d["num"]}'
        self.netid = netid
        self.pos = tuple(d['pos'])
        self.tht, self.npth, self.drill = d['tht'], d['npth'], d['drill']
        self.polys = [p for p in d['poly'] if len(p) > 2]
        self.layers = (F, B) if self.tht else tuple(L for L, k in ((F, 'F'), (B, 'B')) if k in d['layers'])
        pts = [q for p in self.polys for q in p] or [self.pos]
        self.bb = (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))
        self.minw = min(self.bb[2] - self.bb[0], self.bb[3] - self.bb[1])


class Ctx:
    def __init__(self, geom_path):
        G = json.load(open(geom_path))
        self.bw, self.bh = G['board']
        self.grid = Grid(self.bw, self.bh)
        self.fps = {f['ref']: f for f in G['footprints']}
        names = sorted({p['net'] for f in G['footprints'] for p in f['pads'] if p['net']})
        self.netid = {n: i + 1 for i, n in enumerate(names)}
        self.netname = {i: n for n, i in self.netid.items()}
        self.netname[0] = ''
        self.pads = []
        for f in G['footprints']:
            seen = set()
            for d in f['pads']:
                key = (d['num'], round(d['pos'][0], 3), round(d['pos'][1], 3))
                if key in seen:
                    continue
                seen.add(key)
                self.pads.append(Pad(f['ref'], d, self.netid.get(d['net'], 0)))
        self.padmap = {}
        for p in self.pads:
            self.padmap.setdefault(p.name, p)
        self.fields = [Fields(self.grid), Fields(self.grid)]
        self.holes = Scalar(self.grid)     # distance to any drill hole edge
        self.smd = Scalar(self.grid)       # distance to any SMD pad copper
        self.tracks, self.vias = [], []
        self._base()

    # ---------------------------------------------------------------- obstacles
    def clr(self, netid):
        return rules.clearance(self.netname[netid]) if netid else 0.0

    def _base(self):
        import planes
        bw, bh = self.bw, self.bh
        for k in planes.KEEPOUTS:
            self.fields[LAYER_NAMES.index(k['layer'])].add(Poly(k['pts']), 0, 0.0)
        for a, b in (((0, 0), (bw, 0)), ((bw, 0), (bw, bh)), ((bw, bh), (0, bh)), ((0, bh), (0, 0))):
            for L in (F, B):
                self.fields[L].add(Seg(a, b, 0.0), 0, EDGE_CLR)
        for p in self.pads:
            if p.npth:
                r = p.drill / 2
                keep = r + (HOLE_KEEPOUT if p.ref.startswith('H') else 0.0)
                for L in (F, B):
                    self.fields[L].add(Circle(p.pos, keep), 0, rules.MIN['hole_clr'])
                self.holes.add(Circle(p.pos, r))
                continue
            c = self.clr(p.netid)
            for poly in p.polys:
                for L in p.layers:
                    self.fields[L].add(Poly(poly), p.netid, c)
                if not p.tht:
                    self.smd.add(Poly(poly))       # no vias inside any SMD pad (either side)
            if p.tht:
                self.holes.add(Circle(p.pos, p.drill / 2))

    def snapshot(self):
        return ([f.copy() for f in self.fields], self.holes.copy(), self.smd.copy(), list(self.tracks), list(self.vias))

    def restore(self, snap):
        f, h, s, t, v = snap
        self.fields = [x.copy() for x in f]
        self.holes, self.smd = h.copy(), s.copy()
        self.tracks, self.vias = list(t), list(v)

    def add_track(self, net, layer, a, b, w, src='hand'):
        nid = self.netid[net] if isinstance(net, str) else net
        t = dict(net=self.netname[nid], layer=LAYER_NAMES[layer], a=tuple(map(float, a)), b=tuple(map(float, b)), w=w, src=src)
        self.tracks.append(t)
        self.fields[layer].add(Seg(a, b, w / 2), nid, self.clr(nid))
        return t

    def add_via(self, net, p, d=0.5, drill=0.3, src='hand'):
        nid = self.netid[net] if isinstance(net, str) else net
        v = dict(net=self.netname[nid], p=tuple(map(float, p)), d=d, drill=drill, src=src)
        self.vias.append(v)
        for L in (F, B):
            self.fields[L].add(Circle(p, d / 2), nid, self.clr(nid))
        self.holes.add(Circle(p, drill / 2))
        return v

    def polyline(self, net, layer, pts, w, src='hand'):
        for a, b in zip(pts, pts[1:]):
            self.add_track(net, layer, a, b, w, src)

    def rebuild(self, keep):
        """Rebuild fields from pads + the given tracks/vias (used after rip-up)."""
        tracks, vias = [t for t in self.tracks if keep(t)], [v for v in self.vias if keep(v)]
        self.fields = [Fields(self.grid), Fields(self.grid)]
        self.holes, self.smd = Scalar(self.grid), Scalar(self.grid)
        self.tracks, self.vias = [], []
        self._base()
        for t in tracks:
            self.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], t['src'])
        for v in vias:
            self.add_via(v['net'], v['p'], v['d'], v['drill'], v['src'])

    # ---------------------------------------------------------------- queries
    def pad(self, name):
        return self.padmap[name]

    def pads_of(self, net):
        nid = self.netid[net]
        return [p for p in self.pads if p.netid == nid]

    def mask_ok(self, layer, net, hw):
        nid = self.netid[net] if isinstance(net, str) else net
        return self.fields[layer].ok(nid, hw, self.clr(nid))

    def via_ok(self, net, d, drill, allow_smd=False):
        nid = self.netid[net] if isinstance(net, str) else net
        m = self.mask_ok(F, nid, d / 2) & self.mask_ok(B, nid, d / 2)
        m &= self.holes.d >= drill / 2 + rules.MIN['hole_hole'] + 0.01
        if not allow_smd:
            m &= self.smd.d >= d / 2 + 0.02
        return m

    def region_mask(self, pts, inset=0.0):
        """Bool mask of nodes inside polygon pts at least `inset` from its boundary."""
        g = self.grid
        u0 = min(p[0] for p in pts); u1 = max(p[0] for p in pts)
        v0 = min(p[1] for p in pts); v1 = max(p[1] for p in pts)
        m = np.zeros((g.ny, g.nx), bool)
        w = g.window(u0, v0, u1, v1)
        sl, X, Y = w
        inside = poly_dist(X, Y, pts) == 0.0
        if inset > 0:
            de = np.full(X.shape, 99.0)
            n = len(pts)
            for k in range(n):
                de = np.minimum(de, seg_dist(X, Y, *pts[k], *pts[(k + 1) % n]))
            inside &= de >= inset
        m[sl] = inside
        return m

    def pad_nodes(self, p, layer, ok=None, shrink=0.02):
        """Flat node indices inside pad p (optionally restricted to a legality mask)."""
        g = self.grid
        out = []
        for poly in p.polys:
            u0, v0, u1, v1 = Poly(poly).bbox()
            sl, X, Y = g.window(u0, v0, u1, v1)
            inside = poly_dist(X, Y, poly) == 0.0
            if shrink > 0:
                n = len(poly)
                de = np.full(X.shape, 99.0)
                for k in range(n):
                    de = np.minimum(de, seg_dist(X, Y, *poly[k], *poly[(k + 1) % n]))
                inside &= de >= shrink
            if ok is not None:
                inside &= ok[sl]
            jj, ii = np.nonzero(inside)
            out += list((jj + sl[0].start) * g.nx + (ii + sl[1].start))
        return out

    def dump(self, path, zones=()):
        import planes
        json.dump(dict(tracks=self.tracks, vias=self.vias, zones=list(zones), keepouts=planes.KEEPOUTS), open(path, 'w'), indent=0)
