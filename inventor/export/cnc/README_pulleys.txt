SELENO CG-5 drive: GT2 pulleys to machine (all aluminium, 6061-T6 or 7075)
==========================================================================

Files (STEP = master geometry, mm; STL for viewing only). Each STEP has 2 bodies:
the pulley, and its separate 1 mm outer flange disc.

  RA_worm_pulley_40T_CNC.step   make 1   40T, 6 mm bore, long hub 19 dia x 16
  Dec_worm_pulley_40T_CNC.step  make 1   40T, 6 mm bore, hub 20 dia x 7
  Motor_pulley_16T_CNC_x2.step  make 2   16T, 5 mm bore, hub 13 dia x 7

Layout (along the axis, from the hub end):
  hub -> 1 mm hub-side flange (integral) -> 7 mm toothed section -> 1 mm spigot
  The outer flange disc slides onto the spigot and is fixed with retaining
  compound (Loctite 638/603). It is separate so the teeth can be cut straight
  down from the open end on a 3-axis mill, stopping on the integral flange.

Tooth form: GT2 2 mm pitch (2GT / 2MGT), cut the same along the whole 7 mm
  40T: pitch dia 25.465, OD 24.957, root dia 23.429
  16T: pitch dia 10.186, OD 9.678,  root dia 8.150
  Groove: depth 0.764 from OD, width at OD 1.494, bottom radius 0.555,
          straight flanks tangent to the bottom radius.
  Cutter: 1.0 mm flat or ball end mill (smaller is fine).
  Leave the tooth tips sharp, or break them lightly (max 0.1).

Tolerances that matter for tracking accuracy:
  Bore: 6.00 / 5.00 +0.01 / +0.03 (slide fit, no wobble).
  OD and teeth concentric to the bore within 0.02 mm TIR. This is the number
    that sets the tracking error: cut the bore and the teeth in one setup,
    or cut the teeth on a mandrel through the finished bore.
  Groove spacing: equal (40 or 16 positions), +-0.1 deg.
  Faces square to the bore within 0.05.

Grub screws: 2x M3 radial per pulley, 90 deg apart (tap drill 2.5, tap M3).
  RA worm pulley: 4.5 mm from the hub end (the hub reaches over a 9 mm brass
  stub; one grub goes on the shaft's flat). Others: 3.5 mm from the hub end.

Fitting: the RA worm pulley goes on hub-first. Its hub passes through the
frame's U-slot, so tighten the grubs before fitting the frame.
