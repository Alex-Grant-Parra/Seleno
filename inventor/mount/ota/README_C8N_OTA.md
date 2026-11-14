# Celestron C8-N OTA (8" f/5 Newtonian, 1000 mm FL)

Top assembly: `..\CG5_SelenoCompact_Motors_C8N.iam`. It places `CG5_SelenoCompact_Motors.iam` (unchanged) plus `C8N_OTA.iam` on the saddle, in the home position (tube parallel to the RA axis, open end toward the pole). There are 0 interferences.

The model is built from one photo plus published specs. All dimensions are estimates.

## Main dimensions
| Item | Model | Source |
|---|---|---|
| Tube | 232 OD x 940 long (incl. rear cell), 2 mm wall | 37" length from the Celestron manual. The OD is the standard Synta 8" f/5 tube (**measure**) |
| Primary | 203 mm, 25 thick, face 43 mm from the rear | spec |
| Secondary | 50 mm minor axis, 800 mm up the tube from the primary face | f/5 geometry |
| Focuser | 2" rack and pinion, centre 98 mm from the front, knobs fore/aft, 2"->1.25" adapter + 25 mm eyepiece | estimated |
| Finder | 9x50 on a single-ring bracket, 165-205 mm from the front | photo |
| Rings | hinged, 232 ID x 25 wide, clamp thumbscrew on top, hinge on the +Z side; centres 215 and 520 mm from the rear | photo |
| Dovetail | Vixen 44 mm, 15 high, 380 long, 1x M6x16 into each ring from a bottom channel | estimated |
| Collimation | 3 knurled knobs at r 88 + 3 lock screws at r 70 on the rear cell | Synta-style layout |

## Placement
- Tube axis is 272 mm above the RA axis, measured along the Dec axis. The bar sits on the saddle slot floor.
- The saddle is at 445 mm from the rear end, just behind the front ring as in the photo.
- The focuser is turned 50 deg from the top toward -Z, the side away from the photo camera. The finder is 15 deg toward +Z.
- To turn the tube in its rings, change `$A_FOC` / `$A_FND` in the build lib and rebuild.

## Measure to firm it up
1. Tube OD and length.
2. Ring spacing.
3. Bar length and where the saddle clamps it.
4. Focuser distance from the front end and its angle from the bar.
