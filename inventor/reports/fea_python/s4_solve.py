# Linear static FEA of one group, all unit load cases at once.   Usage: s4_solve.py GROUP MAT
# Quadratic tets (P2).  Touching printed parts are tied by stiff penalty springs (bonded contact);
# printed nodes touching the mount / fixed hardware are fixed.  Loads are "remote" loads spread over
# a patch with a rigid-body-consistent traction (like Inventor's remote force).
import numpy as np, sys, os, time, json
import scipy.sparse as sp
from scipy.spatial import cKDTree
from skfem import MeshTet, ElementTetP2, ElementVector, Basis, FacetBasis, LinearForm, asm
from skfem.models.elasticity import linear_elasticity, lame_parameters
from skfem.helpers import dot
import pypardiso
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import GROUPS, HERE, BYTAG, MATERIALS, BOLTS, BOLT_AS, SEAT_ROUT

G, MAT = sys.argv[1], sys.argv[2]
mat = MATERIALS[MAT]; E, nu = mat["E"], mat["nu"]
lam, mu = lame_parameters(E, nu)
FIX_TOL, TIE_TOL, PATCH_TOL = 0.12, 0.30, 0.25
FIX_TOL_OVR = {("Frame", "ra_housing"): 0.30}  # dovetail slide fit: 0.15-0.3 mm clearance, bears once the M4 is tight
H_N = 0.05  # virtual bond-layer thickness for the tie springs (mm)
t0 = time.time()
def log(*a):
    print(f"[{time.time() - t0:6.1f}s]", *a, flush=True)

MT = os.environ.get("MESHTAG", "")
M = np.load(os.path.join(HERE, f"mesh_{G}{MT}.npz"))
S = np.load(os.path.join(HERE, f"surf_{G}.npz"))
xyz, tets, tetvol, tris, trivol = M["xyz"], M["tets"], M["tetvol"], M["tris"], M["trivol"]
pnames = [str(n) for n in M["names"]]
snames = [str(n) for n in S["names"]]
STRI, STRIV = S["tri"], S["triv"]
grp = GROUPS[G]
P = len(pnames)
# drop degenerate (zero-volume) tets, e.g. on the knife-edge where an RA bracket counterbore grazes a side face
_p = xyz[tets]; _a, _b, _c, _d = _p[:, 0], _p[:, 1], _p[:, 2], _p[:, 3]
_V = np.abs(np.einsum("ij,ij->i", np.cross(_b - _a, _c - _a), _d - _a)) / 6
_e2 = sum(((_p[:, i] - _p[:, j]) ** 2).sum(1) for i, j in [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)])
_q = _V * 6 * np.sqrt(2) / (_e2 / 6) ** 1.5
if (_q < 1e-3).any():
    print("dropping", int((_q < 1e-3).sum()), "degenerate tets at", np.round(_p[_q < 1e-3].mean(1), 1).tolist())
    tets, tetvol = tets[_q >= 1e-3], tetvol[_q >= 1e-3]
    _used = np.zeros(len(xyz), bool); _used[tets.ravel()] = True
    _keep = _used[tris].all(1); tris, trivol = tris[_keep], trivol[_keep]


# ---------------- geometry helpers ----------------
def closest_pt_tri(p, a, b, c):
    ab, ac, ap = b - a, c - a, p - a
    d1, d2 = (ab * ap).sum(1), (ac * ap).sum(1)
    bp = p - b; d3, d4 = (ab * bp).sum(1), (ac * bp).sum(1)
    cp = p - c; d5, d6 = (ab * cp).sum(1), (ac * cp).sum(1)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    den = va + vb + vc; den = np.where(np.abs(den) < 1e-30, 1e-30, den)
    v = vb / den; w = vc / den; u = 1 - v - w
    def put(m, uu, vv, ww):
        u[m], v[m], w[m] = uu[m] if np.ndim(uu) else uu, vv[m] if np.ndim(vv) else vv, ww[m] if np.ndim(ww) else ww
    with np.errstate(divide="ignore", invalid="ignore"):
        m = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        ww = (d4 - d3) / ((d4 - d3) + (d5 - d6)); put(m, 0.0, 1 - ww, ww)
        m = (vb <= 0) & (d2 >= 0) & (d6 <= 0); ww = d2 / (d2 - d6); put(m, 1 - ww, 0.0, ww)
        m = (d6 >= 0) & (d5 <= d6); put(m, 0.0, 0.0, 1.0)
        m = (vc <= 0) & (d1 >= 0) & (d3 <= 0); vv = d1 / (d1 - d3); put(m, 1 - vv, vv, 0.0)
        m = (d3 >= 0) & (d4 <= d3); put(m, 0.0, 1.0, 0.0)
        m = (d1 <= 0) & (d2 <= 0); put(m, 1.0, 0.0, 0.0)
    q = u[:, None] * a + v[:, None] * b + w[:, None] * c
    return np.linalg.norm(p - q, axis=1), np.stack([u, v, w], 1)


def nearest_on_tris(pts, T, tol):
    """For each point: (dist, tri index, bary) to the nearest triangle of T (n,3,3) within tol."""
    cen = T.mean(1)
    rad = np.linalg.norm(T - cen[:, None, :], axis=2).max()
    tree = cKDTree(cen)
    lists = tree.query_ball_point(pts, rad + tol)
    pi = np.repeat(np.arange(len(pts)), [len(l) for l in lists])
    ti = np.concatenate([np.asarray(l, dtype=np.int64) for l in lists]) if len(pi) else np.zeros(0, np.int64)
    dist = np.full(len(pts), np.inf); tri = -np.ones(len(pts), np.int64); bary = np.zeros((len(pts), 3))
    if len(pi) == 0:
        return dist, tri, bary
    d, bc = closest_pt_tri(pts[pi], T[ti, 0], T[ti, 1], T[ti, 2])
    order = np.lexsort((d, pi))
    first = np.ones(len(order), bool); first[1:] = pi[order][1:] != pi[order][:-1]
    sel = order[first]
    dist[pi[sel]] = d[sel]; tri[pi[sel]] = ti[sel]; bary[pi[sel]] = bc[sel]
    return dist, tri, bary


# ---------------- per-part FE spaces ----------------
elem = ElementVector(ElementTetP2())
parts = []
g2part = -np.ones(len(xyz), np.int64); g2loc = -np.ones(len(xyz), np.int64)
off = 0
Ks = []
for pid in range(P):
    t = tets[tetvol == pid]
    vid = np.unique(t)
    loc = -np.ones(len(xyz), np.int64); loc[vid] = np.arange(len(vid))
    mesh = MeshTet(np.ascontiguousarray(xyz[vid].T), np.ascontiguousarray(loc[t].T), sort_t=False)
    ib = Basis(mesh, elem, intorder=2)  # exact for P2 stiffness on straight tets
    K = asm(linear_elasticity(lam, mu), ib)
    Ks.append(K)
    g2part[vid] = pid; g2loc[vid] = np.arange(len(vid))
    # boundary vertex outward normals + tributary areas
    bf = mesh.boundary_facets()
    fv = mesh.facets[:, bf]
    tet_of = mesh.f2t[0, bf]
    pa, pb, pc = mesh.p[:, fv[0]].T, mesh.p[:, fv[1]].T, mesh.p[:, fv[2]].T
    nrm = np.cross(pb - pa, pc - pa); area = 0.5 * np.linalg.norm(nrm, axis=1)
    cen_t = mesh.p[:, mesh.t[:, tet_of]].mean(1).T
    sgn = np.sign((((pa + pb + pc) / 3 - cen_t) * nrm).sum(1))
    nrm = nrm * sgn[:, None]
    vn = np.zeros((mesh.p.shape[1], 3)); va = np.zeros(mesh.p.shape[1])
    for k in range(3):
        np.add.at(vn, fv[k], nrm * 0.5)  # area-weighted
        np.add.at(va, fv[k], area / 3)
    bverts = np.unique(fv)
    vn[bverts] /= np.linalg.norm(vn[bverts], axis=1)[:, None]
    parts.append(dict(name=pnames[pid], mesh=mesh, ib=ib, off=off, vid=vid, bf=bf, bverts=bverts, vn=vn, va=va))
    log(pnames[pid], "tets", mesh.t.shape[1], "dofs", ib.N)
    off += ib.N
NDOF = off
log("total dofs", NDOF)


def vdofs(gv):
    """global vertex ids -> (n,3) global dof indices"""
    out = np.zeros((len(gv), 3), np.int64)
    for pid in np.unique(g2part[gv]):
        m = g2part[gv] == pid
        pr = parts[pid]
        out[m] = (pr["ib"].nodal_dofs[:, g2loc[gv[m]]] + pr["off"]).T
    return out


# ---------------- contacts with the mount (fixed) ----------------
fixed_dofs = []
fix_nodes = []  # (pid, local vertex, contact index)
for pid, pr in enumerate(parts):
    gb = pr["vid"][pr["bverts"]]
    d, ti, _ = nearest_on_tris(xyz[gb], STRI, max(FIX_TOL_OVR.values()))
    lab0 = STRIV[np.maximum(ti, 0)]
    tolv = np.array([FIX_TOL_OVR.get((pr["name"], snames[c]), FIX_TOL) for c in lab0])
    hit = d < tolv
    lab = np.where(hit, lab0, -1)
    names_hit = {}
    for c in np.unique(lab[hit]):
        cname = snames[c]
        if grp["contact"].get(int(S["steps"][c])) is None:
            hit[lab == c] = False  # aux solids are load patches, not supports
            continue
        names_hit[cname] = int((lab == c).sum())
    lv = pr["bverts"][hit]
    mask = np.zeros(pr["mesh"].p.shape[1], bool); mask[lv] = True
    fac = pr["bf"][mask[pr["mesh"].facets[:, pr["bf"]]].all(0)]
    dofs = set((pr["ib"].nodal_dofs[:, lv] + pr["off"]).ravel().tolist())
    if len(fac):
        dofs |= set((pr["ib"].get_dofs(facets=fac).all() + pr["off"]).tolist())
    fixed_dofs += list(dofs)
    for v_, c_ in zip(lv, lab[hit]):
        fix_nodes.append((pid, v_, c_))
    log("fixed", pr["name"], names_hit)
fixed_dofs = np.unique(np.array(fixed_dofs, np.int64))

# ---------------- ties between printed parts ----------------
mean_area = [pr["va"][pr["bverts"]].mean() for pr in parts]
rows, cols, vals = [], [], []
ties = []  # dicts per tie set
for A in range(P):
    for B in range(P):
        if A == B or mean_area[A] > mean_area[B] or (mean_area[A] == mean_area[B] and A > B):
            continue  # slave = finer-meshed part
        pa_, pb_ = parts[A], parts[B]
        TB = tris[trivol == B]
        gA = pa_["vid"][pa_["bverts"]]
        d, ti, bc = nearest_on_tris(xyz[gA], xyz[TB], TIE_TOL)
        hit = d < TIE_TOL
        if hit.sum() == 0:
            continue
        ga = gA[hit]; tb = TB[ti[hit]]; w = bc[hit]
        k = E * pa_["va"][pa_["bverts"][hit]] / H_N
        da = vdofs(ga); db = np.stack([vdofs(tb[:, j]) for j in range(3)], 1)  # (n,3 verts,3 comps)
        coef = np.concatenate([np.ones((len(ga), 1)), -w], 1)  # (n,4)
        for c in range(3):
            dd = np.concatenate([da[:, c:c + 1], db[:, :, c]], 1)  # (n,4)
            for i in range(4):
                for j in range(4):
                    rows.append(dd[:, i]); cols.append(dd[:, j]); vals.append(k * coef[:, i] * coef[:, j])
        ties.append(dict(A=A, B=B, ga=ga, da=da, db=db, w=w, k=k, n=pa_["vn"][pa_["bverts"][hit]], x=xyz[ga]))
        log(f"tie {pnames[A]} -> {pnames[B]}: {hit.sum()} nodes, area {pa_['va'][pa_['bverts'][hit]].sum():.0f} mm2")
# ---------------- bolt connectors ----------------
bolts = []
for (lab, tb_, tn_, pa_n, pb_n, dn) in BOLTS[G]:
    bc_, nc_ = np.array(BYTAG[tb_]["com"]), np.array(BYTAG[tn_]["com"])
    bb_ = BYTAG[tb_]["bbox"]; bbn = BYTAG[tn_]["bbox"]
    # axis: from nut toward head; axis point = bolt bbox centre projected
    a = bc_ - nc_
    if np.linalg.norm(a) < 0.5:
        a = np.array([(bb_[0] + bb_[3]) / 2 - (bbn[0] + bbn[3]) / 2, (bb_[1] + bb_[4]) / 2 - (bbn[1] + bbn[4]) / 2,
                      (bb_[2] + bb_[5]) / 2 - (bbn[2] + bbn[5]) / 2])
    a = a / np.linalg.norm(a); p0 = nc_
    import re as _re
    Lnom = float(_re.search(r"x(\d+)", lab).group(1))
    seats = []
    for pn, sg in ((pa_n, 1.0), (pb_n, -1.0)):
        pid = pnames.index(pn); pr = parts[pid]; bv = pr["bverts"]
        Xb = pr["mesh"].p[:, bv].T; nb = pr["vn"][bv]
        dd = Xb - p0; sa = dd @ a; rr = np.linalg.norm(dd - np.outer(sa, a), axis=1)
        m = (rr >= dn / 2 - 0.05) & (rr <= SEAT_ROUT[dn]) & ((nb @ a) * sg > 0.8)
        m &= ((sa > 1.0) & (sa < Lnom + 2.0)) if sg > 0 else ((sa > -3.0) & (sa < 4.0))
        if not m.any():
            raise SystemExit(f"no seat for {lab} on {pn}")
        smax = (sa[m] * sg).max(); m &= (sa * sg) > smax - 0.3
        seats.append((pid, bv[m], pr["va"][bv[m]], float((sa[m]).mean())))
    (pA, vA, wA, sA), (pB, vB, wB, sB) = seats
    L = abs(sA - sB); kb = 2.0e5 * BOLT_AS[dn] / max(L, 1.0)
    gA = parts[pA]["vid"][vA]; gB = parts[pB]["vid"][vB]
    dA = vdofs(gA); dB = vdofs(gB)
    coef = np.concatenate([wA / wA.sum(), -wB / wB.sum()])
    for c in range(3):
        dd = np.concatenate([dA[:, c], dB[:, c]])
        rows.append(np.repeat(dd, len(dd))); cols.append(np.tile(dd, len(dd))); vals.append(kb * np.outer(coef, coef).ravel())
    bolts.append(dict(label=lab, d=dn, axis=a, L=L, k=kb, dA=dA, dB=dB, wA=wA / wA.sum(), wB=wB / wB.sum(),
                      xA=xyz[gA].mean(0), xB=xyz[gB].mean(0), nA=len(vA), nB=len(vB),
                      areaA=float(wA.sum()), areaB=float(wB.sum())))
    log(f"bolt {lab}: head seat {len(vA)} nodes ({wA.sum():.1f} mm2) on {pa_n}, nut seat {len(vB)} nodes ({wB.sum():.1f} mm2) on {pb_n}, L={L:.1f}, k={kb:.0f} N/mm")
KT = sp.coo_matrix((np.concatenate(vals), (np.concatenate(rows), np.concatenate(cols))), shape=(NDOF, NDOF)).tocsr() if rows else None
K = sp.block_diag(Ks, format="csr")
if KT is not None:
    K = (K + KT).tocsr()
del Ks
log("K assembled", K.nnz)

# ---------------- load patches + unit load cases ----------------
cases = []  # (name, F vector)
Fcols = []


def patch_vertices(pid, aux_names, tol=PATCH_TOL, extra=None):
    pr = parts[pid]
    gb = pr["vid"][pr["bverts"]]
    idx = [i for i, n in enumerate(snames) if n in aux_names]
    sel = np.isin(STRIV, idx)
    d, _, _ = nearest_on_tris(xyz[gb], STRI[sel], tol)
    m = d < tol
    if extra is not None:
        m |= extra(xyz[gb])
    return pr["bverts"][m]


def remote_unit_loads(pid, lv, xref, comps, label):
    pr = parts[pid]; mesh = pr["mesh"]
    mask = np.zeros(mesh.p.shape[1], bool); mask[lv] = True
    fac = pr["bf"][mask[mesh.facets[:, pr["bf"]]].all(0)]
    fb = FacetBasis(mesh, elem, facets=fac)
    X = fb.global_coordinates().value  # (3,nf,nq)
    dx = fb.dx
    A = dx.sum(); c = (X * dx).sum((1, 2)) / A
    r = X - c[:, None, None]
    J = np.zeros((3, 3))
    for i in range(3):
        for j in range(3):
            J[i, j] = (((r ** 2).sum(0) * (i == j) - r[i] * r[j]) * dx).sum()
    Ji = np.linalg.inv(J)
    log(f"patch {label}: {len(fac)} facets, area {A:.0f} mm2, centroid {np.round(c, 1)}")
    for comp in comps:
        Fv = np.zeros(3); Mv = np.zeros(3)
        if comp < 3: Fv[comp] = 1.0
        else: Mv[comp - 3] = 1.0  # N*mm
        Mc = Mv + np.cross(np.asarray(xref) - c, Fv)
        a = Fv / A; om = Ji @ Mc
        @LinearForm
        def lf(v, w):
            rr = w.x - c[:, None, None]
            t = np.stack([a[0] + om[1] * rr[2] - om[2] * rr[1], a[1] + om[2] * rr[0] - om[0] * rr[2],
                          a[2] + om[0] * rr[1] - om[1] * rr[0]])
            return dot(t, v)
        f = np.zeros(NDOF); f[pr["off"]:pr["off"] + pr["ib"].N] = asm(lf, fb)
        Fcols.append(f); cases.append(f"{label}:{'Fx Fy Fz Mx My Mz'.split()[comp]}")
    return dict(area=A, centroid=c.tolist(), nfac=len(fac))


patches = {}
def pid_of(n): return pnames.index(n)
def com(tag): return np.array(BYTAG[tag]["com"])
def bbc(tag): b = BYTAG[tag]["bbox"]; return np.array([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2])

if G == "RA":
    scr = np.array([BYTAG[t]["com"] for t in (524, 525, 526, 527)])
    axis = scr[:, :2].mean(0)
    zhead = 62.0  # v3: M3x10 heads bear on the 1.5 mm pulley-side pad
    def heads(p):
        rr = np.min(np.linalg.norm(p[:, None, :2] - scr[None, :, :2], axis=2), axis=1)
        return (np.abs(p[:, 2] - zhead) < 0.15) & (rr < 2.9)
    motor_ref = np.array([axis[0], axis[1], 57.0])
    lv = patch_vertices(pid_of("RA_bracket"), {"RA_motor"}, extra=heads)
    patches["RA_motor"] = remote_unit_loads(pid_of("RA_bracket"), lv, motor_ref, range(6), "RA_motor")
    patches["RA_motor"]["ref"] = motor_ref.tolist()
    conn = {"GX16_socket": (513, -137.0), "GX12_1_socket": (515, -136.0), "GX12_2_socket": (517, -136.0), "DC_socket": (91, -80.0)}
    for n, (tg, zend) in conn.items():
        c0 = bbc(tg); ref = np.array([c0[0], c0[1], zend])
        lv = patch_vertices(pid_of("Frame"), {n})
        patches[n] = remote_unit_loads(pid_of("Frame"), lv, ref, range(6), n); patches[n]["ref"] = ref.tolist()
    ref = com(491)
    lv = patch_vertices(pid_of("Base_plate"), {"USB_socket"}, tol=0.35)
    patches["USB_socket"] = remote_unit_loads(pid_of("Base_plate"), lv, ref, range(6), "USB_socket"); patches["USB_socket"]["ref"] = ref.tolist()
    b = BYTAG[85]["bbox"]; ref = np.array([(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, b[5]])
    lv = patch_vertices(pid_of("Frame"), {"Button_Enter"})
    patches["Button_Enter"] = remote_unit_loads(pid_of("Frame"), lv, ref, [2], "Button_Enter"); patches["Button_Enter"]["ref"] = ref.tolist()
else:
    scr = np.array([BYTAG[t]["com"] for t in (546, 547, 548, 549)])
    axis = scr[:, :2].mean(0)
    zhead = 63.0
    def heads(p):
        rr = np.min(np.linalg.norm(p[:, None, :2] - scr[None, :, :2], axis=2), axis=1)
        return (np.abs(p[:, 2] - zhead) < 0.15) & (rr < 2.9)
    motor_ref = np.array([axis[0], axis[1], 57.5])
    lv = patch_vertices(pid_of("Dec_bracket"), {"Dec_motor"}, extra=heads)
    patches["Dec_motor"] = remote_unit_loads(pid_of("Dec_bracket"), lv, motor_ref, range(6), "Dec_motor")
    patches["Dec_motor"]["ref"] = motor_ref.tolist()

# bolt preload: imposed 1 mm shortening of each bolt spring (scaled to the real preload in post)
for bi, b in enumerate(bolts):
    f = np.zeros(NDOF)
    coefA = b["wA"]; coefB = -b["wB"]
    for c in range(3):
        np.add.at(f, b["dA"][:, c], b["k"] * coefA * (-b["axis"][c]))
        np.add.at(f, b["dB"][:, c], b["k"] * coefB * (-b["axis"][c]))
    Fcols.append(f); cases.append(f"preload:{b['label']}")
# self weight, 1 g along -Y (world up = +Y)
fg = np.zeros(NDOF)
for pr in parts:
    @LinearForm
    def gl(v, w):
        return -mat["rho"] * 9810.0 * v.value[1]
    fg[pr["off"]:pr["off"] + pr["ib"].N] = asm(gl, pr["ib"])
Fcols.append(fg); cases.append("self_weight_1g")
F = np.stack(Fcols, 1)
log("load cases", len(cases))

# ---------------- solve ----------------
free = np.setdiff1d(np.arange(NDOF), fixed_dofs)
Kff = K[free][:, free].tocsr()
solver = pypardiso.PyPardisoSolver(mtype=2)
U = np.zeros((NDOF, F.shape[1]))
U[free] = solver.solve(sp.triu(Kff, format="csr"), F[free])
solver.free_memory(everything=True)
log("solved", Kff.shape)
res = np.abs(Kff @ U[free] - F[free]).max() / max(np.abs(F[free]).max(), 1e-30)
log("relative residual", res)
if not res < 1e-6:
    rr = np.abs(Kff @ U[free] - F[free]).max(1)
    worst = free[np.argsort(rr)[-20:]]
    for pid, pr in enumerate(parts):
        m = (worst >= pr["off"]) & (worst < pr["off"] + pr["ib"].N)
        if m.any():
            loc = pr["ib"].doflocs[:, worst[m] - pr["off"]].T
            log("residual hot dofs in", pr["name"], np.round(loc, 1).tolist()[:6])
    log("max |U|", np.abs(U).max(), "case", np.unravel_index(np.abs(U).argmax(), U.shape)[1])
    raise SystemExit("SOLVE FAILED (singular / unconstrained part?)")
R = K @ U - F  # reactions at fixed dofs (force applied by the support on the part)

# ---------------- post: nodal stresses (vertex-averaged), displacements, joint forces ----------------
Xq = np.array([[0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]], float); Wq = np.full(4, 1 / 24)
out = dict(cases=np.array(cases), names=np.array(pnames), mat=MAT, E=E, nu=nu)
for pid, pr in enumerate(parts):
    mesh = pr["mesh"]
    vb = Basis(mesh, elem, quadrature=(Xq, Wq))
    nv = mesh.p.shape[1]
    sig_all = np.zeros((len(cases), nv, 6), np.float32)
    cnt = np.zeros(nv); np.add.at(cnt, mesh.t.ravel(), 1)
    for ci in range(len(cases)):
        up = U[pr["off"]:pr["off"] + pr["ib"].N, ci]
        Gd = vb.interpolate(up).grad  # (3,3,nel,4)
        eps = 0.5 * (Gd + Gd.transpose(1, 0, 2, 3)); tr = eps[0, 0] + eps[1, 1] + eps[2, 2]
        comps = [(0, 0), (1, 1), (2, 2), (0, 1), (1, 2), (0, 2)]
        for k, (i, j) in enumerate(comps):
            s = 2 * mu * eps[i, j] + (lam * tr if i == j else 0)
            acc = np.zeros(nv); np.add.at(acc, mesh.t.T.ravel(), s.ravel())  # (nel,4) order matches t.T
            sig_all[ci, :, k] = acc / cnt
    disp = np.stack([U[pr["ib"].nodal_dofs[c] + pr["off"]] for c in range(3)], 1).transpose(2, 0, 1)  # (ncase,nv,3)
    out[f"p{pid}_xyz"] = mesh.p.T; out[f"p{pid}_sig"] = sig_all; out[f"p{pid}_u"] = disp.astype(np.float32)
    out[f"p{pid}_tris"] = mesh.facets[:, pr["bf"]].T
    # flag vertices on fixed / tied / loaded boundaries (BC singularities)
    out[f"p{pid}_bverts"] = pr["bverts"]
# fixed-node reactions (vertex dofs only carry most of it; include edge dofs by summing all fixed dofs per part/contact)
fx = []
for pid, pr in enumerate(parts):
    lvs = np.array([v for (p_, v, c_) in fix_nodes if p_ == pid], np.int64)
    labs = np.array([c_ for (p_, v, c_) in fix_nodes if p_ == pid], np.int64)
    if len(lvs) == 0: continue
    ib = pr["ib"]; locs = ib.doflocs.T
    gd = np.arange(pr["off"], pr["off"] + ib.N)
    isfix = np.isin(gd, fixed_dofs)
    fd = gd[isfix]; floc = locs[isfix]
    comp = np.zeros(ib.N, np.int64)
    for c in range(3):
        comp[ib.nodal_dofs[c]] = c
        if hasattr(ib, "edge_dofs") and ib.edge_dofs.size: comp[ib.edge_dofs[c]] = c
    fcomp = comp[isfix]
    tree = cKDTree(pr["mesh"].p[:, lvs].T)
    _, nn = tree.query(floc)
    # accumulate reaction per fixed vertex (edge dofs go to the nearest fixed vertex)
    Rv = np.zeros((len(lvs), 3, len(cases)))
    for c in range(3):
        m = fcomp == c
        np.add.at(Rv[:, c, :], nn[m], R[fd[m]])
    fx.append(dict(pid=pid, x=pr["mesh"].p[:, lvs].T, n=pr["vn"][lvs], lab=labs, R=Rv))
out["fix_pid"] = np.concatenate([np.full(len(f["lab"]), f["pid"]) for f in fx])
out["fix_x"] = np.vstack([f["x"] for f in fx]); out["fix_n"] = np.vstack([f["n"] for f in fx])
out["fix_lab"] = np.concatenate([f["lab"] for f in fx]); out["fix_R"] = np.vstack([f["R"] for f in fx]).astype(np.float32)
out["contact_names"] = np.array(snames)
# tie forces (force on the slave part A)
tA, tB, tX, tN, tF = [], [], [], [], []
for t in ties:
    ua = U[t["da"]]  # (n,3,ncase)
    ub = (U[t["db"]] * t["w"][:, :, None, None]).sum(1)
    f = -t["k"][:, None, None] * (ua - ub)
    tA.append(np.full(len(t["ga"]), t["A"])); tB.append(np.full(len(t["ga"]), t["B"]))
    tX.append(t["x"]); tN.append(t["n"]); tF.append(f)
if ties:
    out["tie_A"] = np.concatenate(tA); out["tie_B"] = np.concatenate(tB); out["tie_x"] = np.vstack(tX)
    out["tie_n"] = np.vstack(tN); out["tie_F"] = np.vstack(tF).astype(np.float32)
if bolts:
    D = []
    for b in bolts:
        uA = (U[b["dA"]] * b["wA"][:, None, None]).sum(0)  # (3,ncase)
        uB = (U[b["dB"]] * b["wB"][:, None, None]).sum(0)
        Fb = b["k"] * (uA - uB)  # spring force vector; tension = D . axis
        ci = cases.index(f"preload:{b['label']}")
        Fb[:, ci] += b["k"] * b["axis"]  # remove the imposed shortening
        D.append(Fb)
    out["bolt_F"] = np.array(D, np.float32); out["bolt_axis"] = np.array([b["axis"] for b in bolts])
    out["bolt_label"] = np.array([b["label"] for b in bolts]); out["bolt_xA"] = np.array([b["xA"] for b in bolts])
    out["bolt_xB"] = np.array([b["xB"] for b in bolts]); out["bolt_L"] = np.array([b["L"] for b in bolts])
    out["bolt_d"] = np.array([b["d"] for b in bolts]); out["bolt_area"] = np.array([[b["areaA"], b["areaB"]] for b in bolts])
np.savez_compressed(os.path.join(HERE, f"res_{G}_{MAT}{MT}.npz"), **out)
json.dump(dict(patches=patches, cases=cases, ndof=int(NDOF), nfixed=int(len(fixed_dofs)), residual=float(res)),
          open(os.path.join(HERE, f"res_{G}_{MAT}{MT}.json"), "w"), indent=1)
log("saved")
