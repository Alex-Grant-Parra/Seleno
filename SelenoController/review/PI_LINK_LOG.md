# Pi link change: drop the USB hub and CH340, ESP32 controlled from Pi GPIO (started 2026-10-14)

> If the session disconnects, resume from **Progress**. Tick items as they complete.

## Why
Cost. This removes the USB2512B hub, the 24 MHz crystal and the CH340C, which are 2 to 3 JLC extended loading fees plus about $4.50 of parts.
It also removes the USB-A stacked socket, both USBLC6 ESD chips and the camera PTC. The camera now plugs into the Pi Zero 2's own USB port.

## Design (agreed with Alex)
- **J7: 1x10 2.54 mm pin header.** A 10-way single-row Dupont ribbon runs from it to the **even row** of the Pi header, pins 2 to 20, one-to-one.
  Alex asked for one row on the Pi side ("1x10 is better than 2x5").

| J7 pin | Pi pin | Pi function | Board net |
|---|---|---|---|
| 1 | 2  | 5V      | +5V (the board powers the Pi through the header) |
| 2 | 4  | 5V      | +5V |
| 3 | 6  | GND     | GND |
| 4 | 8  | GPIO14 TXD | UART2 → ESP32 U0RXD (U5 pin 34) |
| 5 | 10 | GPIO15 RXD | UART1 ← ESP32 U0TXD (U5 pin 35) |
| 6 | 12 | GPIO18  | DTR (auto-reset pair) |
| 7 | 14 | GND     | GND |
| 8 | 16 | GPIO23  | RTS (auto-reset pair) |
| 9 | 18 | GPIO24  | no-connect (spare) |
| 10 | 20 | GND    | GND |

- **The Pi's 3V3 (pin 1) is NOT connected.** The board's 3V3 rail must not be tied to the Pi's own regulator.
- **Auto-reset kept as-is:** Q2/Q3 MMBT3904, cross-coupled, with R22/R23 10k. GPIO18 takes the old CH340 DTR role and GPIO23 the RTS role.
  - EN goes low only when RTS=0 and DTR=1. IO0 goes low only when DTR=0 and RTS=1.
  - Both lines equal (including the Pi's default pull-downs during boot) = no effect.
  - Flashing from the Pi needs a small GPIO wrapper around `esptool --before no_reset`. This is software, so it isn't done here.
- **Power:** the Pi takes 5 V on header pins 2/4, which bypasses its micro-USB polyfuse. The camera is powered by the Pi's USB port from the same 5 V. The AP62300 is 3 A, so there's plenty of headroom.

## Removed parts (all on the USB sheet)
U2 USB2512B, Y1 24 MHz, U3 CH340C, J6 USB-A stacked, U10/U11 USBLC6, F2 PTC,
C9 C13 C14 C17 C19 C21 C22 C23 C24 C25 C44 C45 C46, R9 R10 R11 R12 R13 R14 R15 R18.
**Kept:** Q2, Q3, R22, R23. **Added:** J7.

## Progress
- [x] 0. Backups: session scratchpad `orig/` holds every .kicad_sch, .kicad_pcb, .kicad_pro and .kicad_prl as of the start. Netlist before = `before.net`.
- [x] 1. Schematic (in scratch `work/`, not yet installed). Script: scratchpad `pi_sch.py` (with `schlib.py`, `sexp.py`, `conn10.txt`).
      - usb.kicad_sch is now **pi_link.kicad_sch**, sheet "Pi Link".
      - J7 (Conn_01x10, mirrored, pins at x 35.56, y 68.58-91.44) with +5V/GND symbols, UART1/UART2 globals and DTR/RTS locals; pin 9 is a no-connect; pin-map note at (88.9, 61).
      - Q2/Q3/R22/R23 untouched.
      - All sheets are now rev 8 / 2026-10-14. Root, MCU pin map and U9 description texts updated.
      - Added a +3.3V PWR_FLAG (#FLG5, #PWR161) on the Power sheet at x 109.22, because the CH340C's V3 pin had been the rail's only ERC driver.
      - Fixed in passing: the power sheet had stray doubled CRs (`\r\r\n`) from an old edit; normalised to CRLF.
- [x] 2. ERC 0/0. Netlist audit (`netdiff.py before.net after.net`): exactly the 28 parts removed + J7 added; no other net changed; DTR/RTS renamed `/Pi Link/...`.
      +5V = C12 C37 C38 L2 R29 J7.1 J7.2. UART1 = U5.35 + J7.5. UART2 = U5.34 + J7.4. DTR = Q2.2 R22.2 J7.6. RTS = Q3.2 R23.2 J7.8.
      PDF can't be rendered here (no poppler), so the visual check of the Pi Link sheet is for Alex.
- [ ] 3. PCB (in scratch `pcb/`, built from the CURRENT installed board including Alex's hand routing; nothing regenerated)
  - [x] 3a. `pi_pcb1.py orig/ASTRA.kicad_pcb pcb/step1.kicad_pcb`: 28 footprints, 103 dead-net items and 113 hanging stubs/plane-only vias deleted.
  - [x] 3b. `pi_pcb2.py pcb/step1.kicad_pcb pcb/step2.kicad_pcb work/pi_link.kicad_sch`:
    - DTR/RTS nets renamed to /Pi Link/...; all old DTR/RTS copper deleted (long detours; DTR had been left open).
    - J7 PinHeader_1x10 placed at rot 90 on the top edge: pin row v 2.2, pins at u 8.4 + 2.54(n-1) (pin 10 at 31.26). Linked to the symbol uuid.
    - TP11 (SCL) moved to (34.2, 3.8) and TP12 (SDA) to (36.5, 5.3), with new stubs. Their silk labels moved.
    - Silk "PI:1-4 CAM:5-8" deleted; new label "PI: EVEN ROW, PIN 2 ... PIN 20" at (19.83, 4.55).
  - [ ] 3c. Route the 4 signals (paused here at Alex's request):
    - UART2: J7.4 to the trunk via at (53.6, 17.2).
    - UART1: J7.5 to the trunk via at (52.9, 17.3).
    - DTR: J7.6 to Q2.2 (54.06, 17.95) and R22.2 (55.51, 10.6).
    - RTS: J7.8 to Q3.2 (54.06, 13.95) and R23.2 (55.51, 19.4).
    - Plan: an F.Cu bus east along v ~6.5-8.3 through the empty area, then the local router (`lroute.py`, via drill already fixed to 0.3) for the last few mm.
    - The area around Q2/Q3 is crowded: RES/DC/BL on F.Cu, and 2EN/T1_DIAG/T2_DIAG/BL on B.Cu.
    - +5V/GND need no tracks: J7 is inside the In2 +5V island and the In1 GND plane.
- [x] 4. **INSTALLED 2026-10-14** (Alex: "just apply to KiCad so I can finish it myself"). KiCad was closed and the files were unchanged since the copy.
      - Zones refilled.
      - `pi_pcb3.py` cleared the parity warnings: J7 lib id, Datasheet and Description fields; U9 Description.
      - **usb.kicad_sch was deleted and replaced by pi_link.kicad_sch.**
      - Backups: `review/backup/*.pre_pi_link`. These are the pcb, pro, root and all 7 sheets, including usb.kicad_sch.
      - Installed result: ERC 0. DRC: 0 errors, parity 0, **6 unconnected** (the 4 nets in 3c, left for Alex to route).
        Warnings: the 16 old `lib_footprint_mismatch` (test pads + U5/U7/U8), plus 2 `via_dangling`. Those two are the UART1 via (52.9, 17.3) and the UART2 via (53.6, 17.2): the ends of the ESP32 TX/RX trunks, i.e. the connection points for J7.5/J7.4. They clear once routed.
- [ ] 5. Alex routes 3c in KiCad. Then: PARTS_LIST.txt (remove the hub, crystal, CH340C, USB-A, USBLC6 x2, F2 and the 22 passives; add the 1x10 header), PCB_HANDOFF.md, REVIEW_LOG.md.

## PHASE 2: full PCB re-layout (Alex, 2026-10-14: "redo the pcb placement and design, much more space now; use the routing tools; lots of test pads; min hole 0.3")
Rules are the same as PCB_HANDOFF.md "What Alex asked for":
- 85×50 exactly, 4 layers (In1 GND, In2 power), all parts on top.
- Net-class widths; connectors and LEDs at the edges; M2.5 holes at 3.5 mm from the corners.
- No F/B pours. Silk title is now "SELENO.v0.7" (Alex's).
- New: **every drill >= 0.3 mm**: vias 0.45/0.3 min, and the stock U5/U7/U8 thermal vias opened up to 0.3 after the build.
- Time-box router runs to about a minute.

Scratch root: session scratchpad `seleno/` (set `SELENO_SCR` to it). It holds `final/astra.net` (exported from the installed rev 8 schematic) and `pcbwork/` (a project copy).
The scripts in `review/pcb_build/` are edited in place (the originals are in git 05cd8e3).

- [x] P1. Scripts edited:
  - rules: MIN drill 0.3, annular 0.075, TIGHT_VIA 0.45/0.3; USB/VBUS_CAM patterns dropped.
  - build: min drill 0.3; PTH pads < 0.3 opened to 0.3; TESTPADS are now 6-tuples.
  - hand: VIA_T 0.45/0.3; usb() deleted; U6 uses place.U6_AT; the C1 -> U6 +12V track is gone (U6 is fed from the In2 corridor by fanout).
  - route: every (0.45, 0.2) is now (0.45, 0.3); EP via grid only on U1; signal budget 60 s.
  - planes: ISL_12V gains a corridor u 15-21.5, v 13.7-22.4 up to U6's Cin; ISL_5V is now the top-left block (J7, C12, buck output) with a notch for the corridor.
  - finish: labels for J7 ("PI: EVEN ROW, PIN 2 ... PIN 20") and J3 ("IMU"); QWIIC/IMU(J5)/PI-CAM labels dropped; test-pad labels generated from place.TESTPADS; title moved to (35, 17).
- [x] P2. place.py, new floorplan:
  - J7 (8.4, 2.2) rot 90; C12 (9.7, 5.4).
  - **U6 5 V buck moved to (19, 11.5)** (top-left, by the Pi); R31/R32 BUCK_EN divider at (16.4, 18.0/19.1) in the +12V corridor.
  - Q2/Q3/R22/R23 moved to u 49 (6 mm further from the ESP32).
  - USB/hub/IMU parts removed. Everything else is unchanged (drivers, U9, input, ESP32, MCP, LEDs, buttons).
  - **33 test pads**:
    - TP1-4: 12V/GND left edge, 5V/GND by U6.
    - TP5-10: ESP32 3V3/EN/IO0/TX/RX/GND, right edge.
    - TP11/12: SCL/SDA. TP13/14: DTR/RTS.
    - TP15-19: LCD column (SCK MOSI DC RES BL) at u 55.6.
    - TP20-29: motor row at v 24 (1STEP 1DIR 1EN 1DIAG 2STEP 2DIR 2EN 2DIAG TUART TTX).
    - TP30/31: 3V3/GND middle. TP32: BUCK_EN. TP33: EXP_INT.
  - Placement DRC: no courtyard or clearance problems (silk only; finish.py fixes those). Render: scratch `seleno/place.png`.
- [x] P3. Hand routes: done as part of P1 (the driver, buck, power and button routes are kept).
- [x] P2b. First route attempt: 62/73 nets in one 113 s pass. The motor TP row (v 24) and the LCD TP column (u 55.6) sat in the signal channels and blocked them.
      Moved: the motor TPs to a 2x5 block above C1, the LCD column to u 52, and Q2/Q3/R22/R23 to u 44.5 with DTR/RTS TPs at u 41.
      The second route run was stopped by Alex.
- [x] P2c. **QFN -> leaded swap envelopes** (Alex may change the QFN chips for leaded parts). `place.ENVELOPES`, checked by `build.check_envelopes()` on every build:
  - U7/U8: TMC2209 has no leaded version; the twin is the **TMC2226-SA (HTSSOP-28)**.
    - Envelope: 10.6 x 8.2 around each driver centre, long side horizontal.
    - Driver blocks moved to v 36.55, motor connectors unchanged at v 46.55.
    - Support parts moved out of the envelope: sense R at dv +5.3, Ccp (6.0,-0.25), Cvcp (6.4,-2.0), C5v (2.6,-5.0), Cio (-3.72,-5.1) rot 90, Cvs (-5.9,2.3).
    - hand.driver updated to match. The VCP cap's +12V via is now at (6.88,+0.3), below the +3V3 band.
    - The In2 +3V3 band is now v 32.4-36.45.
  - U1: MCP23017-E/SS (SSOP-28) envelope 10.8 x 10.8, which covers either rotation. SOIC-28 doesn't fit without moving the 3.3 V buck.
    - Moved: C6 (77.2, 34.6), R19 (69.2, 29.6), C33 0805 (66.8, 29.9) rot 0, R38 (64.0, 28.6) rot 0, TP33 (62.2, 30.9).
  - Placement DRC clean (silk only); 0 envelope intrusions. Render with envelopes: scratch `seleno/place2e.png`.
  - On a later swap: change the footprint, redo hand.driver's pin-escape routes (or the MCP button bus) for the new pinout, and re-route. Nothing else moves.
- [x] P2d. **Logical grouping** (Alex OK'd 2026-10-14). Script: scratch `group_edit.py`.
  - Power strip: **U9 3.3 V buck moved to (38, 11.5)**, beside U6 under J7; C15/C16 at (33.2/35.0, 17.3).
    - ISL_12V now has a band at v 19.6-22.4 with two corridors (u 15-21.5 to U6, u 35.6-40 to U9).
    - The C1 -> U9 B.Cu hand route is deleted (U9 is fed from the corridor by fanout).
  - Programming/debug group bottom-middle:
    - R22 (51.5, 30.6), Q3 (51.5, 33), Q2 (51.5, 37), R23 (51.5, 39.4).
    - 2x4 TPs at u 55/57.6/60.2/62.8, v 31/34: GND 3V3 TX RX / EN IO0 DTR RTS. Silk "ESP32 PROG".
  - TPs renumbered TP1-TP33:
    - power: 12V/GND (TP1/2), 5V/GND (TP3/4), 3V3/GND at U9 (TP5/6);
    - SCL/SDA TP7/8 at v 2.2 (left of J3);
    - LCD TP9-13;
    - prog TP14-21;
    - motor TP22-31;
    - BUCK_EN TP32, EXP_INT TP33 at (61.6, 28.6).
  - Title moved to (28.5, 21.2). Render: scratch `seleno/place3.png`.
- [ ] P4. Route.
  - Run 3 (regrouped): 66/73 in one 71 s pass. Saved as scratch `seleno/routes_run3.json`.
    Failed: SDA, 2EN, DC, GPIO0, SCK, YELLOW (U1.17 boxed in by the EXP_INT and BLUE vias), U1 RESET.
  - Run 4: route.py signal budget 60 -> 75 s, so pass 2 runs, with the failed nets first.
  - Run 4: **69/73** in 2 passes (126 s). Saved as `seleno/routes_run4.json`. Failed: 2EN, SCK, T2_DIAG, RES (the ESP32 top-left corner / left channel is full).
    `--repair` on run 4: 0/4 in 3 passes.
  - Alex: "feel free to move any components to get it to fit, just make it look professional". Script: scratch `channel_edit.py`.
    - **U5 moved +2 mm east to (70.41, 16.79)**; the keep-out follows. R16 -> (69.3, 28), R35 -> (72.47, 28.1), R7/R8 -> u 81.8.
    - R36 -> (60.3, 6.2). LCD TP column -> u 47 (labels on the right). TP5/TP6 (3V3/GND) -> (41.6/44.2, 17.6).
    - Placement clean.
  - Run 5: 66/73 (pass 1 overran the 75 s budget, so no pass 2).
  - Run 6: budget 100, passes 2. Best pass 66/73 (`routes_run6.json`).
  - New `route.py --ripup`: 71/73 in 127 s (`routes_rip.json`). Left: SDA, TMC_TX.
  - Pipeline after routing: scratch `finalize.sh [routes.json]` (build, apply, dangle, finish, DRC with parity, render final.png).
  - Manual (Alex: "just do the last ones manually"), scratch `manual/`:
    - ops1.json: TMC_UART vias at U7/U8 moved to (12.8, 34.3) / (28.2, 34.3) as 0.45/0.3, because they were 0.05 mm from the EP corners. TMC_TX U5.30 -> R16 routed.
    - tp31.py: TP31 moved to (67.25, 27.75) beside R16 and stubbed (its motor-block slot was walled in on both layers).
    - ops2.json: SDA J3 -> U5.9, U5.9 -> R8, R8 -> U1.9, all routed with lroute.
    - silkfix.py: TP2 GND label and the LED letters nudged. The 'TUART' label is now 'UART'.
  - **Final: DRC 0 errors, 0 unconnected, parity 0** (36 lib_footprint_mismatch warnings).
- [x] P4. Route, apply, dangling cleanup, finish, DRC + parity: done (above).
- [x] P5. **INSTALLED 2026-10-15.**
  - ASTRA.kicad_pcb + ASTRA.kicad_pro copied in; backups in `review/backup/*.pre_relayout`.
  - Installed check: ERC 0; DRC 0 errors / 0 unconnected / parity 0.
  - PNGs: `review/board_3d.png` (3D top) and `review/board_copper.png` (copper, both layers).
  - Docs updated: PARTS_LIST.txt (rev 8), PCB_HANDOFF.md (new status section), REVIEW_LOG.md.
  - Scripts in `pcb_build/` match the board's placement, but not the manual routing steps.
  - Nothing committed.

## Notes as I go
