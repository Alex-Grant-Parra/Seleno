# CG-5 motor drive (NEMA 17, belt)

Top assembly: `..\CG5_SelenoCompact_Motors.iam` (CG-5 + SELENO compact + both drives, 0 interferences).

## Drive train (both axes identical)
- Motor: SIMAX3D NEMA 17 42x38, 1.8 deg, 1.5 A, 5 mm D-shaft
- GT2 16T pulley (5 mm bore) on the motor, GT2 40T pulley (6 mm bore) on the 6 mm brass worm stub, hub facing the mount, grub screw on the D-flat
- GT2 6 mm closed belt, 158 mm, at 50.4 mm centres. The motor slots give ±4 mm, which also fits 152 or 160 mm loops.
- 2.5:1 belt x 144:1 worm = 360 motor turns per axis turn. At 1/16 microstepping that is 1.125 arcsec per microstep. Tracking speed is 0.25 rpm at the motor; about 600 rpm at the motor slews about 10 deg/s.

## RA
- `ra_motor_bracket.ipt` screws onto the SELENO frame's +Z wall above the buttons: 3x M3x30 into M3 heat-set inserts (4.2 mm holes, 7.5 deep).
- The inserts sit in `frame_compact_connectors_RAmotor.ipt`, a **copy** of the frame with 3 internal bosses added. `SelenoCompact_RAmotor.iam` is a copy of SelenoCompactAssembly that uses it. The originals are untouched.
- A tab also reaches down to the flat bit's +Z axial M5 on its polar face. Swap the stock M5x16 for an M5x20 through the tab. The tab hole is 6 mm so it lines up with the frame screws.
- The motor sits polar of the worm. Buttons, the RA clutch lever, the -Z knob and the GX sockets all stay clear.

## Dec
- Main fixing: a 6 mm tab over the Dec worm carrier strip on the worm block's polar face. The carrier's 2 cap screws become M5x25 through the tab. A 7 mm hole leaves the push grub (worm mesh) adjustable. Retighten the carrier screws without shifting the worm mesh.
- The clamp ring stays as extra support. Tighten it last.
- `dec_motor_bracket.ipt` + `dec_clamp_cap.ipt` form a split ring around the Dec housing (76 mm in the model), between the Dec junction and the Dec clutch boss. They are held with 2x M4x16 into M4 heat-set inserts.
- The motor hangs straight below the Dec worm on the +Z side, clear of the saddle and of anything fixed during RA rotation.

## Hardware
2 motors; 2x GT2 40T 6 mm bore; 2x GT2 16T 5 mm bore; 2x 158 mm GT2 6 mm loops; 8x M3x8 (motors); 3x M3x30 + 3x M3 inserts + 1x M5x20 (RA); 2x M4x16 + 2x M4 inserts + 2x M5x25 (Dec). Print the brackets in PETG or ASA with 4+ perimeters.

Mount model changes for this (originals backed up in `..\OldVersions_before_motor_edits\`): added the -Z flat-bit axial M5, and the Dec worm carrier (pocket, strip, 2x M5x16, push grub) from photo 173116.

## Measure before printing (the mount model is from photos)
1. Dec housing diameter (model: 76 mm) and the worm-shaft-to-Dec-axis offset (model: 36 mm).
2. Brass stub length beyond the frame wall (RA, model: 7.8 mm) and beyond the nut (Dec, model: 11 mm). The pulley hub needs about 7 mm.
3. Clearance from the RA body to the frame's +Z wall inside, near the new bosses (model: about 1 mm to spare).
4. Dec carrier screws: spacing (model 26 mm), distance from the worm shaft (model 6 mm toward the saddle), size (model M5), and length.
5. Dec clutch lever to Dec housing surface (model: the ring clears it by about 3 mm).
