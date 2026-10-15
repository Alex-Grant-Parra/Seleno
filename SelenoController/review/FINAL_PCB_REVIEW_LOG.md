# SELENO v0.8: final pre-order PCB review (log)

Started 2026-10-15. Alex is about to order the PCBs and asked for one last very deep review of
`SELENO.kicad_pcb`: check that every part works with the others and has the support parts it needs,
using the datasheets in `datasheets/`.

> **Resume rule:** if this session is cut off, read this file top to bottom, then carry on from the
> first unticked item in "Progress". Findings are recorded as they are found, so nothing is lost.
> All analysis happens on scratch copies; the project files are **not** edited during the review
> unless a finding below says "APPLIED".

## Baseline
- Branch `hardware`, HEAD 7a8d5f5 "v0.8.3". Untracked: `jlcpcb/`.
- Board file `SELENO.kicad_pcb` modified 2026-10-15 16:01; `.kicad_pro` 16:18; JLC outputs 16:15-16:16.
- No KiCad lock files when the review started.
- Tools: KiCad 10.0.5 per-user install, `C:\Users\alexg\AppData\Local\Programs\KiCad\10.0\bin\`
  (`kicad-cli.exe`, `python.exe` with pcbnew).
- Scratch folder (session-specific, regenerate if lost):
  `C:\Users\alexg\AppData\Local\Temp\claude\z--ASTRA\3a7cbff4-0287-4fa3-bd5d-e4a511cf5d90\scratchpad\`

## Progress
- [x] 1. Baseline: ERC 0. DRC 0 errors / 0 unconnected / parity 0; 35 warnings, all `lib_footprint_mismatch`
      (33 test pads + U7/U8, as before; U5 too). Saved zone fills = a fresh refill (same area and polygon count
      for all 6 zones), so the board's fills are current. Outline 85.0 x 50.0 mm, 4 layers, 1.6 mm, 122 footprints,
      753 track segments, 243 vias. Rules: min track 0.15, clearance 0.15, via 0.45/0.3, annular 0.075, edge 0.3.
- [x] 2. Netlist + pin-by-pin dump of every part (scratch `out/pins.txt`, `out/nets.txt`, `out/board.json`;
      tools in scratch `tools/`). 86 schematic parts, 104 nets. Datasheet text extracted to scratch `ds/*.txt`
      (PyMuPDF from an older session's `pylibs2`).
      Gotcha: with `MSYS_NO_PATHCONV=1`, kicad-cli takes `/c/...` literally and writes to `C:\c\...`; pass `C:/...` paths.
- [x] 3. Per-IC datasheet checks (pinout, pin functions, support parts, abs-max, layout notes)
  - [x] U6 TPS5430 buck (+ L2, D8, C35-C38, C48/C49, C52-C54, R29/R30/R39)
  - [x] U9 AMS1117-3.3 LDO (+ C50, C51, C15/C16; stability, dissipation)
  - [x] 12 V input chain: P1, F1, Q1 AO3401A, D6 BZT52C10, R21, D7 SMAJ15A, C1
  - [x] U7/U8 TMC2209 (+ sense R, charge pump, 5VOUT, VCC_IO, VREF, MS/addr, ENN, PDN_UART, J1/J2)
  - [x] U5 ESP32-WROOM-32UE (straps, EN RC, decoupling, flash pins, input-only pins, UART0)
  - [x] Q2/Q3 MMBT3904 auto-program from the Pi (GPIO18/23)
  - [x] J7 Pi Zero 2 link (pin map vs the Pi header, 5 V back-feed, UART levels)
  - [x] U1 MCP23017 (address, RESET, INT, GPA7/GPB7 output-only, pull-ups)
  - [x] S1 LCD (ST7735S), J3 I2C/IMU, SW1-SW7, LEDs D1-D5
- [x] 4. Footprints vs datasheet land patterns, pin-1 / polarity orientation of every polarised part
      (L2, U1, U7/U8 EP, U9 pinout checked against drawings; silk marks checked visually)
- [x] 5. Layout: current capacity of power paths, plane islands, thermal (U6, U9, U7/U8), loops, return paths
- [x] 6. DFM: JLC 4-layer rules (drill, annular ring, track/space), silk, edge clearance, paste, fiducials
- [x] 7. Mechanical: connector placement/orientation, mounting holes, tall parts, heatsinks, antenna cable
- [x] 8. Fabrication outputs: regenerate gerbers/drill and diff with `jlcpcb/`; BOM/CPL rotations, LCSC numbers
- [x] 9. Summary of findings + recommendations for Alex (below).
- [x] 10. **APPLIED 2026-10-15 (Alex: "implement those two changes", then clear and regenerate the JLC files).**
      KiCad had the project open (locks); Alex closed it with no unsaved edits before the install.
      Backup: `review/backup/SELENO.kicad_pcb.pre_final_fix`. Applied on a fresh scratch copy of the project, then
      semantic diff vs the old board: exactly +2 vias, +2 tracks (+5V) and 40 pads -> solid, nothing else.
      Installed board: DRC 0 errors / 0 unconnected / parity 0 (35 lib_footprint_mismatch warnings), ERC 0.
      `jlcpcb/`: old BOM, CPL, gerber/ and zip deleted and regenerated (gerbers with `--board-plot-params`; drill
      excellon, separate PTH/NPTH, mm; zip flat). BOM and CPL byte-identical to the previous ones. The copper gerbers
      have the same content as the reviewed candidate (sorted-line identical; only plot order differs). PTH.drl now
      has 319 holes (was 317). The rotation check passes as before.

## Summary (2026-10-15)
**No blockers.** Every IC's pinout, support parts and limits check out against its datasheet; the netlist, board,
schematic and JLC files agree; the JLC gerbers are current and the CPL rotations land pad-for-pad. The board can
be ordered as it is.

Two cheap improvements, both prepared and verified on a scratch copy, then INSTALLED (see Progress item 10):
- **F1** (SHOULD): solid In1 connection for the 40 thermal vias of U6/U7/U8. Estimated ~4-6 C cooler per driver at
  full current (reliefs are 0.25 gap / 0.4 spoke -> ~1.5 K/W extra per driver); the TMC datasheet asks for solid vias.
  Not a substitute for the heatsinks / lower IRUN (N2).
- **F2** (optional): 2 extra +5V vias at the buck output, so 4 vias feed the In2 +5V island (N4).

Candidate result: DRC 0 errors / 0 unconnected / parity 0, same 35 lib_footprint_mismatch warnings; In1 GND area
3946.1 -> 3955.9 mm2; BOM and CPL regenerate byte-identical (no footprint moved); gerbers that change: F.Cu, B.Cu,
In1, In2 and PTH.drl (317 -> 319 holes); masks, paste, silk, outline, NPTH unchanged.

### How to (re)apply F1 + F2 (if Alex says yes)
1. Check for KiCad lock files (`~*.lck`) in `SelenoController/`; back up `SELENO.kicad_pcb` to
   `review/backup/SELENO.kicad_pcb.pre_final_fix`.
2. With KiCad's python (`-I`), on a scratch copy of the whole project folder (so Save keeps the real .kicad_pro):
   - U6 pads "9" and U7/U8 pads "29" with attribute PTH -> `SetLocalZoneConnection(ZONE_CONNECTION_FULL)`.
   - +5V vias 0.5/0.3 F.Cu-B.Cu at KiCad abs (82.4, 61.1) and (89.0, 61.1) [board-local (5.9, 15.6), (12.5, 15.6)];
     F.Cu +5V tracks 0.5 mm (82.4,61.1)-(84.6,61.1) and (86.8,61.1)-(89.0,61.1). Save.
   (Script used: scratch `tools/applyfix.py`.)
3. `kicad-cli pcb drc --severity-all --schematic-parity --refill-zones --save-board` -> expect 0/0/0 + 35 warnings.
4. Copy the board into the project; regenerate `jlcpcb/gerber/` (`pcb export gerbers --board-plot-params`,
   `pcb export drill --format excellon --excellon-separate-th --excellon-units mm`), re-zip
   `SELENO_v0.8_gerbers.zip`, re-run `make_jlc_files.py` (BOM/CPL should be unchanged).
5. Add a line to REVIEW_LOG.md / PCB_HANDOFF.md.

### Order-time reminders for Alex
- JLC: 4 layers, 1.6 mm, stackup JLC04161H-7628, outer 1 oz / inner 0.5 oz, min via 0.3 mm (0.45 pad), vias tented.
  In the assembly preview, eyeball pin 1 of U1/U7/U8/U6/U9, Q1-Q3 and the cathode bands of D6/D7/D8.
- Use a 12 V supply of >= 3 A. Motors + Pi + ESP32 can pull ~1.5-2.5 A, and F1 holds 3 A.
- J7 cable: a reversed 1x10 plug puts 5 V on Pi GPIO24 and shorts 5 V to GND. Mark pin 1 (5 V) on the 1x10
  housing; a 2x10 housing on the Pi end stops odd-row mistakes.
- Hand-solder list: C1, D1-D5 (cathode = the closed end of the silk bracket, towards the board edge), R25-R28,
  U5, all connectors. The ESP32 centre pad is optional (datasheet: "not a must").

## Findings
Severity: **BLOCKER** (board will not work or gets damaged) · **SHOULD** (fix before ordering if cheap) ·
**NOTE** (fine as is; worth knowing) · **OK** (checked, no issue).

### F1 [SHOULD] Thermal vias under U7, U8 (TMC2209) and U6 (TPS5430) join the In1 GND plane through thermal-relief spokes
- U7/U8 pad 29 (16 vias each) and U6 pad 9 (8 vias) have zone connection "inherit"; the GND plane's mode is
  THT-thermal, so each via connects through spokes (seen in the In1 render as a lattice of relief gaps).
  U5's (ESP32) thermal vias are "solid" (stock footprint) and connect fully. With no F/B pours, In1 is the drivers'
  main heat spreader, and the drivers are already the hottest parts.
- Fix: set pad zone connection = solid on those via pads (or on the whole U6/U7/U8 footprints), refill zones.
  No copper moves, no routing change. Same kind of change as P1/C1/J7 got on 2026-10-15.
- Size of the effect: the plane's reliefs are 0.25 mm gap / 0.4 mm spokes, which adds ~1.5 K/W per driver,
  so ~4-6 C at 2.6 W. TMC2209 datasheet 21.1: "use a reasonable amount of solid, thermally conducting vias
  between the die attach pad and the ground plane".

### F2 [OPTIONAL] Only 2 vias carry the whole +5V output into the In2 island (see N4); add 2 more

### OK items (checked, no issue)
- U6 TPS5430 pinout (1 BOOT, 2/3 NC, 4 VSENSE, 5 ENA, 6 GND, 7 VIN, 8 PH, DAP GND) matches symbol/footprint/nets.
  Boot cap C36 10nF 50V (TI: 0.01 uF, >=10 V). ENA open (internal 1.5M pull-up; TI forbids a resistor to GND).
  Catch diode SS34 40 V/3 A (TI example B340A, same class). Vout = 1.221*(1+15/4.7) = 5.118 V (4.94-5.30 worst case).
  UVLO 5.3-5.5 V, Dmax 87 % - fine at 12 V in.
- U6 compensation = TI Fig 7-11 topology exactly (C52=C6 across R29; C53=C4 VSENSE-GND; C54+R39 = C7+R3; R30=R2).
  TI's recipe for this design gives C7 0.11 uF / R3 350 R / C6 650 pF; the board has 100 nF / 330 R / 1 nF.
  My own loop model (Eq 15 internal type III, feed-forward gain 25 from Eq 7/8, 15 uH + 71 mOhm DCR, 3 mOhm ESR):
  PM 56-75 deg, GM 21-30 dB over 25-60 uF effective Cout and 0.05-3 A; still PM >= 48 deg at 80 uF. The 1 nF C6 is
  actually a little better than 680 pF in the model. Without the network the loop is unstable (PM -14 deg), so
  C52/C53/C54/R39 are essential parts. C4 = C6/10 (TI's own example is also exactly 1/10). fLC with ~40 uF eff = 6.5 kHz
  (TI limit 7 kHz for ceramics; 4x22 uF 0805 at 5 V ~ 35-45 uF eff).
- L2 FXL0840-150-M: 15 uH, DCR 71 mOhm, Isat 4.9 A, Irms 4.8 A (datasheet p12). Ripple 0.39 A pk-pk at 12 V in;
  peak at 3 A load 3.2 A. TI's own example uses a 3.4 A Isat part, so Isat need not exceed the 4-6 A switch limit.
  Footprint pads 2.75 x 5.5 at +-3.38 = datasheet land pattern (gap 4.0, span 9.5, width 5.5).
- U9 AMS1117-3.3: pinout 1 GND, 2 VOUT (tab), 3 VIN matches SOT-223-3_TabPin2 and nets. Headroom 5.12-3.3 = 1.82 V
  (dropout max 1.3 V at 0.8 A). C50 10 uF right at VIN, C51 22 uF at the tab.
- C1 polarity: pad 1 (+12V) = the "+" side; filled half of the silk circle = GND pad 2. Correct.
- In1 = one solid GND polygon (3946 mm2), only via antipads. In2 = 3V3 plane + +5V and +12V islands.
- U7/U8 TMC2209 pin table (datasheet p9-10) = symbol pin for pin (7 SPREAD, 20 STDBY, 25 unused, EP 29 GND).
  CPO-CPI 22 nF 50 V, VCP-VS 100 nF, 5VOUT 2.2 uF, 1206 0.15 R sense (datasheet: 1206 0.5 W), SPREAD/CLK to GND,
  STDBY open (100k pull-down), MS1/MS2 = addr 0 (U7) / addr 1 (U8), ENN 10k pull-ups, VCC_IO 3.3 V.
  VREF tied to 3.3 V is allowed: 2.5-5 V = full scale (p54-55); the test-mode condition needs VREF > both VCC_IO
  and 5VOUT by 10 %, and VREF = VCC_IO here, so it can never trigger. Abs max VS 32 V vs TVS clamp 24.4 V.
  EP 3.7 x 3.7 (datasheet J/K 3.6-3.8). Sense resistors have separate GND vias (datasheet: don't share).
  Coil pairs J1/J2: 1-2 = OA1/OA2, 3-4 = OB1/OB2.
- U5 ESP32-WROOM-32UE: module pin table (datasheet v2.1 Table 3) = symbol 1:1 (17-22 and 32 NC). Straps: IO0 Q2 +
  internal pull-up; IO2 NC (pull-down); IO12/MTDI NC (pull-down -> 3.3 V flash; datasheet: keep low at power-up);
  IO15/MTDO = LCD DC, IO5 = BL (only U0TXD log / SDIO timing, harmless). EN 10k + 1 uF (= Espressif's
  recommendation), C11 100 nF right at pin 2, C43 22 uF ~4.5 mm away. IO34/35/39 input-only used as inputs only;
  IO39 has R38 pull-up. IO16 = 1DIR, so the module must not be an -NxR2 (PSRAM on IO16, datasheet note 3).
- Q2/Q3 auto-program: Q3 base<-DTR, E=RTS, C=EN; Q2 base<-RTS, E=DTR, C=IO0 = the standard esptool circuit.
  Pi GPIO18/23 default to pull-downs at boot -> both transistors off -> EN/IO0 released.
- J7 vs Pi even row: 1/2 = pins 2/4 5V, 3 = pin 6 GND, 4 = pin 8 GPIO14 TXD -> ESP RXD0, 5 = pin 10 GPIO15 RXD <-
  ESP TXD0, 6 = pin 12 GPIO18 (DTR), 7 = pin 14 GND, 8 = pin 16 GPIO23 (RTS), 9 = pin 18 NC, 10 = pin 20 GND. Correct.
- U1 MCP23017 QFN: datasheet Table 2-1 = symbol 1:1. Inputs only on bidirectional pins; GPB7 (output-only) drives the
  white LED; GPA7 unused. A0-A2 GND (0x20), RESET 10k/10 uF RC. Footprint = Microchip ML land pattern (C04-2105A:
  4.25 centre pad max, 5.70 spacing, 0.37 x 1.00 pads; footprint 0.30 x 1.025).
- Q1 AO3401A: -30 V, +-12 V Vgs, <50 mOhm @ -10 V, 4 A; SOT-23 1 G / 2 S / 3 D matches. Drain to the input, source
  to the rail (body diode conducts forward): correct reverse-polarity orientation. D6 K on +12V, A on gate -> Vgs
  ~ -10 V (BZT52C10 9.4-10.6 V < 12 V). D7 SMAJ15A after Q1 (unidirectional; pad 1/cathode on +12V).
- Silkscreen polarity marks all present and consistent: IC pin-1 triangles (U1, U5, U6, U7, U8, U9, Q1-Q3), diode
  cathode bars at the pad-1 end (D6, D7, D8; LEDs D1-D5 bar on the board-edge side = cathode/GND), C1 "+",
  P1 "12V+" at pin 2 (the JST triangle marks pin 1 = GND), J7 square pad 1 + "PI: EVEN ROW, PIN 2 ... PIN 20".
- Fab outputs: every file in `jlcpcb/gerber/` and the zip = a fresh kicad-cli export of the current board
  (byte-identical apart from timestamps). Absolute coordinates, Y negated - the same frame as the CPL.
  NPTH = 4 x 2.7 mm holes. Min drill 0.3. Job file: 4 layers, 1.6 mm, HAL lead-free.
- JLC BOM/CPL re-verified independently (scratch `tools/rotchk.py`, using the LCSC/EasyEDA footprint JSONs that
  the earlier session cached; the live API now returns 403): each LCSC footprint placed at the CPL position and
  rotation lands pad-for-pad on the KiCad pads. U1/U6/U7/U8 all pins within 0.05 mm (so the +270 QFN correction
  is right); Q1 G/S/D and Q2/Q3 B/E/C pin names match; D6/D7/D8 LCSC pad 1 = K = KiCad pad 1; U9 GND/VOUT/VIN/tab
  match. Residuals 0.2-0.75 mm on D7/D8/Q1/L2/U9 are only different pad spacing in the LCSC footprints (a wrong
  rotation would show 2-6 mm). All 26 LCSC numbers resolve to the intended MPN/value/voltage.
- DRC with the 5 ignored checks switched back on (scratch copy): only 7 x track_not_centered_on_via (still
  connected) and footprint_type_mismatch on U6 - cosmetic. All warning-level checks except lib_footprint_mismatch
  report 0 (silk, dangling, slivers, hole-to-hole...). No DRC/ERC exclusions.
- Mounting holes: nearest surface copper 3.8-6 mm from the hole centres; inner planes pulled back to r 1.6 mm.
- Power paths: input P1 -> F1 1.5 mm -> Q1 1.0 mm -> 3 vias into the In2 +12V island; buck input 2 vias; GND
  everywhere via In1 + 70 vias.

### NOTE items
- N1 AMS1117 stability: the AMS datasheet only promises stability with a 22 uF solid tantalum on the output; this
  rail is all-ceramic (C51 22u + C43 22u + C15 10u + 100 nFs, ~30-35 uF effective). AMS1117 + MLCC is very common
  and normally fine (no ESR window is specified, unlike the TI LM1117). If the first board's 3.3 V shows ringing or
  oscillation, fit a 22 uF tantalum (or 1 ohm in series with an extra MLCC) at C51/C43.
- N2 TMC2209 heat (known, Alex has heatsinks): datasheet p80 says 2.8 W at 1.4 A RMS and 1.4 W at 1.0 A RMS, with
  30 K/W only on a 1 oz 2s2p 70x133 mm board. This board is 0.5 oz inner with no F/B pour, so expect a higher K/W.
  Do F1, fit the heatsinks, and set IRUN for ~1.0 A RMS unless more torque is really needed.
- N3 TMC2209 decoupling distances: C27/C31 (5VOUT) sit ~2.5 mm from pin 8 and return through a plane via; the
  datasheet (p81) prefers the cap right at the pin with its GND straight to the die pad. VS 100 nF caps C2/C32 are
  ~4-5 mm from the VS pins (through In2/In1 vias), C47 22 uF between the drivers, C1 680 uF 11-26 mm away on the
  In2 +12V island. Works as built; not worth re-routing now.
- N4 +5V output vias: since the power change, the buck output reaches the In2 +5V island through only 2 vias,
  (8.1, 15.6) and (10.3, 15.6). Everything on +5V goes through them: the Pi (+ its USB camera) and the AMS1117
  feed (via (14.6, 23.3)). That's ~1-1.5 A typical, ~2 A peak; a 0.3 mm via is good for roughly 1-1.5 A, so it
  works with thin margin. The design check before the power change had given the Pi feed 4 vias. Cheap to add
  2 more next to the output-cap strip (optional, see F2 candidate).
- N5 F1 value text says "PTC 3A hold 16V+", the ordered part (C207089 Littelfuse 2920L300/15DR) is 15 V max.
  Fine for a 12 V supply (and equal to the TVS stand-off), but a wrong 19-24 V adapter could stress a tripped
  PTC. Cosmetic value mismatch only.
- N6 Silkscreen text is 0.8 mm / 0.15 mm stroke (JLC recommends >= 1.0 mm height, >= 0.153 mm stroke); footprint
  silk lines 0.12 mm. Small labels may print soft; nothing to change for function.
- N7 C1 (10 mm, likely 16-20 mm tall) courtyard is 0.7 mm from the SW5/SW6 (SELECT/RETURN) SH headers: plugging
  those two cables in next to the can will be fiddly; plug them before fitting C1 or use tweezers.
- N8 Thermal vias in U6/U7/U8 pads are open 0.3 mm holes under printed paste (stock KiCad footprints): expect some
  solder wicking/voids; normal for JLC, no action.
- N9 Firmware/hook-up notes (not PCB issues): MCP23017 buttons are on port B but only INTA is wired, so set
  IOCON.MIRROR=1; enable the MCP pull-ups (GPPU) for the buttons; Pi UART on GPIO14/15 needs the console
  disabled and (on a Zero 2) Bluetooth moved off the PL011; IO39 (EXP_INT) has the ESP32 GPIO36/39 glitch
  erratum when Wi-Fi/ADC power-save is active; TMC2209 IRUN/I_scale_analog set over UART (VREF = full scale).

## Work notes
- Scratch tools (session scratchpad `tools/`): `pins.py` (XML netlist -> pin table), `boarddump.py` (board ->
  JSON), `region.py` (parts/tracks in a u,v window), `view.py` (render a window of a kicad-cli layer PDF via
  PyMuPDF), `pdftext.py` / `pdfpng.py` (datasheet text / page images), `loop.py` (TPS5430 loop model),
  `zones.py` (zone fill stats), `padzc.py` (pad zone connections), `holes.py`, `cy.py` (courtyard gaps),
  `silk.py`, `rotchk.py` (CPL vs LCSC footprints), `applyfix.py` (F1+F2 candidate).
- Board-local (u, v) = KiCad abs - (76.5, 45.5), as in the pcb_build scripts.
- Read tool can't render PDFs here (no pdftoppm); render pages with PyMuPDF to PNG instead.
