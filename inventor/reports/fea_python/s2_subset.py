# Keep only the solids the analysis needs (model-level removal is fast) and write subset.brep
import gmsh, time, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import ALL_KEEP, HERE
STEP = r"Z:\ASTRA\inventor\export\SELENO_CG5_C8N_assembly.step"
t0 = time.time()
gmsh.initialize(); gmsh.option.setNumber("General.Terminal", 0)
gmsh.model.occ.importShapes(STEP); gmsh.model.occ.synchronize()
print("import", round(time.time() - t0, 1), flush=True)
rem = [(3, t) for _, t in gmsh.model.getEntities(3) if t not in ALL_KEEP]
gmsh.model.removeEntities(rem, recursive=True)
gmsh.write(os.path.join(HERE, "subset.brep"))
print("left", len(gmsh.model.getEntities(3)), "of", len(ALL_KEEP), round(time.time() - t0, 1))
gmsh.finalize()
