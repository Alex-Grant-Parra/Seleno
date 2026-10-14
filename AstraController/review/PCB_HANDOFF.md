# SELENO v0.3 PCB build — handoff (2026-10-13, updated 2026-10-14)

## STATUS 2026-10-14 (latest): board installed, 6 nets left for Alex to route by hand
- **Installed:** `AstraController/ASTRA.kicad_pcb` is the new SELENO v0.3 board.
- **2026-10-14, later:** Alex asked for no F.Cu/B.Cu GND pours. They were removed from the board and from `planes.py`. Only In1 (GND) and In2 (power islands) are filled now.
  - Old board: git HEAD 201f042, plus a scratch backup.
  - `ASTRA.kicad_pro` now has the net classes (Default, Power, HV12, Motor, BuckSW, USB), their patterns and the new DRC minimums (0.15 clearance, 0.3 edge, 0.45/0.2 via).
- **DRC with schematic parity:**
  - 0 errors, 0 parity issues;
  - 13 warnings, all `lib_footprint_mismatch` on the test pads (their silk rings were moved to F.Fab so the net labels fit);
  - 6 unconnected.
- **Left for Alex** (he routes these in KiCad):
  - SCL: U5 pin 8 to the existing SCL B.Cu track;
  - TMC_UART: R16.1 to U5 pin 31;
  - MOSI: S1.4 to U5 pin 16;
  - DC: S1.6 to U5 pin 23;
  - 1EN: R35.2 to U5 pin 33;
  - UART2: U5 pin 34 to its B.Cu stub.
  - All six are in the ESP32 channels, which the autorouter left congested.
- **To rebuild the board from scripts:**
  1. `scratchpad/full.sh` runs build → route (about 3 min, time-boxed) → apply → DRC.
  2. `scratchpad/final.sh` runs build → apply `routes.json` → dangling cleanup → `finish.py` (silkscreen + stackup) → DRC.
  3. Copy `pcbwork/ASTRA.kicad_pcb` into the project.
  - Don't regenerate once Alex has hand-edited the board in KiCad: the scripts start from scratch.
- **Silkscreen:**
  - The USB note "PI:1-4 CAM:5-8" means pins 1–4 (Pi) and 5–8 (camera). Check against the CUI UJ2-ADH-TH drawing which physical port that is.
  - Passive and test-pad references are on F.Fab only. 13 other references had no clean spot and are hidden too.

## Update 2026-10-14 (read this first)
- Placement is finished and DRC-clean. Changes from the plan below:
  - R35/R36 (ENN pull-ups) moved to the ESP32 end of 1EN/2EN. R35 is at (70.47, 28.1); R36 is at (62.85, 6.15) and feeds from J5 pin 1.
  - C28/C30 moved to block x 3.7.
  - TP1 (+12V) moved to (1.7, 34) and TP5 (GND) to (1.7, 36.6), by the TVS. TP2/TP4 (+5V/GND) are at the left edge by the USB. TP3 (+3V3) is at (83.45, 12.8).
- New scripts in `pcb_build/`:
  - `rules.py`: net classes, plus writing them into `.kicad_pro`.
  - `field.py`: exact clearance fields on a 0.05 mm grid.
  - `ctx.py`: routing context.
  - `conn.py`: connectivity.
  - `planes.py`: zones, In2 islands and the ESP32 F.Cu/via keep-out.
  - `hand.py`: hand routes for the drivers, bucks, 12 V input, U9 feed (B.Cu), USB pairs, crystal and hub-west escapes, and the button bus.
  - `route.py`: EP vias, plane fanout, then a signal A* router with rip-up and history cost.
  - `apply.py`: writes `routes.json` into the board and fills zones.
  - `finish.py`: stackup (JLC04161H-7628) and silkscreen.
  - `dbg.py`: debug helpers.
- In2 layout: +3V3 everywhere at priority 0, with +12V (input, left column, both drivers, C1) and +5V (USB area plus a corridor to L2/C37/C38) as priority-1 islands. A +3V3 band at v 33–36.5 feeds the drivers' VCC_IO.
- Scratch folder for this session: `%TEMP%\claude\z--ASTRA\eb408975-...\scratchpad` (`common.SCR`; override with the `SELENO_SCR` environment variable). `full.sh` there runs build → route → apply → DRC → plot.
- Router lessons:
  - Weighted A* (w 1.5) is needed for long nets.
  - F.Cu must prefer vertical runs and B.Cu horizontal; otherwise long F.Cu runs wall off the ESP32 bottom pins.
  - Route the longest nets first.

Start here in a new chat. Read this file, then `review/REVIEW_LOG.md`. The scripts are in `review/pcb_build/`.

## What Alex asked for (all still in force)
- **Wipe `ASTRA.kicad_pcb` and build a new board from scratch.** Do not touch the schematic.
- **Board size: exactly 85 × 50 mm, non-negotiable.** Keep the old outline: KiCad coordinates (76.5, 45.5) to (161.5, 95.5).
- **Stackup:**
  - 4 layers: F.Cu (top), In1.Cu (GND plane), In2.Cu (power), B.Cu (bottom).
  - 1.6 mm thick.
  - In2 is for power only; no signals on it.
- **All components on the TOP side, no passives on the bottom.** B.Cu is free for routing.
- **ESP32-WROOM-32UE:** external antenna, so no RF keep-out zone.
- **Track widths by net class:**

  | Class | Track | Clearance / gap |
  |---|---|---|
  | Signals | 0.2 mm | 0.15 mm |
  | +3V3 / +5V / GND | 0.4 mm | |
  | 12 V path | 1.0 mm | |
  | Motor phases and sense | 0.6 mm | |
  | Buck switch nodes | 0.8 mm | |
  | USB pairs | 0.25 mm | 0.15 mm gap (≈90 Ω on JLC04161H-7628) |

  Vias are 0.5/0.3 mm generally and 0.45/0.2 mm where tight. Write these classes into `ASTRA.kicad_pro` `net_settings` at install time. The existing classes there (+V12, +V5) have no patterns.
- **LEDs near the edge.** Connectors near the edges where possible.
- **Test pads.** Use board-only `TestPoint_Pad_D1.0mm` footprints.
- **M2.5 mounting holes** near the four corners at (3.5, 3.5) from each corner. Board-only `MountingHole_2.7mm_M2.5`.
- **Silkscreen text: "SELENOv0.3"** (big, F.SilkS).
- **Keep it neat.**
- **Standing rules:**
  - No firmware or software unless asked.
  - Commit only when asked.
  - Don't contact GitHub.

## Status
- **Nothing has been installed yet.** The real `AstraController/ASTRA.kicad_pcb` is untouched; the old version is also in git HEAD (201f042).
- **Work happens in a scratch copy of the project.** It contains `ASTRA.kicad_pro`, all `.kicad_sch` files, `fp-lib-table` and `ASTRAfootprints.pretty`. The old scratch folder was `%TEMP%\claude\z--ASTRA\a7d97d99-...\scratchpad\pcbwork`. Recreate it if it's gone, by copying those files from `AstraController/`.
- **Placement is about 85% done** (`place.py`). The last DRC still flags these, to fix next:
  - **Courtyard overlaps:** C30/U8, C28/U7 (charge-pump cap too close to the driver; move it right ~0.3 mm), D6/F1, U9/C40, U6/C36 (buck boost cap to IC; move it up ~0.3 mm), C15/C16, C44/R18, C25/J4.
  - **Clearance:** C15 pad to C16 pad.
- **Not started yet:**
  - planes and zones;
  - fanout vias;
  - hand routes for the critical areas;
  - autorouter;
  - silkscreen cleanup;
  - stackup text in the file;
  - netclasses in `.kicad_pro`;
  - install into the project.
- `pcb_build/place2.png` is the current placement render: courtyards, pads coloured by net, yellow ratsnest.

## Tooling
- **KiCad Python** (has pcbnew 10.0.5, numpy and PIL; no scipy or matplotlib): `C:\Users\alexg\AppData\Local\Programs\KiCad\10.0\bin\python.exe`. Run every board script with it.
- **kicad-cli** is in the same `bin` folder.
  - `pcb drc --severity-all --format json [--schematic-parity] [--refill-zones --save-board]`
  - `pcb render --side top` (3D PNG)
- **Netlist:** `kicad-cli sch export netlist -o astra.net ASTRA.kicad_sch`. The scripts expect it at `<SCR>/final/astra.net`.
- **Scripts** in `pcb_build/`:
  - `common.py`: paths, `rot()` using KiCad's rotation convention, `load_netlist()`.
  - `place.py`: all positions in board-local mm. u is right and v is down, origin at the top-left board corner; KiCad absolute = (76.5+u, 45.5+v). It contains block templates for the drivers and bucks, plus `HOLES` and `TESTPADS`.
  - `build.py`: builds the board from the netlist and `place.py`, links symbol paths, sheets and BOM flags, and writes `geom.json`.
  - `plot.py`: draws `geom.json` (plus an optional routes JSON) to PNG. Use `opts` "r" for ratsnest.
  - `fpsizes.py`, `paddump.py`: dump footprint geometry.
- **Path fix needed after the move:** `common.py` sets `SCR` to the parent of its own folder. It imports `parse_net` from there and expects `final/astra.net` and `pcbwork/` there. Now that the scripts sit in `review/pcb_build/`, either point `SCR` at the new scratch folder or copy `parse_net.py` and the netlist accordingly.
- **Stackup:** the API doesn't expose it (`GetStackupDescriptor` isn't wrapped). Add the `(stackup ...)` block to the saved file by text, using the old board's file (git HEAD) as a template.

## Floorplan (board-local u, v)

### Top edge (vertical SH connectors at rot 0, pins facing inward at v 4.07)
| Part | u range | Notes |
|---|---|---|
| J6 USB-A stacked, rot −90, anchor (19.0, 13.55) | 6.6–24.4 | Opening on the top edge; body courtyard down to v 18.9 |
| J4 Qwiic | 31.2–39.1 | |
| J3 Qwiic | 39.3–47.2 | |
| S1 LCD | 47.4–59.3 | |
| J5 IMU | 59.5–72.4 | |
| SW7 reset | 72.5–78.4 | |

The top edge between u 24.4 and 31.2 holds C25, TP11 and TP12.

### Bottom edge
- J1 at u 6.6–21.6 and J2 at u 22–37.
- Button connectors (rot 180, pins up), left to right, ordered to match the MCP pins: SW5 SELECT, SW6 RETURN, SW1 TOP, SW2 BOTTOM, SW3 LEFT, SW4 RIGHT, spanning u 37.4–74.7.

### Left edge
- P1 at rot 90, (4.45, 29.0): +12V pin at v 25.04, GND at v 29.0.
- D7 TVS at (4.5, 36).
- Test pads TP1–TP5 in a column at u 2.6.

### Right edge
- LEDs D4, D5, D3, D2, D1 at u 82.4, v 30–40, with their resistors to the west.
- Test pads TP6–TP10 at u 83.2.
- ESP32 support parts at u ≈ 80.6–81.6.

### Interior
- **ESP32 U5** at rot −90, (68.41, 16.79); courtyard u 58–78.3, v 7–26.6.
  - Top-facing pins (v 8.04): pin 1 GND at u 76.82 down to pin 14 at u 60.31, 1.27 mm pitch.
  - Bottom-facing pins (v 25.54): pin 25 IO0 at u 60.31 up to pin 38 at u 76.82.
  - Left-facing pins (u 59.06): pin 15 at v 11.08 to pin 24 at v 22.51.
- **MCP23017 U1** at rot 180, (70.5, 36.5). Its button pins face down toward the bottom edge; RED/GREEN/WHITE face right toward the LEDs.
- **Driver blocks** (`place.driver()`): U7 at (14.6, 38.05) and U8 at (30.0, 38.05), drivers rotated 180 so the motor side faces down to J1/J2.
  - Motor-side pins (v cy+2.45): VS −1.5, BRA −1.0, OA1 −0.5, NC 0, OB1 +0.5, BRB +1.0, VS +1.5.
  - OA2 is at the bottom of the left side and OB2 at the bottom of the right side.
  - Logic side: STEP/DIR/VCC_IO/VREF on the left side; UART/DIAG/MS/5VOUT on the top.
  - Sense resistors in 1206 at (±3.03, 4.6).
  - **OA1 must hop to B.Cu** (via below its pad, then to J1 pin 1). This is the only crossing, because J1's pin order is OA1, NC, OA2, OB1, NC, OB2.
  - VS pins go to In2 +12V by vias at about (∓2.15, 3.3). OA2 and OB2 run down at u ∓3.0, between the sense-resistor pads.
- **C1** (680 µF THT) at anchor (41.0, 38.6).
- **Buck template** (`place.buck()`):
  - IC rot 0 (west pins: GND, SW, VIN; east pins: BST, EN, FB).
  - Inductor to the west, rot 180.
  - Cin (1206) south, with its +12V pad under VIN.
  - BST cap north at (0, −2.1); route SW→BST around the north of the GND pin; GND pin via-in-pad or under the body.
  - FB resistors to the east; output caps south of the inductor.
  - 5 V buck U6 at (45.0, 25.5); 3.3 V buck U9 at (59.6, 34.0).
  - EN divider R31/R32 at (48.8, 22–23).
- **Input protection:** F1 at (14.6, 26.5) rot 180, Q1 at (21.5, 26.5) rot 180 (drain west), D6 and R21 below Q1.
- **USB hub area** (hub U2 at rot 90, (35.5, 14.0)):
  - West pins (u 32.735): SUSP 12.0, VDDA 12.5, UP− 13.0, UP+ 13.5, XO 14.0, XI 14.5, PLLFILT 15.0, RBIAS 15.5, VDDA 16.0.
  - Bottom pins (v 16.765): DN1− 33.5, DN1+ 34.0, DN2− 34.5, DN2+ 35.0, VDDA 35.5.
  - Top pins (v 11.235): VBUS_DET 33.5, RESET_N 34.0, HS_IND 34.5, SCL 35.0, VDD33 35.5, SDA 36.0.
  - East pins (u 38.265): PRTPWR2 13.0, VDD33 13.5, CRFILT 14.0, PRTPWR1 15.0, TEST 15.5 (stub to the centre pad), VDDA 16.0.
- **CH340 U3** at rot 90, (47.0, 15.0). Bottom pins (v 17.475): TXD 43.825, RXD 45.095, UD+ 47.635, UD− 48.905. Top pins: RTS 45.095, DTR 46.365.
- **Q3** (RTS→EN) at (55, 13); **Q2** (DTR→IO0) at (55, 17).

## USB routing plan (worked out; D+/D− order is topological)
- **Pi pair (port 1, pins 2/3 at v 13.55) runs on B.Cu.**
  - Each line drops separately through the gaps between the camera-port pins (u 13.25 and 17.75), then they merge.
  - Route south to v ≈ 19.8, east, north at u ≈ 25, east at v ≈ 7.5, then south.
  - Via up to F.Cu at (29.15, ~8.2) and (31.05, ~8.2).
  - Into U10 (rot 90 at (30.1, 10.6), flow-through north to south).
  - D− turns east at v 13.0 and D+ at v 13.5, into the hub's west pins.
- **Camera pair (port 2, v 16.17) runs on F.Cu.**
  - South to v ≈ 22.5/22.9, east through U11 (rot 180 at (26.9, 23)).
  - East to u ≈ 33.75, then north into DN1 (bottom pins).
- **DN2 pair:** south from the hub's bottom pins, east at v ≈ 20, north into the CH340's UD pins. TXD/RXD drop to B.Cu with vias at v ≈ 19.2.
- **VBUS_CAM:**
  - B.Cu from J6 pin 5 (19.0, 16.17), south to v 18.9, east.
  - Via at about (23.9, 18.9), then F.Cu to F2 pad 2 (F2 at (22.3, 20.3)).
  - F2's +5V pad gets a via at about (20.9, 21.5), clear of the port 1 B.Cu run at v 19.6–20.0 and the port 2 F.Cu run.
- **Crystal Y1** (rot 90 at (28.5, 16.3); XO top-left, XI bottom-right):
  - XO: up to v 14.0, then east.
  - XI: east from its pad, north at u ≈ 30.9 to v 14.5, then east.
  - PLLFILT, RBIAS and VDDA36 escape west into columns at u 31.35, 31.75 and 32.15 heading south, to C19 and R9.
- **Hub pin vias:**
  - VDDA29 via (0.45/0.2) at about (31.7, 12.5).
  - SUSP column at u 31.75, going north to R13.
  - VBUS_DET: short stub west to a via at about (32.4, 10.5).

## Plane plan
- **In1:** solid GND.
- **In2 islands:**
  - +12V over the power input, drivers, C1 and both buck inputs (bottom-left and centre);
  - +5V over J6, the USB/ESD area and the 5 V buck output;
  - +3.3V over everything else.
  - Keep In2 continuous under the B.Cu USB pair.
- **F.Cu and B.Cu:** GND pours with stitching vias.
- **Pads:** every GND/power pad gets a fanout via. THT pins join inner zones automatically.
- **Exposed pads:** the hub and MCP pads need via grids. The TMC and ESP32 footprints already include vias.

## Next steps
1. Fix the 8 overlaps above, re-render, and confirm placement visually.
2. Hand-routes as polylines: driver blocks ×2, bucks ×2, power input, USB pairs, crystal and hub escapes.
3. Fanout vias for plane nets.
4. Write the grid A* router: 0.1 mm grid, F.Cu and B.Cu, vias, rip-up and retry; output tracks and vias as JSON; a finish script adds them via pcbnew.
5. Zones; then `kicad-cli pcb drc --refill-zones --save-board --schematic-parity`. Iterate to 0 errors.
6. Silkscreen: hide passive refs, label connectors (12V+, M1/M2, PI/CAM, LCD, IMU, QWIIC, button names, RST), add "SELENOv0.3", fix the stackup text.
7. Install:
   - back up the old `ASTRA.kicad_pcb` (it's also in git);
   - check for KiCad lock files and ask Alex to close KiCad;
   - copy the board into the project and add the netclasses to `ASTRA.kicad_pro`;
   - run final DRC with parity.
