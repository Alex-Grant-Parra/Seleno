# SELENO drive stress analysis (Python FEA)

These scripts produced `../SELENO_Drive_Stress_PLA_PETG.html` on 2026-11-10. They run outside Inventor, because Inventor's Stress Analysis has no API.

Setup: Python 3.14 venv with `numpy scipy gmsh scikit-fem meshio matplotlib pyamg pypardiso pillow`. These tools are used only for the analysis and are not part of the product.

Run in this folder, in this order:

| Step | Command | What it does | Time |
|---|---|---|---|
| 1 | `python s1_import.py` | Lists every solid in `export/SELENO_CG5_C8N_assembly.step` and writes `solids_all.json` | 70 s |
| 2 | `python s2_subset.py` | Keeps the 34 solids the analysis needs and writes `subset.brep` | 90 s |
| 3 | `python s3_mesh.py RA 0 0` and `python s3_mesh.py DEC 0 0` | Quadratic-tet meshes of the printed parts, plus surface meshes of the mount, connectors and motors | 10 s each |
| 4 | `python s4_solve.py RA PLA` (also PETG, and DEC) | Solves all unit load cases with MKL Pardiso and writes `res_*.npz` | 1.5 to 2.5 min each |
| 5 | `python s5_post.py RA PLA` (and the others) | Combines the unit cases into the real load cases and writes `post_*.json` | 1 min |
| 6 | `python s6_hand.py` | Hand checks: belt, pulleys, grub screws, motor shaft, worm stubs, bolt torques, J7 movement | 5 s |
| 7 | `python s7_fig.py ...` | Stress maps (see the argument list at the top of the script) | 30 s |
| 8 | `python s8_report.py out.html` | Builds the report | 5 s |

Mesh check: `HSCALE_PART=RA_bracket:1.35 python s3_mesh.py RA 0 0 _coarse`, then run steps 4 and 5 with `MESHTAG=_coarse`. Results are in `conv.json`.

Load cases, materials and bolt definitions are in `common.py` and at the top of `s5_post.py`.
