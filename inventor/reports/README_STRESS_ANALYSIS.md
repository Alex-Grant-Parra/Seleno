# SELENO compact enclosure — Stress Analysis (PETG / ASA / PA12 / ABS)

Static stress check of the two printed parts of `SelenoCompactAssembly.iam`
(`frame_compact_connectors.ipt` + `enclosure/base_plate_compact.ipt`) under handling loads,
run in Inventor Stress Analysis and exported with its Report generator.

The study setup is manual, because Inventor's Stress Analysis has no API. Everything that can
be scripted is already done in `fea/`:

| File | What it is |
|---|---|
| `fea/Seleno_FEA.iam` | The frame and base plate only, placed exactly as in `SelenoCompactAssembly.iam`. It has 3 work points for the remote forces. |
| `fea/frame_compact_FEA.ipt` | A copy of `frame_compact_connectors.ipt`. Load faces are **red**. All 4 materials are in the file, and PETG is active. |
| `fea/base_plate_compact_FEA.ipt` | A copy of `base_plate_compact.ipt`. The fixed face is **blue**. All 4 materials are in the file, and PETG is active. |

The original parts are untouched.

## Materials (already in the files)

| Inventor material | E (MPa) | ν | ρ (kg/m³) | Yield used (MPa) | Tensile (MPa) | Source |
|---|---|---|---|---|---|---|
| `PETG (FDM, Prusament)` | 1500 | 0.38 | 1270 | 47.0 | 47.0 | Prusament PETG TDS v1.1: printed, horizontal 47 / vertical 50 MPa, E 1.5 GPa |
| `ASA (FDM, PolyLite)` | 2379 | 0.38 | 1070 | **32.0 (Z)** | 43.8 (X-Y) | Polymaker PolyLite ASA TDS: X-Y 43.8 / Z 32 MPa, E 2379 MPa |
| `PA12 (MJF, HP 3D HR)` | 1700 | 0.40 | 1010 | 48.0 | 48.0 | HP 3D HR PA12: 48 MPa X-Y and Z, E 1700 (X-Y) / 1800 (Z) MPa |
| `ABS (FDM, PolyLite)` | 2247 | 0.38 | 1040 | **29.7 (Z)** | 33.4 (X-Y) | Polymaker PolyLite ABS TDS v5.6: X-Y 33.4 / Z 29.7 MPa, E 2247 MPa |

**Yield used** is the lower of the X-Y and Z strengths. So Inventor's safety factor already
assumes the worst layer direction, which matters for walls printed upright. These are typical
datasheet values for comparison, not guaranteed minimums.

## Load case: "handling"

All coordinates below are in the coordinates of `Seleno_FEA.iam`. They match
`SelenoCompactAssembly.iam`: the GX/DC connector wall is at **x = −45** and the button wall is
at **x = +45**.

| # | Type | Faces | Value |
|---|---|---|---|
| C1 | Fixed constraint | Base plate bottom (**blue**) | — |
| L1 | Remote Force | GX16 (IMU) bore (red, Ø16.3) | Fy = **−50 N** at point `RF GX16 (IMU)` = (−95, 15, −8) mm |
| L2 | Remote Force | GX12 #1 bore (red, Ø12.3, upper, z = +13) | Fy = **−50 N** at `RF GX12 #1` = (−95, 37, 13) mm |
| L3 | Remote Force | GX12 #2 bore (red, Ø12.3, upper, z = −8) | Fy = **−50 N** at `RF GX12 #2` = (−95, 37, −8) mm |
| L4 | Force | DC socket bore (red, Ø8.2) | Fx = **+10 N** (plug pushed in) |
| L5 | Moment | DC socket bore (same face) | **0.3 N·m** about X (nut / plug twist) |
| L6 | Force | Button **Enter** hole: bore + D-flat (2 red faces, top row) | Fx = **−20 N** (pressed inwards) |

- **L1 to L3** model a cable snagged downward. The 50 N acts at the plug's cable exit, 50 mm
  out from the wall, so each wall also carries about 2.5 N·m of bending.
- **L6** uses only one button, because nobody presses all six at once.
- The loads act on the hole bores rather than on nut-sized rings. That gives slightly higher,
  and so conservative, peaks at the hole edges.

## Steps in Inventor (about 15 min in total)

1. Open `reports/fea/Seleno_FEA.iam`, then go to **Environments → Stress Analysis**.
2. Click **Create Study** and set:
   - Name: `PETG`
   - Type: Static Analysis
   - Design objective: Single Point
3. Set up contacts:
   1. Open **Stress Analysis Settings → Contact** and set the tolerance to **0.25 mm**. The
      base-plate screw tabs sit 0.2 mm inside the skirt, and bonding them stands in for the
      M3 screws.
   2. Right-click **Contacts → Automatic Contacts**, and keep the default type, *Bonded*.
   3. Check that the browser shows contacts between the frame and the base plate.
4. Open **Assign Materials** and check that both components show `PETG (FDM, Prusament)`. Set
   the safety factor to **Yield strength**.
5. Add **Fixed** constraint C1 on the blue face.
6. Add loads L1 to L6 as listed in the table. For the Remote Force, select the red bore, then
   type in the location or pick the work point, and enter Fy = −50 N.
7. Set the mesh:
   - In **Mesh Settings**: average element size 0.05, minimum 0.2, grading 1.5, and *Create
     curved mesh elements* on.
   - Add a **Local Mesh Control** of **0.8 mm** on the 6 red faces.
8. Run **Simulate**. Then run **Convergence Settings** with 2 h-refinements on the von Mises
   stress, and simulate again if the peak moves by more than 10%.
9. Right-click the study, choose **Copy Study** three times, and rename the copies `ASA`,
   `PA12` and `ABS`.
10. In each copy, open **Assign Materials** and set the **Override material** for both
    components to that copy's material. Then run Simulate.
11. Generate the reports. For each study, click **Report**, choose that study only, and save it
    here in `reports/`:
    - `SELENO_Stress_PETG.mht`
    - `SELENO_Stress_ASA.mht`
    - `SELENO_Stress_PA12.mht`
    - `SELENO_Stress_ABS.mht`

## What to expect

This is a linear static analysis with the same geometry and loads in every study. The
**stress field is almost the same in all four** (PA12 differs only slightly, through its
Poisson's ratio of 0.40). The materials differ in two ways:

- **Safety factor** scales with the yield used: PA12 48 ≈ PETG 47 > ASA 32 > ABS 29.7 MPa.
- **Deflection** scales with 1/E. Taking ASA as 1.0, ABS is 1.06, PA12 1.40 and PETG 1.59.

If a study shows a minimum safety factor below 1, check where it is. If it is at a GX hole,
the 2 mm connector wall is the problem, not the material choice.

## Limits of this model

- The material model is isotropic and linear-elastic. Real FDM parts are anisotropic, and the
  conservative Z strength is used only for yield. The model includes no creep and no
  temperature effects.
- The material values come from manufacturers' typical datasheets. The datasheets themselves
  say they are not design specifications. Wall count, infill and print orientation shift them.
- Bonded contacts treat the frame-to-base-plate joint and the screws as rigid.
- The base plate's micro-USB bracket is not loaded.
