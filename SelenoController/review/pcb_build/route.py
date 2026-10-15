"""Grid A* router for SELENO v0.8 (F.Cu + B.Cu, through vias, 0.1 mm grid). Time-boxed.

python.exe route.py <geom.json> <routes.json>
Order: hand routes -> exposed-pad vias -> plane fanout -> signal nets (with rip-up and retry) -> zones.
"""
import sys, time, math, heapq, json
import numpy as np
import rules, planes, hand
from ctx import Ctx
from conn import components, pad_hit
from field import F, B, RES, LAYER_NAMES, Poly, MARG

INF = float('inf')
SQ2 = math.sqrt(2.0)
DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]
TURN = {0: 0.0, 1: 1.5, 7: 1.5, 2: 5.0, 6: 5.0}       # 135 / 180 degree turns are not allowed
VIA_COST = 14.0      # grid units (0.1 mm): a via costs about 1.4 mm of track
TIGHT_COST = 0.6


def step_costs():
    out = []
    for L in (F, B):
        row = []
        for di, dj in DIRS:
            base = SQ2 if di and dj else 1.0
            if di and dj:
                f = 1.15
            elif (L == F and di) or (L == B and dj):
                f = 1.4           # F.Cu (pads, escapes) prefers vertical, B.Cu horizontal
            else:
                f = 1.0
            row.append(base * f)
        out.append(row)
    return out


STEP = step_costs()


class Router:
    def __init__(self, ctx, log=print):
        self.c = ctx
        self.g = ctx.grid
        self.nx = ctx.grid.nx
        self.DELTA = [dj * self.nx + di for di, dj in DIRS]
        self.log = log
        self.island_masks = {}
        self.progress = None
        n = ctx.grid.nx * ctx.grid.ny
        self.hist = [bytearray(n), bytearray(n)]     # PathFinder-style history cost per node

    # ------------------------------------------------------------ masks
    def masks(self, net, w, via):
        c = self.c
        hw = w / 2
        ok = [c.mask_ok(L, net, hw) for L in (F, B)]
        tight = [~c.fields[L].ok(c.netid[net], hw, c.clr(c.netid[net]) + 0.1) for L in (F, B)]
        vok = c.via_ok(net, via[0], via[1])
        return ([m.astype(np.uint8).tobytes() for m in ok], [m.astype(np.uint8).tobytes() for m in tight],
                vok.astype(np.uint8).tobytes(), ok, vok)

    def island(self, net, r):
        key = (net, round(r, 3))
        if key in self.island_masks:
            return self.island_masks[key]
        c = self.c
        if net == 'GND':
            m = c.region_mask(planes.FULL, 0.4 + r)
        elif net == '+3.3V':
            m = c.region_mask(planes.FULL, 0.4 + r)
            for isl in planes.ISLANDS.values():
                m &= ~self._dilated(isl, 0.25 + r)
        else:
            m = c.region_mask(planes.ISLANDS[net], r + 0.1)
        self.island_masks[key] = m
        return m

    def _dilated(self, pts, d):
        """Nodes inside the polygon or within d of it."""
        from field import poly_dist
        g = self.g
        X, Y = np.meshgrid(np.arange(g.nx) * RES, np.arange(g.ny) * RES)
        return poly_dist(X, Y, pts) <= d

    def plane_ok_fn(self, net):
        if net not in rules.PLANE_NETS:
            return None
        m = self.island(net, 0.2)
        g = self.g

        def f(u, v):
            i, j = int(round(u / RES)), int(round(v / RES))
            return bool(m[j, i])
        return f

    # ------------------------------------------------------------ node sets
    def comp_nodes(self, comp, okm, layers=(F, B)):
        """(node, layer) pairs a path may start from / end on for this component."""
        c, g = self.c, self.g
        out = set()
        for p in comp['pads']:
            for L in p.layers:
                if L in layers:
                    for k in c.pad_nodes(p, L, okm[L]):
                        out.add((k, L))
        for t in comp['tracks']:
            L = LAYER_NAMES.index(t['layer'])
            if L not in layers:
                continue
            (a1, b1), (a2, b2) = t['a'], t['b']
            n = max(1, int(math.hypot(a2 - a1, b2 - b1) / (RES / 2)))
            for s in range(n + 1):
                u, v = a1 + (a2 - a1) * s / n, b1 + (b2 - b1) * s / n
                i, j = int(round(u / RES)), int(round(v / RES))
                if okm[L][j, i]:
                    out.add((j * g.nx + i, L))
        for v in comp['vias']:
            i0, j0 = int(round(v['p'][0] / RES)), int(round(v['p'][1] / RES))
            rr = int(v['d'] / 2 / RES)
            for j in range(j0 - rr, j0 + rr + 1):
                for i in range(i0 - rr, i0 + rr + 1):
                    if math.hypot(i - i0, j - j0) * RES <= v['d'] / 2 - 0.05:
                        for L in layers:
                            if okm[L][j, i]:
                                out.add((j * g.nx + i, L))
        return out

    # ------------------------------------------------------------ search
    def astar(self, ok, tight, vok, sources, targets, goal=None, limit=150000, maxcost=INF, allow_via=True,
              hw_=1.5, tboxes=None, deadline=None):
        """sources: iterable of (node, layer); targets: set of node*2+layer; goal: optional (bytes mask, layer)
        accepted as a target on that layer. Returns list of (node, layer) or None.
        State is node*2+layer; the turn cost uses the direction the node was reached from."""
        nx = self.nx
        DELTA = self.DELTA
        if targets and goal is None:
            if not tboxes:
                ii = [(t >> 1) % nx for t in targets]
                jj = [(t >> 1) // nx for t in targets]
                tboxes = [(min(ii), max(ii), min(jj), max(jj))]
            use_h = True
        else:
            use_h = False
            tboxes = []
        boxes = tboxes
        gd, par, pdir = {}, {}, {}
        heap = []
        for node, L in sources:
            st = (node << 1) | L
            gd[st] = 0.0
            pdir[st] = 8
            heap.append((0.0, 0.0, st))
        heapq.heapify(heap)
        hist = self.hist
        exp = 0
        gmask, glayer = goal if goal else (None, None)
        tnow = time.time
        while heap:
            f, gc, st = heapq.heappop(heap)
            if gc > gd.get(st, INF):
                continue
            node, L = st >> 1, st & 1
            if st in targets or (gmask is not None and L == glayer and gmask[node]):
                path = [(node, L)]
                while st in par:
                    st = par[st]
                    path.append((st >> 1, st & 1))
                path.reverse()
                self.last_exp = exp
                return path
            exp += 1
            if exp > limit or gc > maxcost or (deadline and not exp & 4095 and tnow() > deadline):
                self.last_exp = exp
                return None
            okL = ok[L]; tL = tight[L]; hL = hist[L]; sc = STEP[L]
            d = pdir[st]
            for k in range(8):
                if d != 8:
                    tc = TURN.get((k - d) & 7)
                    if tc is None:
                        continue
                else:
                    tc = 0.0
                nn = node + DELTA[k]
                if not okL[nn]:
                    continue
                c2 = gc + sc[k] + tc + hL[nn]
                if tL[nn]:
                    c2 += TIGHT_COST
                ns = (nn << 1) | L
                if c2 < gd.get(ns, INF):
                    gd[ns] = c2
                    par[ns] = st
                    pdir[ns] = k
                    if use_h:
                        j, i = divmod(nn, nx)
                        hb = INF
                        for bi0, bi1, bj0, bj1 in boxes:
                            dx = bi0 - i if i < bi0 else (i - bi1 if i > bi1 else 0)
                            dy = bj0 - j if j < bj0 else (j - bj1 if j > bj1 else 0)
                            hh = (dx + dy) - 0.5858 * (dx if dx < dy else dy)
                            if hh < hb:
                                hb = hh
                        heapq.heappush(heap, (c2 + hw_ * hb, c2, ns))
                    else:
                        heapq.heappush(heap, (c2, c2, ns))
            if allow_via and vok[node]:
                ns = st ^ 1
                if ok[1 - L][node]:
                    c2 = gc + VIA_COST
                    if c2 < gd.get(ns, INF):
                        gd[ns] = c2
                        par[ns] = st
                        pdir[ns] = 8
                        heapq.heappush(heap, (c2 + f - gc, c2, ns))
        self.last_exp = exp
        return None

    # ------------------------------------------------------------ coarse corridor
    def span(self, src, tg):
        nx = self.nx
        si = [n % nx for n, _ in src]; sj = [n // nx for n, _ in src]
        ti = [(t >> 1) % nx for t in tg]; tj = [(t >> 1) // nx for t in tg]
        dx = max(0, min(ti) - max(si), min(si) - max(ti))
        dy = max(0, min(tj) - max(sj), min(sj) - max(tj))
        return dx + dy

    def corridor(self, okm, vok, src, tg, rad, K=5):
        """Coarse A* on KxK blocks; returns a fine bool mask of blocks within `rad` of the coarse path."""
        g = self.g
        nx, ny = g.nx, g.ny
        cx, cy = (nx + K - 1) // K, (ny + K - 1) // K

        def frac(m):
            p = np.zeros((cy * K, cx * K), np.float32)
            p[:ny, :nx] = m
            return p.reshape(cy, K, cx, K).mean(axis=(1, 3))
        fr = [frac(okm[L]) for L in (F, B)]
        cost = [np.where(f > 0, 1.0 + 2.0 * (1.0 - f), 0.0).ravel().tolist() for f in fr]
        cvia = frac(vok).ravel() > 0
        cblk = lambda n: (n // nx // K) * cx + (n % nx) // K
        srcb = {(cblk(n), L) for n, L in src}
        tgb = {(cblk(t >> 1) << 1) | (t & 1) for t in tg}
        ti = [(b >> 1) % cx for b in tgb]; tj = [(b >> 1) // cx for b in tgb]
        bi0, bi1, bj0, bj1 = min(ti), max(ti), min(tj), max(tj)

        def h(b):
            j, i = divmod(b, cx)
            dx = bi0 - i if i < bi0 else (i - bi1 if i > bi1 else 0)
            dy = bj0 - j if j < bj0 else (j - bj1 if j > bj1 else 0)
            return 1.2 * ((dx + dy) - 0.5858 * min(dx, dy))
        dirs = [(di, dj, (SQ2 if di and dj else 1.0)) for di, dj in DIRS]
        gd, par, heap = {}, {}, []
        for b, L in srcb:
            st = (b << 1) | L
            gd[st] = 0.0
            heap.append((h(b), 0.0, st))
        heapq.heapify(heap)
        end = None
        while heap:
            f, gc, st = heapq.heappop(heap)
            if gc > gd.get(st, INF):
                continue
            if st in tgb:
                end = st
                break
            b, L = st >> 1, st & 1
            j, i = divmod(b, cx)
            for k, (di, dj, base) in enumerate(dirs):
                ii, jj = i + di, j + dj
                if not (0 <= ii < cx and 0 <= jj < cy):
                    continue
                nb = jj * cx + ii
                cc = cost[L][nb]
                if not cc:
                    continue
                c2 = gc + base * STEP[L][k] / (SQ2 if di and dj else 1.0) * cc
                ns = (nb << 1) | L
                if c2 < gd.get(ns, INF):
                    gd[ns] = c2; par[ns] = st
                    heapq.heappush(heap, (c2 + h(nb), c2, ns))
            if cvia[b]:
                ns = st ^ 1
                if cost[1 - L][b]:
                    c2 = gc + 3.0
                    if c2 < gd.get(ns, INF):
                        gd[ns] = c2; par[ns] = st
                        heapq.heappush(heap, (c2 + h(b), c2, ns))
        if end is None:
            return None
        on = np.zeros((cy, cx), bool)
        st = end
        while True:
            b = st >> 1
            on[b // cx, b % cx] = True
            if st not in par:
                break
            st = par[st]
        for b, L in srcb:
            on[b // cx, b % cx] = True
        for t in tgb:
            on[(t >> 1) // cx, (t >> 1) % cx] = True
        # dilate by rad blocks
        d = on.copy()
        for dj in range(-rad, rad + 1):
            for di in range(-rad, rad + 1):
                d |= np.roll(np.roll(on, dj, 0), di, 1)
        fine = np.repeat(np.repeat(d, K, 0), K, 1)[:ny, :nx]
        return fine

    # ------------------------------------------------------------ commit
    def commit(self, net, path, w, via, src, end_via=False):
        g = self.g
        pts = []          # list of (layer, [uv...])
        vias = []
        cur = None
        for node, L in path:
            uv = g.uv(node)
            if cur is None or cur[0] != L:
                if cur is not None:
                    vias.append(uv)
                cur = (L, [uv])
                pts.append(cur)
            else:
                cur[1].append(uv)
        added = []
        for L, run in pts:
            simp = simplify(run)
            for a, b in zip(simp, simp[1:]):
                added.append(self.c.add_track(net, L, a, b, w, src))
        for p in vias:
            self.c.add_via(net, p, via[0], via[1], src)
        if end_via:
            self.c.add_via(net, g.uv(path[-1][0]), via[0], via[1], src)
        return added


def simplify(run):
    out = [run[0]]
    for p in run[1:]:
        if p == out[-1]:
            continue
        if len(out) >= 2:
            (x0, y0), (x1, y1) = out[-2], out[-1]
            if abs((x1 - x0) * (p[1] - y1) - (y1 - y0) * (p[0] - x1)) < 1e-9:
                out[-1] = p
                continue
        out.append(p)
    return out


# ------------------------------------------------------------------ plane fanout
def fanout(R, log):
    c = R.c
    todo = []
    for net in rules.PLANE_NETS:
        comps = components(c, net, R.plane_ok_fn(net))
        for comp in comps:
            if not comp['plane'] and comp['pads']:
                todo.append((net, comp))
    # tight pads first
    todo.sort(key=lambda x: min(p.minw for p in x[1]['pads']))
    fails = []
    for net, comp in todo:
        comps = components(c, net, R.plane_ok_fn(net))
        comp = next((k for k in comps if any(p in k['pads'] for p in comp['pads'])), None)
        if comp is None or comp['plane']:
            continue
        cls = rules.CLASSES[rules.netclass(net)]
        minw = min(p.minw for p in comp['pads'])
        done = False
        for w, via, lim, maxc in ((min(cls['track'], 0.4, max(0.2, minw)), (0.5, 0.3), 6000, 40),
                                  (min(0.25, max(0.2, minw)), (0.45, 0.3), 12000, 60),
                                  (0.3, (0.45, 0.3), 120000, INF)):
            ok_b, tight_b, vok_b, okm, vok = R.masks(net, w, via)
            isl = R.island(net, via[0] / 2)
            goal = (vok & isl).astype(np.uint8).tobytes()
            src = R.comp_nodes(comp, okm, layers=(F,))
            if not src:
                continue
            plane_comp = next((k for k in comps if k['plane']), None)
            tg = set()
            if plane_comp is not None:
                for node, L in R.comp_nodes(plane_comp, okm, layers=(F,)):
                    tg.add((node << 1) | L)
            path = R.astar(ok_b, tight_b, vok_b, src, tg, goal=(goal, F), limit=lim, maxcost=maxc, allow_via=maxc == INF)
            if path:
                endnl = (path[-1][0] << 1) | path[-1][1]
                R.commit(net, path, w, via, 'fan', end_via=endnl not in tg)
                done = True
                break
        if not done:   # far from its island: route straight to the nearest plane-connected copper of the net
            plane_comp = next((k for k in comps if k['plane']), None)
            if plane_comp is not None:
                ok_b, tight_b, vok_b, okm, vok = R.masks(net, 0.3, (0.45, 0.3))
                src = R.comp_nodes(comp, okm)
                tgn = R.comp_nodes(plane_comp, okm)
                tg = {(n << 1) | L for n, L in tgn}
                if src and tg:
                    nx = R.nx
                    boxes = [(min(n % nx for n, _ in tgn), max(n % nx for n, _ in tgn),
                              min(n // nx for n, _ in tgn), max(n // nx for n, _ in tgn))]
                    path = R.astar(ok_b, tight_b, vok_b, src, tg, limit=200000, hw_=1.3, tboxes=None,
                                   deadline=time.time() + 4)
                    if path:
                        R.commit(net, path, 0.3, (0.45, 0.3), 'fan')
                        done = True
        if not done:
            fails.append((net, [p.name for p in comp['pads']]))
            log('  fanout FAIL', net, [p.name for p in comp['pads']])
    return fails


# ------------------------------------------------------------------ signal routing
def route_net(R, net, w, via, limit=150000, weights=(1.5, 1.15), budget=5.0):
    """Connect all pieces of `net`. Masks are computed once (own copper never blocks itself)."""
    c = R.c
    deadline = time.time() + budget
    ok_b, tight_b, vok_b, okm, vok = R.masks(net, w, via)
    nx = R.nx
    for _ in range(64):
        comps = components(c, net, None)
        comps = [k for k in comps if k['pads'] or k['tracks'] or k['vias']]
        if len(comps) <= 1:
            return True
        if time.time() > deadline:
            return False
        comps.sort(key=lambda k: -(len(k['pads']) + len(k['tracks'])))
        src = R.comp_nodes(comps[0], okm)
        tg, boxes = set(), []
        for k in comps[1:]:
            kn = R.comp_nodes(k, okm)
            for node, L in kn:
                tg.add((node << 1) | L)
            if kn:
                boxes.append((min(n % nx for n, _ in kn), max(n % nx for n, _ in kn),
                              min(n // nx for n, _ in kn), max(n // nx for n, _ in kn)))
        if not src or not tg:
            return False
        if any(((n << 1) | L) in tg for n, L in src):
            R.log('  overlap but unconnected?', net)
            return False
        path = None
        far = R.span(src, tg) > 60           # > 6 mm apart: plan a corridor on a coarse grid first
        tries = [(2, weights[:1]), (5, weights)] if far else []
        for rad, wts in tries + [(None, weights)]:
            if rad is None:
                o, v = ok_b, vok_b
            else:
                corr = R.corridor(okm, vok, src, tg, rad)
                if corr is None:
                    continue
                o = [(okm[L] & corr).astype(np.uint8).tobytes() for L in (F, B)]
                v = (vok & corr).astype(np.uint8).tobytes()
            for wgt in wts:
                path = R.astar(o, tight_b, v, src, tg, limit=limit, hw_=wgt, tboxes=boxes, deadline=deadline)
                if path or time.time() > deadline:
                    break
            if path or time.time() > deadline:
                break
        if not path:
            return False
        R.commit(net, path, w, via, 'route')
    return False


def signal_nets(c):
    out = []
    for net in c.netid:
        if not net or net.startswith('unconnected') or net in rules.PLANE_NETS:
            continue
        if len(c.pads_of(net)) < 2:
            continue
        out.append(net)
    return out


def net_span(c, net):
    ps = [p.pos for p in c.pads_of(net)]
    return (max(p[0] for p in ps) - min(p[0] for p in ps)) + (max(p[1] for p in ps) - min(p[1] for p in ps))


def try_route(R, net):
    cls = rules.netclass(net)
    w = rules.CLASSES[cls]['track'] if cls in ('Default', 'USB') else 0.3
    t = 2.0 + net_span(R.c, net) / 15.0          # seconds: ~2 s for local nets, ~8 s for board-crossing ones
    return (route_net(R, net, w, (0.5, 0.3), budget=t) or route_net(R, net, w, (0.45, 0.3), budget=t)
            or (w > 0.2 and route_net(R, net, 0.2, (0.45, 0.3), budget=t))
            or route_net(R, net, 0.15, (0.45, 0.3), budget=t))      # last resort: 0.15 mm neck-down


def one_pass(R, order, log):
    failed = []
    for net in order:
        t0 = time.time()
        if try_route(R, net):
            log(f'  routed {net} ({time.time() - t0:.1f}s)')
        else:
            failed.append(net)
            log(f'  FAIL {net} ({time.time() - t0:.1f}s)')
    return failed


def route_signals(R, nets, log, budget=100.0, passes=2):
    """Pass 1 routes long nets first. Each later pass restarts from the fanout state with the nets
    that failed so far routed first (they are the hard ones). Keeps the best pass. Time-boxed."""
    c = R.c
    t_start = time.time()
    base = c.snapshot()
    # tiny local nets (< 5 mm) first: they have no alternatives; then long nets first
    order = sorted(nets, key=lambda n: (net_span(c, n) >= 5.0, -net_span(c, n)))
    short = [n for n in order if net_span(c, n) < 5.0]
    best = None
    hard = []
    for k in range(passes):
        if k:
            restore_with(c, base, [t for t in base[3]], [v for v in base[4]])
            order = [n for n in short if n not in hard] + hard + [n for n in order if n not in hard and n not in short]
        failed = one_pass(R, order, log)
        log(f'pass {k + 1}: {len(order) - len(failed)}/{len(order)} routed, failed {failed} ({time.time() - t_start:.0f}s)')
        if best is None or len(failed) < len(best[0]):
            best = (failed, c.snapshot())
        if not failed or time.time() - t_start > budget:
            break
        hard = hard + [n for n in failed if n not in hard]
    failed, snap = best
    c.restore(snap)
    if R.progress:
        c.dump(R.progress, planes.zones())
    return failed


def restore_with(c, base, tracks, vias):
    fields, holes, smd, bt, bv = base
    c.fields = [f.copy() for f in fields]
    c.holes, c.smd = holes.copy(), smd.copy()
    c.tracks, c.vias = list(bt), list(bv)
    have_t = {id(t) for t in bt}
    have_v = {id(v) for v in bv}
    for t in tracks:
        if id(t) not in have_t:
            c.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], t['src'])
    for v in vias:
        if id(v) not in have_v:
            c.add_via(v['net'], v['p'], v['d'], v['drill'], v['src'])


def blockers(R, net, base):
    """Route `net` on the base board (no other router tracks) and report routed nets in its way."""
    c = R.c
    cur = c.snapshot()
    routed = [t for t in c.tracks if t['src'] == 'route' and t['net'] != net]
    routed_v = [v for v in c.vias if v['src'] == 'route' and v['net'] != net]
    restore_with(c, base, [t for t in c.tracks if t['src'] != 'route' or t['net'] == net],
                 [v for v in c.vias if v['src'] != 'route' or v['net'] == net])
    n0 = len(c.tracks)
    w = 0.2
    okr = route_net(R, net, w, (0.45, 0.3), budget=4.0)
    newt = c.tracks[n0:]
    newv = [v for v in c.vias if v['src'] == 'route' and v['net'] == net]
    c.restore(cur)
    if not okr:
        return [], []
    clr = 0.15 + 0.1 + 0.05
    hit = set()
    from conn import seg_seg_dist
    for t in newt:
        for o in routed:
            if o['layer'] == t['layer'] and seg_seg_dist(t['a'], t['b'], o['a'], o['b']) < (t['w'] + o['w']) / 2 + clr:
                hit.add(o['net'])
        for v in routed_v:
            if math.dist(v['p'], t['a']) < 1e9 and seg_seg_dist(t['a'], t['b'], v['p'], v['p']) < t['w'] / 2 + v['d'] / 2 + clr:
                hit.add(v['net'])
    if not hit:      # nothing directly in the way: free up whatever runs close to the clean path
        for t in newt:
            for o in routed:
                if o['layer'] == t['layer'] and seg_seg_dist(t['a'], t['b'], o['a'], o['b']) < 0.8:
                    hit.add(o['net'])
    return sorted(hit), newt


def bump_history(R, tracks, amount=6, radius=0.3):
    """Make the cells a failed net wanted more expensive for everyone (negotiated congestion)."""
    g = R.g
    rr = int(radius / RES)
    for t in tracks:
        L = LAYER_NAMES.index(t['layer'])
        h = R.hist[L]
        (a1, b1), (a2, b2) = t['a'], t['b']
        n = max(1, int(math.hypot(a2 - a1, b2 - b1) / RES))
        seen = set()
        for s in range(n + 1):
            i0 = int(round((a1 + (a2 - a1) * s / n) / RES))
            j0 = int(round((b1 + (b2 - b1) * s / n) / RES))
            for dj in range(-rr, rr + 1):
                for di in range(-rr, rr + 1):
                    k = (j0 + dj) * g.nx + i0 + di
                    if k not in seen and 0 <= k < len(h):
                        seen.add(k)
                        h[k] = min(200, h[k] + amount)


def cleanup(c, log=print):
    """Remove duplicate vias, vias connected on < 2 layers, and track ends that touch nothing (repeat)."""
    import math as _m
    from conn import pad_hit
    removed_v = removed_t = 0
    seen = {}
    for v in list(c.vias):
        k = (v['net'], round(v['p'][0], 3), round(v['p'][1], 3))
        if k in seen:
            c.vias.remove(v); removed_v += 1
        else:
            seen[k] = v
    pads_by_net = {}
    for p in c.pads:
        pads_by_net.setdefault(p.net, []).append(p)
    changed = True
    while changed:
        changed = False
        by_net = {}
        for t in c.tracks:
            by_net.setdefault(t['net'], []).append(t)
        vias_by_net = {}
        for v in c.vias:
            vias_by_net.setdefault(v['net'], []).append(v)

        def touches(pt, layer, net, skip):
            L = LAYER_NAMES.index(layer)
            for p in pads_by_net.get(net, []):
                if pad_hit(p, pt, L, 0.01):
                    return True
            for v in vias_by_net.get(net, []):
                if _m.dist(pt, v['p']) <= v['d'] / 2 + 0.005:
                    return True
            for o in by_net.get(net, []):
                if o is skip or o['layer'] != layer:
                    continue
                from conn import seg_dist
                if seg_dist(pt, o['a'], o['b']) <= o['w'] / 2 + 0.005:
                    return True
            return False

        for v in list(c.vias):
            if v['src'] == 'hand' and v['net'] in rules.PLANE_NETS:
                continue
            from conn import seg_dist as _sd
            r = v['d'] / 2 + 0.005
            n_items = 0
            for t in by_net.get(v['net'], []):      # track end inside the via, or via centre on the track
                if _m.dist(t['a'], v['p']) <= r or _m.dist(t['b'], v['p']) <= r or _sd(v['p'], t['a'], t['b']) <= t['w'] / 2 + 0.005:
                    n_items += 1
            for p in pads_by_net.get(v['net'], []):
                if pad_hit(p, v['p'], 0, 0.01) or pad_hit(p, v['p'], 1, 0.01):
                    n_items += 1
            plane = v['net'] in rules.PLANE_NETS
            # a signal via is only removable when it is a dead end (touches at most one item);
            # a plane via needs one item (its stub) because the plane is its other side
            if n_items < 1 or (not plane and n_items < 2):
                c.vias.remove(v); removed_v += 1; changed = True
        for t in list(c.tracks):
            if not (touches(t['a'], t['layer'], t['net'], t) and touches(t['b'], t['layer'], t['net'], t)):
                c.tracks.remove(t); removed_t += 1; changed = True
                by_net[t['net']].remove(t)
    log(f'cleanup: removed {removed_v} vias, {removed_t} tracks')


def ep_vias(c):
    """GND via grid in the exposed pad of the expander U1 (its footprint has none)."""
    for ref in ('U1',):
        ep = max((p for p in c.pads if p.ref == ref and p.net == 'GND'), key=lambda p: p.minw)
        cx, cy = ep.pos
        s = 1.0 if ep.minw > 3.0 else 0.9
        for du in (-s, 0, s):
            for dv in (-s, 0, s):
                c.add_via('GND', (cx + du, cy + dv), 0.45, 0.3, 'hand')


def main(geom, out):
    t0 = time.time()
    c = Ctx(geom)
    hand.add_all(c)
    ep_vias(c)
    print(f'hand routes: {len(c.tracks)} tracks {len(c.vias)} vias ({time.time() - t0:.1f}s)')
    R = Router(c)
    fails = fanout(R, print)
    print(f'fanout done: {len(c.vias)} vias, {len(fails)} failures ({time.time() - t0:.1f}s)')
    c.dump(out.replace('.json', '_fan.json'), planes.zones())
    nets = signal_nets(c)
    R.progress = out.replace('.json', '_part.json')
    failed = route_signals(R, nets, print)
    print(f'signals done, failed {failed} ({time.time() - t0:.1f}s)')
    cleanup(c)
    c.dump(out, planes.zones())


def repair(geom, routes_in, out):
    """Load a routed state (all tracks/vias) and complete whatever is still unconnected."""
    t0 = time.time()
    c = Ctx(geom)
    d = json.load(open(routes_in))
    for t in d['tracks']:
        c.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], t['src'])
    for v in d['vias']:
        c.add_via(v['net'], v['p'], v['d'], v['drill'], v['src'])
    R = Router(c)
    fails = fanout(R, print)
    print(f'repair fanout: {len(fails)} failures ({time.time() - t0:.1f}s)')
    nets = [n for n in signal_nets(c) if len([k for k in components(c, n, None) if k['pads']]) > 1]
    print('repair nets:', nets)
    R.progress = out.replace('.json', '_part.json')
    failed = route_signals(R, nets, print)
    print(f'repair done, failed {failed} ({time.time() - t0:.1f}s)')
    cleanup(c)
    c.dump(out, planes.zones())


def ripup(geom, routes_in, out, budget=120.0):
    """Rip-up and re-route: for each unconnected net, find the routed nets in its way (blockers()), tear them up,
    route the stuck net first, then re-route the torn-up ones. A move is kept only if the number of unconnected
    nets drops; otherwise the board is restored. Time-boxed."""
    t0 = time.time()
    c = Ctx(geom)
    d = json.load(open(routes_in))
    for t in d['tracks']:
        if t['src'] != 'route':
            c.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], t['src'])
    for v in d['vias']:
        if v['src'] != 'route':
            c.add_via(v['net'], v['p'], v['d'], v['drill'], v['src'])
    base = c.snapshot()                       # hand + fanout only
    for t in d['tracks']:
        if t['src'] == 'route':
            c.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], t['src'])
    for v in d['vias']:
        if v['src'] == 'route':
            c.add_via(v['net'], v['p'], v['d'], v['drill'], v['src'])
    R = Router(c)

    def unrouted():
        return [n for n in signal_nets(c) if len([k for k in components(c, n, None) if k['pads'] or k['tracks']]) > 1]

    failed = unrouted()
    print(f'ripup start: {len(failed)} unrouted {failed} ({time.time() - t0:.1f}s)')
    tried = set()
    while failed and time.time() - t0 < budget:
        net = next((n for n in failed if n not in tried), None)
        if net is None:
            break
        tried.add(net)
        hit, clean = blockers(R, net, base)
        if not hit:
            ok = try_route(R, net)
            print(f'  {net}: no blockers, direct {"ok" if ok else "FAIL"}')
        else:
            before = c.snapshot()
            c.rebuild(lambda t: not (t['src'] == 'route' and t['net'] in hit))
            ok = try_route(R, net)
            redo = [n for n in hit if not try_route(R, n)]
            now = unrouted()
            if len(now) < len(failed):
                print(f'  {net}: ripped {hit}, kept ({len(failed)} -> {len(now)} unrouted, lost {redo})')
                tried = {n for n in tried if n not in now}     # earlier nets may be routable now
            else:
                c.restore(before)
                bump_history(R, clean)
                print(f'  {net}: ripped {hit}, no gain (lost {redo}), restored')
        failed = unrouted()
    print(f'ripup done: {len(failed)} unrouted {failed} ({time.time() - t0:.1f}s)')
    cleanup(c)
    c.dump(out, planes.zones())


def clean_only(geom, routes_in, out):
    c = Ctx(geom)
    d = json.load(open(routes_in))
    for t in d['tracks']:
        c.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], t['src'])
    for v in d['vias']:
        c.add_via(v['net'], v['p'], v['d'], v['drill'], v['src'])
    cleanup(c)
    c.dump(out, planes.zones())


if __name__ == '__main__':
    if sys.argv[1] == '--clean':
        clean_only(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == '--repair':
        repair(sys.argv[2], sys.argv[3], sys.argv[4])
    elif sys.argv[1] == '--ripup':
        ripup(sys.argv[2], sys.argv[3], sys.argv[4])
    else:
        main(sys.argv[1], sys.argv[2])
