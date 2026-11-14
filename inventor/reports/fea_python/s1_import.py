# Import the assembly STEP once, list every solid with name / bbox / volume / COM,
# then keep only the parts the stress analysis needs and write them to one BREP.
import gmsh, json, time, sys, os
STEP = r"Z:\ASTRA\inventor\export\SELENO_CG5_C8N_assembly.step"
OUT = os.path.dirname(os.path.abspath(__file__))
t0 = time.time()
gmsh.initialize()
gmsh.option.setNumber("General.Terminal", 0)
gmsh.option.setNumber("Geometry.OCCImportLabels", 1)
gmsh.model.occ.importShapes(STEP)
gmsh.model.occ.synchronize()
print("import s", round(time.time() - t0, 1), flush=True)
rows = []
for d, t in gmsh.model.getEntities(3):
    name = gmsh.model.getEntityName(d, t)
    bb = gmsh.model.occ.getBoundingBox(d, t)
    v = gmsh.model.occ.getMass(d, t)
    c = gmsh.model.occ.getCenterOfMass(d, t)
    rows.append(dict(tag=t, name=name, bbox=bb, vol=v, com=c))
json.dump(rows, open(os.path.join(OUT, "solids_all.json"), "w"), indent=0)
print("solids", len(rows), "s", round(time.time() - t0, 1))
