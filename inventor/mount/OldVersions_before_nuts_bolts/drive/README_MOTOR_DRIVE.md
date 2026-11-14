# CG-5 motor drive (NEMA 17, belt) - brackets v2

Top assemblies:
- `..\CG5_SelenoCompact_Motors.iam`: CG-5 + SELENO compact + both drives. 0 interferences.
- `..\CG5_SelenoCompact_Motors_C8N.iam`: the same, plus the C8-N OTA. 0 interferences.

v2 (2026-11-04) follows a mechanical engineer's review: more support on both axes, a wrap strap on the RA, a second band on the Dec, and fillets on every printed part. The SELENO frame and base plate are orange in these assemblies. v1 files are in `..\OldVersions_before_bracket_v2\`.

## Drive train (both axes identical)
- Motor: SIMAX3D NEMA 17 42x38, 1.8 deg, 1.5 A, 5 mm D-shaft
- GT2 16T pulley (5 mm bore) on the motor, GT2 40T pulley (6 mm bore) on the 6 mm brass worm stub, hub facing the mount, grub screw on the D-flat
- GT2 6 mm closed belt, 158 mm, at 50.4 mm centres. The motor slots give ±4 mm, which also fits 152 or 160 mm loops.
- 2.5:1 belt x 144:1 worm = 360 motor turns per axis turn. At 1/16 microstepping that is 1.125 arcsec per microstep.

## RA
- `ra_motor_bracket.ipt` screws onto the SELENO frame's +Z wall above the buttons with **5x M3x30** into M3 heat-set inserts (4.2 mm holes, 7.5 deep). v1 had 3.
  - The foot block now runs x 22..60 along the wall.
  - The motor plate is about 20 mm bigger on the polar side and below the motor.
  - A 9 mm rim stiffens the plate on three sides of the motor. The screw heads sit in counterbores through the rim.
  - Bigger webs join the plate to the tab.
- The inserts sit in `frame_compact_connectors_RAmotor.ipt`, a **copy** of the frame with 5 internal bosses, coloured orange. `SelenoCompact_RAmotor.iam` is a copy of SelenoCompactAssembly that uses it, plus `base_plate_compact_orange.ipt` (an orange copy of the base plate). The originals are untouched.
- **Wrap strap** (`ra_wrap_strap_pz.ipt` + `ra_wrap_strap_mz.ipt`): two halves that clamp over the top of the RA body (r40, 12 mm wide).
  - They are joined on top with 1x M4x16 into an M4 insert in the -Z half.
  - Each half has a lug on one of the flat bit's axial M5s. Both stock M5x16 become **M5x25**.
  - On +Z the strap lug stacks on the bracket tab, so the bracket and strap share that screw. On -Z the lug is 12 mm thick on its own.
  - The strap can't pass under the RA body, because the latitude pivot web fills that space. That's why it goes over the top.
  - Each half is less than 180 degrees, so it fits on from the side without removing anything.
- The motor sits polar of the worm. The buttons, RA clutch lever, -Z knob and GX sockets all stay clear.

## Dec
- Main fixing: a **12 mm** tab (was 6) over the Dec worm carrier strip on the worm block's polar face.
  - The carrier's 2 cap screws become M5x25. Their heads sit in 10 mm counterbores, so the screw length is unchanged.
  - A 7 mm hole leaves the push grub (worm mesh) adjustable.
- The arm is 20 mm thick (was 8), with a corner gusset at the tab.
- The motor plate is 20 mm bigger on three sides of the motor, and runs up to fully join the arm. A 9 mm rim stiffens it.
- **Two bands** round the Dec housing:
  - The bracket's worm-side half band now runs y 47..70.5 (ring 1 + band 2 in one piece).
  - Ring 1's far half is `dec_clamp_cap.ipt` (2x M4x16).
  - Band 2's far half can't be a full ring, because the Dec clutch boss is in the way. It is 2x `dec_band2_cap.ipt` (the same part used twice, mirrored), each with 1x M4x16. They hook round the housing and stop 1.5 mm short of the boss.
- Tighten order: carrier screws first (without moving the worm mesh), then ring 1, then band 2.
- The web from band 2 to the arm stays below the belt (z < 51).

## Fillets
- 3 mm on inside corners, 1.5 mm on outside edges. They were applied by script on every printed part.
- Kept sharp on purpose:
  - screw holes, slots and counterbores
  - the strap's seat for the bracket tab
  - the rim base next to the motor
  - corners that touch the mount (flat bit, Dec worm block)
- A few edges where Inventor couldn't fit a fillet are also sharp: RA bracket 2, Dec bracket 4.

## Hardware (both axes)
- 2 motors
- 2x GT2 40T (6 mm bore), 2x GT2 16T (5 mm bore), 2x 158 mm GT2 6 mm loops
- 8x M3x8 (motors)
- RA: 5x M3x30 + 5x M3 inserts, 2x M5x25, 1x M4x16 + 1x M4 insert
- Dec: 2x M5x25, 4x M4x16 + 4x M4 inserts

Print in PETG or ASA with 4+ perimeters and 30%+ infill.

## Measure before printing (the mount model is from photos)
1. Dec housing diameter (model: 76 mm), and the gap between the Dec clutch boss and the worm block bottom (band 2 is 9 mm tall in a 10.5 mm gap).
2. RA body diameter at the flat bit's polar face (model: 80 mm; the strap clamps on it) and the flat bit axial M5 hole depth (model 17 mm; M5x25 engages 13 mm).
3. Brass stub length beyond the frame wall (RA, model: 7.8 mm) and beyond the nut (Dec, model: 11 mm). The pulley hub needs about 7 mm.
4. Clearance from the RA body to the frame's +Z wall inside, near the bosses (model: about 1 mm to spare).
5. Dec carrier screws: spacing (model 26 mm), size (model M5), and length.
6. Dec clutch lever to Dec housing surface (model: the rings clear it by about 3 mm).
