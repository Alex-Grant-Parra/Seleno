# Mesh one group.  Usage: s3_mesh.py GROUP hmin hmax [tag]
#  pass 1: 3D tet mesh of the printed parts (each solid meshed on its own -> non-conformal;
#          the solver ties touching parts together, like Inventor's bonded contacts)
#  pass 2: fine surface mesh of the contact / aux solids, used to find which printed
#          boundary nodes touch the mount (fixed support) or carry a load patch.
import gmsh, numpy as np, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import GROUPS, HERE, match_volumes

G = sys.argv[1]; hmin = float(sys.argv[2]); hmax = float(sys.argv[3])
tag = sys.argv[4] if len(sys.argv) > 4 else ""
grp = GROUPS[G]
t0 = time.time()


def load():
    gmsh.initialize()
    gmsh.option.setNumber("General.Terminal", 0)
    gmsh.option.setNumber("General.NumThreads", 12)
    gmsh.model.occ.importShapes(os.path.join(HERE, "subset.brep"))
    gmsh.model.occ.synchronize()
    return match_volumes(gmsh)


def keep_only(vmap, steptags):
    rem = [(3, v) for v, s in vmap.items() if s not in steptags]
    gmsh.model.removeEntities(rem, recursive=True)
    return {v: s for v, s in vmap.items() if s in steptags}


# ---------- pass 1: printed parts, 3D, one solid per session (with retries) ----------
CONN_BALLS = [(57.25,125.9,-45,13,1.1),(27.0,122.9,-45,11,1.1),(39.9,139.4,-45,11,1.1),(43.7,108.55,-45,9,1.1),
              (-9,152,42,10,1.0),(25.6,130.9,45,11,1.2)]
SIZES = {  # part: list of (hmin, hmax, curv, alg3, balls) attempts
    "Frame": [(1.0,2.4,6,1,CONN_BALLS),(1.0,2.2,6,10,CONN_BALLS),(0.9,2.0,8,1,CONN_BALLS)],
    "Base_plate": [(1.2,3.0,6,1,[]),(1.0,2.6,6,10,[])],
    "RA_bracket": [(1.4,3.2,6,1,[(-3.1,148.2,67.5,3,0.3),(-4,148.5,67,7,0.8)]),(1.4,3.2,6,1,[(-3.1,148.2,67.5,2.5,0.2)]),(1.3,3.0,6,1,[(-4,148.5,67,6,0.6)]),(1.2,2.8,8,10,[])],
    "RA_strap_pz": [(1.3,3.0,6,1,[]),(1.2,2.6,6,10,[])],
    "RA_strap_mz": [(1.3,3.0,6,1,[]),(1.2,2.6,6,10,[])],
    "Dec_bracket": [(1.8,4.5,6,1,[]),(1.6,4.0,6,10,[]),(1.5,3.6,8,1,[])],
    "Dec_clamp_cap": [(1.3,3.0,6,1,[]),(1.2,2.6,6,10,[])],
    "Dec_band_cap_pz": [(1.2,2.6,6,1,[]),(1.0,2.4,6,10,[])],
    "Dec_band_cap_mz": [(1.2,2.6,6,1,[]),(1.0,2.4,6,10,[])],
}
SCALE0 = float(os.environ.get("HSCALE", "1"))
PSCALE = dict(kv.split(":") for kv in os.environ.get("HSCALE_PART", "").split(",") if kv)
order = list(grp["printed"].keys())
X, TETS, TV, TRIS, TRV = [], [], [], [], []
off = 0
for pid, s in enumerate(order):
    name = grp["printed"][s]
    SCALE = SCALE0 * float(PSCALE.get(name, 1.0))
    ok = False
    for hmin, hmax, curv, alg3, balls in SIZES[name]:
        vmap = load()
        vm = keep_only(vmap, {s})
        gmsh.option.setNumber("Mesh.MeshSizeMin", hmin * SCALE)
        gmsh.option.setNumber("Mesh.MeshSizeMax", hmax * SCALE)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", curv)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.option.setNumber("Mesh.Algorithm3D", alg3)
        gmsh.option.setNumber("Mesh.Optimize", 1)
        if balls:
            fids = []
            for x, y, z, r, h in balls:
                f = gmsh.model.mesh.field.add("Ball")
                for k, val in (("XCenter", x), ("YCenter", y), ("ZCenter", z), ("Radius", r), ("VIn", h * SCALE), ("VOut", 1e3)):
                    gmsh.model.mesh.field.setNumber(f, k, val)
                fids.append(f)
            fm = gmsh.model.mesh.field.add("Min"); gmsh.model.mesh.field.setNumbers(fm, "FieldsList", fids)
            gmsh.model.mesh.field.setAsBackgroundMesh(fm)
        try:
            gmsh.model.mesh.generate(3)
        except Exception as e:
            print("  fail", name, (hmin, hmax, curv, alg3), str(e)[:60], flush=True)
            gmsh.finalize(); continue
        v = list(vm.keys())[0]
        ntags, xyz, _ = gmsh.model.mesh.getNodes()
        xyz = xyz.reshape(-1, 3)
        idx = -np.ones(int(ntags.max()) + 1, dtype=np.int64); idx[ntags.astype(np.int64)] = np.arange(len(ntags))
        et, _, en = gmsh.model.mesh.getElements(3, v)
        t4 = idx[[en[i] for i in range(len(et)) if et[i] == 4][0].reshape(-1, 4).astype(np.int64)]
        t3s = []
        for _, f in gmsh.model.getBoundary([(3, v)], oriented=False):
            et, _, en = gmsh.model.mesh.getElements(2, abs(f))
            t3s.append(idx[[en[i] for i in range(len(et)) if et[i] == 2][0].reshape(-1, 3).astype(np.int64)])
        t3 = np.vstack(t3s)
        used = np.unique(t4); rm = -np.ones(len(xyz), dtype=np.int64); rm[used] = np.arange(len(used))
        X.append(xyz[used]); TETS.append(rm[t4] + off); TV.append(np.full(len(t4), pid))
        TRIS.append(rm[t3] + off); TRV.append(np.full(len(t3), pid)); off += len(used)
        print(" ", name, "tets", len(t4), "nodes", len(used), (hmin, hmax, curv, alg3), round(time.time() - t0, 1), flush=True)
        gmsh.finalize(); ok = True; break
    assert ok, name
np.savez(os.path.join(HERE, f"mesh_{G}{tag}.npz"), xyz=np.vstack(X), tets=np.vstack(TETS), tetvol=np.concatenate(TV),
         tris=np.vstack(TRIS), trivol=np.concatenate(TRV), names=np.array([grp["printed"][s] for s in order]))
print(G, "nodes", off, "tets", sum(len(t) for t in TETS), round(time.time() - t0, 1), flush=True)

# ---------- pass 2: contact + aux surfaces, 2D ----------
if not os.path.exists(os.path.join(HERE, f"surf_{G}.npz")):
    vmap = load()
    others = {**grp["contact"], **grp["aux"]}
    vm = keep_only(vmap, set(others))
    gmsh.option.setNumber("Mesh.MeshSizeMin", 0.2)
    gmsh.option.setNumber("Mesh.MeshSizeMax", 1.0)
    gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 20)
    gmsh.model.mesh.generate(2)
    ntags, xyz, _ = gmsh.model.mesh.getNodes()
    xyz = xyz.reshape(-1, 3)
    idx = -np.ones(int(ntags.max()) + 1, dtype=np.int64); idx[ntags.astype(np.int64)] = np.arange(len(ntags))
    T, TV = [], []
    names = list(others.values()); steps = list(others.keys())
    for v, s in vm.items():
        for _, f in gmsh.model.getBoundary([(3, v)], oriented=False):
            et, _, en = gmsh.model.mesh.getElements(2, abs(f))
            t3 = [en[i] for i in range(len(et)) if et[i] == 2][0].reshape(-1, 3)
            T.append(xyz[idx[t3.astype(np.int64)]]); TV.append(np.full(len(t3), steps.index(s)))
    np.savez(os.path.join(HERE, f"surf_{G}.npz"), tri=np.vstack(T), triv=np.concatenate(TV), names=np.array(names),
             steps=np.array(steps))
    print("surf tris", sum(len(t) for t in T), round(time.time() - t0, 1))
    gmsh.finalize()
