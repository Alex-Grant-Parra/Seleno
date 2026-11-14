# CG-5 motor drive (NEMA 17, belt) - brackets v2

Top assemblies:
- `..\CG5_SelenoCompact_Motors.iam`: CG-5 + SELENO compact + both drives. 0 interferences.
- `..\CG5_SelenoCompact_Motors_C8N.iam`: the same, plus the C8-N OTA. 0 interferences.

v2 (2026-11-04) follows a mechanical engineer's review: more support on both axes, a wrap strap on the RA, a second band on the Dec, and fillets on every printed part. The SELENO frame and base plate are orange in these assemblies. v1 files are in `..\OldVersions_before_bracket_v2\`.

**2026-11-09: no more heat-set inserts.** Every joint that screwed into an insert is now a through bolt with a hex nut in a printed pocket. See "Fasteners" below. Files from before the change are in `..\OldVersions_before_nuts_bolts\`.

**2026-11-10: RA bracket v3 (from the stress analysis).** Report: `..\..\reports\SELENO_Drive_Stress_PLA_PETG.html`. Files from before the change are in `..\OldVersions_before_ra_plate_v3\`.
- The motor plate is 6.5 mm thick, up from 5. A 1.5 mm pad on the pulley side (z 62..63.5) covers the whole plate outline and keeps the slots and the centre hole. It was sketched 1.5 mm inside the plate, so it fills the old edge fillets rather than leaving a groove. The belt clears it by 2 mm.
- The corner beside the +x motor slot is filled out. It was a 2 mm ligament between the slot end and the plate edge, which was the knock-case hot spot. The same feature backs bolt #4's head seat, which used to graze the plate edge and leave a 0.1 mm knife edge.
- The RA motor screws are now **M3x10**, with the heads on the pad. Thread engagement in the motor goes from 3.0 to 3.5 mm. The Dec motor keeps M3x8.
- 0 interferences in both top assemblies. The bracket is 70.3 cm³, up from 63.3.

**2026-11-11: Dec housing is 80 mm, not 76.** The printed Dec clamp cap would not go round the Dec axis; the RA straps (80 mm bore) do. Files from before the change are in `..\OldVersions_before_dec80\`.
- Mount model: `dec_housing.ipt` Dec housing and `saddle_vixen.ipt` Dec shaft flange, r38 -> r40.
- Dec bracket half band, `dec_clamp_cap.ipt` and `dec_band2_cap.ipt`: bore 76 -> 80 mm, outside 86 -> 90 mm, so the ring wall stays 5 mm. The bolt positions (z ±48) are unchanged.
- The bigger ring rises above the cap ear faces where the M4 heads sit, so each cap bolt has an 8 mm spot face.
- One bracket round (17 edges) lost its edges in the change and was re-applied on the new edges.
- 0 interferences in CG5_Mount, both motor top assemblies and the C8N top. The clamp cap is now 2.0 mm from the Dec clutch lever (was about 3).

## Drive train (both axes identical)
- Motor: SIMAX3D NEMA 17 42x38, 1.8 deg, 1.5 A, 5 mm D-shaft
- GT2 16T pulley (5 mm bore) on the motor, GT2 40T pulley (6 mm bore) on the 6 mm brass worm stub, hub facing the mount, grub screw on the D-flat
- GT2 6 mm closed belt, 158 mm, at 50.4 mm centres. The motor slots give ±4 mm, which also fits 152 or 160 mm loops.
- 2.5:1 belt x 144:1 worm = 360 motor turns per axis turn. At 1/16 microstepping that is 1.125 arcsec per microstep.

## RA
- `ra_motor_bracket.ipt` bolts onto the SELENO frame's +Z wall above the buttons with **3x M3x30** through the wall, into M3 nuts on the inside.
  - The foot block now runs x 22..60 along the wall.
  - The motor plate is about 20 mm bigger on the polar side and below the motor.
  - A 9 mm rim stiffens the plate on three sides of the motor. The bolt heads sit in counterbores through the rim, 1.5 mm deeper than before so the M3x30 tips end flush with the inside of the frame block.
  - Bigger webs join the plate to the tab.
- The nuts sit in `frame_compact_connectors_RAmotor.ipt`, a **copy** of the frame with a 6 mm bolt block inside the +Z wall, coloured orange. `SelenoCompact_RAmotor.iam` is a copy of SelenoCompactAssembly that uses it, plus `base_plate_compact_orange.ipt` (an orange copy of the base plate). The originals are untouched.
- 2026-11-11: the frame copy's back side (the flat-bit side with the M4 locking screw) is 4 mm deeper. The lower back wall is now at part z -34..-32 and the ledge runs z -34..-23. The M4 hole moved from z -26.5 to -30.5, with the screwdriver groove, USB notch and base hole B moved too. The dovetail lips/rails did not move. The base plate copy grew 4 mm to match, and its USB socket bracket and tab B moved with the wall. The flat bit's M4 hole and screw in the mount model moved 4 mm toward the polar end. Backup: `..\OldVersions_before_back_ext\`.
- 2026-11-11: handling rounds on the frame and base copies ("Handling rounds: ..." features at the end of each tree). R2 on the vertical outer corners of both parts, so the joint lines up. Frame: R1/R0.6 on the top rim and RA notch, R1 on the lip outer edges, an R0.6 lead-in on the lip tops, R0.5 on the lip ridges, R1 on the ledge back, R0.8 on the screen window. Base: R1 round the bottom. Left sharp: the frame/base joint, the dovetail contact faces, the worm U-slots, holes and cut-outs, and the RA bolt block.
- 2026-11-11: the 6 button holes on the -x wall are now full 16 mm circles; the anti-rotation flat 7 mm below each centre is gone (feature "Enc_ButtonHoles full round").
- 2026-11-11: the M4 flat-bit screw hole is now a slot, 4 mm wide, running front to back across the ledge, with centres from z -30.8 to -26.5 (4.3 mm of travel). The back end clears the screwdriver groove by 0.2 mm; the front end leaves 1.5 mm of ledge. With the 7 mm pan head, the head touches the back wall when the screw is behind about z -28.5.
- **Wrap strap: removed (2026-11-12).** The two strap halves (`ra_wrap_strap_pz/mz.ipt`) are no longer used. They sat on the polar side of the flat bit, where the real RA body tapers down to the polar scope; the model had a plain 80 mm cylinder there, so the printed straps didn't fit. The only cylinder on the RA body is between the Dec junction and the seam ring, and that part turns with RA, so nothing can clamp to it.
  - +Z flat-bit screw: **M5x20** (was M5x25 through strap + tab). The head now sits straight on the bracket tab (6 mm), 14 mm thread engagement in the 17 mm hole. Fit an M5 washer under the head.
  - -Z flat-bit screw: the stock M5x16 goes back in.
  - The strap top M4x20 + nut are gone.
  - FEA re-run without the straps (PLA, same loads as the 2026-11-10 report): bracket peak 5.8 MPa at stall and 17.5 MPa in a 50 N knock (both unchanged), frame joint shear 62 -> 63 N against 177 N friction (margin 2.8), tab-to-flat-bit load 81 N at stall (was 88). The straps carried very little; the bracket is held by the 3 frame bolts, the dovetail and the tab.
  - Backup of the strap parts, STLs and the assemblies before the change: `..\OldVersions_before_no_straps\`.
- The motor sits polar of the worm. The buttons, RA clutch lever, -Z knob and GX sockets all stay clear.

## Dec
- Main fixing: a **12 mm** tab (was 6) over the Dec worm carrier strip on the worm block's polar face.
  - The carrier's 2 cap screws become M5x25. Their heads sit in 10 mm counterbores, so the screw length is unchanged.
  - A 7 mm hole leaves the push grub (worm mesh) adjustable.
- The arm is 20 mm thick (was 8), with a corner gusset at the tab.
- The motor plate is 20 mm bigger on three sides of the motor, and runs up to fully join the arm. A 9 mm rim stiffens it.
- **Two bands** round the Dec housing:
  - The bracket's worm-side half band now runs y 47..70.5 (ring 1 + band 2 in one piece).
  - Ring 1's far half is `dec_clamp_cap.ipt` (2 bolts).
  - Band 2's far half can't be a full ring, because the Dec clutch boss is in the way. It is 2x `dec_band2_cap.ipt` (the same part used twice, mirrored), each with 1 bolt. They hook round the housing and stop 1.5 mm short of the boss.
  - The 4 cap bolts go through the caps and the bracket into M4 nuts on the bracket side: M4x25 on +Z (the bracket is 15.75 mm thick there), M4x16 on -Z (6.25 mm ear). Ring 1 nuts sit in closed hex pockets. Band 2 nuts sit in slots open at the top, because band 2 is only 9 mm tall.
- Tighten order: carrier screws first (without moving the worm mesh), then ring 1, then band 2.
- The web from band 2 to the arm stays below the belt (z < 51).

## Fillets
- 3 mm on inside corners, 1.5 mm on outside edges. They were applied by script on every printed part.
- Kept sharp on purpose:
  - screw holes, slots and counterbores
  - the rim base next to the motor
  - corners that touch the mount (flat bit, Dec worm block)
- A few edges where Inventor couldn't fit a fillet are also sharp: RA bracket 2, Dec bracket 4.

## Hardware (both axes)
- 2 motors
- 2x GT2 40T (6 mm bore), 2x GT2 16T (5 mm bore), 2x 158 mm GT2 6 mm loops
- 4x M3x10 (RA motor, since v3) + 4x M3x8 (Dec motor)
- RA: 3x M3x20 + 3x M3 nuts, 1x M5x20 + M5 washer (+Z flat bit); the -Z flat-bit screw stays the stock M5x16
- Dec: 2x M5x25, 2x M4x25 + 2x M4x16 + 4x M4 nuts

Print in PETG or ASA with 4+ perimeters and 30%+ infill.

**2026-11-11: electronics on the base ("sled").** The PCB and the Pi now both sit on the base plate, so the whole electronics unit comes out of the bottom of the frame in one piece. The Pi still plugs straight into J7, and the Pi-to-USB-socket cable is unchanged. Files from before the change are in `..\OldVersions_before_sled\`.
- Base copy: 3 PCB posts up to the PCB underside (y -15.52), each with a 2.7 hole and an M2.5 nut slot open toward the middle. The 4th PCB hole is right over the Pi's corner hole (1 mm apart), so it has no post; J7 on the Pi header holds that corner.
- Frame copy: the 4 PCB hangers, their holes and nut pockets are suppressed. The Pi SD card slot is cut down to the bottom edge, because the card sticks into the wall; the base plate closes the slot from below.
- To remove it: unplug the outside USB cable, take out the 4 M3 base bolts, lower the unit, unplug the JST leads on top of the PCB (buttons, screen, IMU, motors, power). Checked: lowering it straight down by up to 130 mm hits nothing.
- Post support: the R screw tab is 1.8 mm thicker so the post head next to it sits fully on it (the M3 nut pocket is deeper; push the nut all the way in). The post by the USB socket is solid under its head, with a small clearance hole for the socket bolt tip.
- The panel parts' leads need enough slack to lower the unit a few cm before unplugging them.

## Fasteners: nuts instead of heat-set inserts (2026-11-09)
Into the motor's own threads: M3x10 x4 on the RA (v3, 2026-11-10) and M3x8 x4 on the Dec. Into the mount's threads: M5x25 x2 (Dec carrier), M5x20 x1 (RA flat bit +Z, since 2026-11-12). Unchanged: M2x5 self-tapping x4 (LCD).

| Joint | Bolt | Nut | Clamped length | Notes |
|---|---|---|---|---|
| RA bracket to frame | 3x M3x30 SHCS | 3x M3 | 25.6 | nut pocket 4.4 deep, tip flush with the block |
| Dec ring 1 + band 2, +Z | 2x M4x25 SHCS (was M4x16) | 2x M4 | 19.8 | tip 1.5 mm past the bracket |
| Dec ring 1 + band 2, -Z | 2x M4x16 SHCS (same) | 2x M4 | 10.3 | tip 2 mm past the bracket, as before |
| Base plate to frame | 4x M3x10 pan (was M3x6) | 4x M3 | 4.8 | tabs thickened 3 to 5.5 mm, tip 2.3 mm inside the case |
| PCB to base posts (2026-11-11) | 3x M2.5x8 pan, from the top | 3x M2.5 | 2.8 | nut in a side slot 1.2 mm under the PCB; the 4th PCB hole is over the Pi's corner hole, so J7 holds that corner |
| Pi to base | 4x M2.5x8 pan (was M2.5x6) | 4x M2.5 | 4.0 | nut under the base; tip 0.9 mm below the base |
| USB panel socket | 2x M2.5x10 countersunk (was M2.5x5 pan) | 2x M2.5 | 5.5 | head flush in the bracket's outer face, nut on the socket flange |

Totals: 7x M3, 4x M4 and 9x M2.5 hex nuts (ISO 4032). Clamped length = head face to nut face, in mm.

Holes: 3.2 mm for M3, 4.2 mm for M4, 2.7 mm for M2.5. Nut pockets are the nut's across-flats + 0.2 mm (5.7 / 7.2 / 5.2), and at least nut height + 0.5 deep. Some are deeper so the bolt doesn't stick out where it would hit something.

Fitting the nuts:
- RA frame block: press the 3 nuts into the pockets on the inside of the +Z wall before sliding the frame onto the flat bit. The pockets face the RA body.
- PCB (on the base, 2026-11-11): bolt the Pi down first, slide the 3 M2.5 nuts into the side slots of the base posts (slots face the middle), push the PCB's J7 onto the Pi header, then bolt the PCB down from the top.
- Base: push the nuts into the pockets on the inner face of the 4 tabs, then bolt through the skirt wall.
- Pi: nuts go in the pockets under the base plate; the base has 3.7 mm radius feet under the standoffs to back the pockets.
- USB: fit the countersunk bolts from outside the socket bracket before the base goes into the frame, because the frame wall covers the heads afterwards.
- Dec: put the nuts in the bracket first. The band 2 nuts slide down into their slots.

Why the PCB and USB joints differ from a plain "nut underneath": the PCB posts are short and crowded (USB socket bolt, screw tabs), so the nut sits in a side slot near the top; the USB socket bracket sits 0.2 mm from the frame wall, so there's no room for a nut on that side.

## Measure before printing (the mount model is from photos)
1. Dec housing diameter: 80 mm (user, 2026-11-11). Still to check: the gap between the Dec clutch boss and the worm block bottom (band 2 is 9 mm tall in a 10.5 mm gap).
2. RA body diameter at the flat bit's polar face (no longer needed, the straps are gone) and the flat bit axial M5 hole depth (model 17 mm; the M5x20 engages 14 mm, 13 with a washer).
3. Brass stub length beyond the frame wall (RA, model: 7.8 mm) and beyond the nut (Dec, model: 11 mm). The pulley hub needs about 7 mm.
4. Clearance from the RA body to the frame's +Z wall inside, near the bolt block (model: about 1 mm to spare). The M3 nuts sit 2 mm deep in the block, so nothing sticks out toward the RA body.
5. Dec carrier screws: spacing (model 26 mm), size (model M5), and length.
6. Dec clutch lever to the clamp cap (model: 2.0 mm since the 80 mm change). Swing the lever through its travel with the cap fitted.
