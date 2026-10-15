# ASTRA Controller – Pre-PCB Schematic Review Log

Started: 2026-10-13 · Schematic: `AstraController/ASTRA.kicad_sch` (rev 6, KiCad 10.0.5)
Purpose: confirm schematic is ready for PCB layout; identify parts/circuits to swap or remove.

> If the session disconnects, resume from **Progress** below. Tick items as completed.

## Tooling
- kicad-cli: `C:\Users\alexg\AppData\Local\Programs\KiCad\10.0\bin\kicad-cli.exe`
- Working exports (scratchpad, regenerate if lost): BOM csv, netlist, PDF, pin map via `parse_net.py`
- Note: C14 and G1 have `(on_board no)` so normal netlist omits them; analysed via a scratch copy with the flag flipped.

## Progress
- [x] 1. Export BOM / netlist / PDF, run ERC (13 violations: 11 err, 2 warn)
- [x] 2. Pin-by-pin connectivity dump of every part
- [x] 3. First-pass review of every subsystem (findings below, some marked VERIFY)
- [x] 4. Download datasheets → quarantine → safety scan → `AstraController/datasheets/` (20 PDFs, all clean, see MANIFEST.md; missing: Würth L2, 2N3904 – blocked 403)
- [x] 5. Verify VERIFY-flagged items against datasheets
- [x] 6. Write final findings report (`review/SCHEMATIC_REVIEW.md`) ← **authoritative list; the first-pass list below is superseded**
- [ ] 7. Discuss part swaps / removals with Alex (no schematic edits made yet) – 4 open questions at end of SCHEMATIC_REVIEW.md

## Alex's answers (2026-10-13)
1. Host = Pi Zero 2 on J6 port 1 (the Pi is the USB data host but takes its power from the board through the cable). Keep the hub + CH340 for now; may later drop both and use Pi GPIO UART (research separately).
   → B4 withdrawn: VBUS1→+5V is intentional (it powers the Pi). VBUS_DET→3.3V is OK (host is permanently attached and board-powered).
2. 3.3 V from a second AP62300: approved (Alex asked for the reasoning).
3. IMU is external (on the OTA dovetail) → G1 becomes a cable connector to the BNO085 breakout.
4. Wire U8/J2. Motors = SIMAX3D NEMA17 1.5 A, 6-pin PH, pins 1,3,4,6 used → coil A = 1&3, coil B = 4&6. Keep 6-pin JST-PH (S6 XH suggestion withdrawn).
5. Single shared UART for both TMCs: approved. **Claude to write the half-duplex UART firmware.**
6. Motor 1.5 A → 0.15 Ω (1.35 A RMS max) is fine.
7. LCD = generic ST7735S module (no link) → keep BL on IO5, note to verify.
8. LEDs are unknown 0805 parts in 5 colours → choose resistors for roughly even brightness.
9. JLCPCB assembly for hard parts, hand-solder the rest → LCSC part numbers to be picked later.
Extras: yes to camera-port PTC, USB ESD, 12 V TVS, MCP INTA→ESP32.

## Implementation plan (git base: 201f042 "temp commit")
Tooling: scratchpad/schedit.py (span-preserving editor; pin-geometry validated 356/356 against the KiCad netlist).
Verify after each batch: ERC + netlist diff against expected changes.
- [x] Batch 1 – APPLIED to real file (netlist partition identical to baseline; ERC unchanged). Backup: scratchpad/work/after_b1.kicad_sch. property-only: R25-28 1206 0.5W; L2 → Device:L 3.3uH; D6 BZT52C10 SOD-123; F1 rating; Q1 AO3407A;
      R29/R30 52.3k/10k; LED R (R/Y 330R, G 150R, B/W 47R); cap voltages; C14 + G1 on board; G1→J5 JST-SH 9p;
      C34 100nF; Q2/Q3 → MMBT3904; R9 1%
- [x] Batch 2 – APPLIED (every netlist change audited as intended; ERC = only +5V power flag, which Batch 3 fixes). Backup: scratchpad/work/after_b2.kicad_sch.
      Also: VCC/AD0/IMU_CS/PS0/CS labels → plain text; U2 hidden stacked supply pins unhidden; nets renamed 1TX→TMC_TX, 1RX/2RX→TMC_UART;
      IO25=IMU_INT, IO26=IMU_RST, IO39=EXP_INT, IO4=LCD RES, IO2 + GPA7 no-connect. Rewires: SELECT→GPA2; RES→IO4; shared UART (delete R17, U8 PDN→1RX); IO25=IMU_INT, IO26=IMU_RST;
      MCP INTA→IO39; VCC/AD0 labels→text; U8→J2 + J1 6-pin (1,3,4,6); Y1 crystal fix; delete C18/C20; delete U4/C10
- [x] Batch 3 – APPLIED. Real-file ERC: 0 errors / 0 warnings. 405/405 connected pins geometry-verified; all remaining open pins reviewed and intentional.
      New refs: U9 C39 C40 L3 C41 C42 R33 R34 (3V3 buck), C43-C46, R35-R38 (pull-ups), D7 (TVS), F2 (cam PTC), U10/U11 (ESD), #FLG1/2; sheet A3, rev 7.
      Backup: scratchpad/work/after_b3.kicad_sch. New circuits (sheet → A3, new blocks on right): 3V3 buck (AP62300 31.6k/10k, 3.3uH, 10uF, 2x22uF, 100nF BST, EN shared);
      TVS SMAJ15A on +12V; PTC on J6 VBUS2 (VBUS_CAM); 2x USBLC6-2SC6; ENN pull-ups x2; IMU_INT pull-up; EXP_INT pull-up;
      22uF at ESP32; hub decoupling 2x100nF + 4.7uF
- [ ] Final: ERC, netlist audit, PDF visual check, update SCHEMATIC_REVIEW.md, commit
- [x] DONE 2026-10-13: hierarchical design installed. Files: ASTRA.kicad_sch (root: 6 sheet boxes + overview), power / mcu / usb /
      motors / ui / imu .kicad_sch (A4 each). Netlist partition identical to the flat rev 7 (135/135 nets); ERC 0/0 with NO exclusions
      (cleared 134 stale flat-design exclusions; added PWR_FLAGs on +12V/GND; U2 CRFILT/PLLFILT pin type → passive; 31 explicit no-connects).
      Readability pre-pass: ESP32EN exit above Q3, IMU reset RC detached from J5 (own IMU_RST label), Q3 fields moved.
      Flat backup: scratchpad/work/flat_rev7_backup.kicad_sch/.kicad_pro. Generator: scratchpad/hier_prepass.py + hier_build.py.
      Original plan:
- [ ] Reorganise into a hierarchical multi-sheet design (Alex chose this 2026-10-13). Root = sheet symbols + notes;
      sub-sheets Power / MCU / USB / Motors / UI / IMU+expansion (A4 each). Blocks move rigidly; nets crossing sheets become global labels.
      Acceptance: netlist partition identical to after_b3; ERC clean; PDF check per page.
      PCB note for Alex: symbol paths change → in "Update PCB from Schematic" tick "Re-link footprints to schematic symbols based on their reference designators".
- [x] FINAL PRE-PCB REVIEW 2026-10-13 (hierarchical rev 7): PASS, no schematic changes needed.
      ERC 0/0. 135 nets; all 37 single-pin nets are explicit no-connects. Every footprint resolves and every symbol pin has a pad
      (extra pads: JST-SH MP tabs + USB2512B NC pins 6-9/18-21 only). Pin tables re-checked against datasheet text:
      TMC2209 (p9-10), AP62300 TSOT26 (p3), USB2512B CRFILT/PLLFILT "up to 0.1 uF" (p14, now actually verified) + straps 47-100k (p15).
      AP62300 VFB = 0.800 V (non-T), so 4.98 V / 3.33 V. The AP62300**T** is 0.763 V: do NOT order it.
      Custom hub QFN footprint: CCW numbering, 0.5 mm pitch, EP 3.7 mm with 4 windowed paste apertures (~50%). Add GND vias in layout.
      PCB parity vs old ASTRA.kicad_pcb: +25 footprints, -6 (C10 C18 C20 R17 U4 + stale R24), 11 footprint swaps, 137 pad-net changes.
      Old PCB routing is largely invalid (Q1 S/D, Q2/Q3 TO-92, ESP32 pin moves, crystal). Plan a re-route, not a patch.
      Open items for Alex: BOM-excluded SW1-7/S1 (JST-SH, 1 mm pitch) are hard to hand-solder; mark the USB port roles on silkscreen;
      12 V wiring = VH pin 2 positive; LCSC picks (crystal CL 12 pF, SMAJ15A not CA, USB2512B /M2 SQFN, AP62300WU-7).
- [x] 2026-10-14 IMU moved to its own small board (Alex). Removed J4 (2nd Qwiic), J5 (BNO085 9p cable), R20/C34 (IMU reset RC)
      and R37 (IMU_INT pull-up). J3 (JST-SH 4p: 1 GND, 2 3V3, 3 SDA, 4 SCL) is now the only IMU/I2C connector; the IMU's own
      R/C go on the IMU board. R7/R8 bus pull-ups stay. IMU_INT/IMU_RST deleted: U5 IO25/IO26 are now explicit no-connects.
      Notes on root, MCU pin map and IMU sheet updated. ERC 0/0. PCB updated to match (see PCB_HANDOFF.md).
      Backups: review/backup/*.pre_imu_strip.
- [x] NEW PCB (SELENO v0.3) INSTALLED 2026-10-14:
      - 85×50 mm, 4 layers, 1.6 mm, JLC04161H-7628 stackup; all parts on top.
      - In1 is GND. In2 is +3V3 with +12V and +5V islands. GND pours on F.Cu and B.Cu.
      - Net classes are in `.kicad_pro`.
      - DRC: 0 errors, schematic parity 0.
      - 6 nets are left for Alex to route by hand (SCL, TMC_UART, MOSI, DC, 1EN, UART2).
      - Details: review/PCB_HANDOFF.md. Not committed.
- [x] 2026-10-14/15 REV 8 (cost cut): the USB2512B hub, Y1, CH340C, J6 USB-A, U10/U11 ESD, F2 and 22 passives were removed.
      J7, a 1x10 header to the Pi Zero 2 even row (pins 2-20), was added; the Pi drives the ESP32 UART0 and the Q2/Q3 auto-program pair (GPIO18 = DTR, GPIO23 = RTS).
      The usb.kicad_sch sheet is now pi_link.kicad_sch ("Pi Link"). A +3V3 PWR_FLAG was added (the CH340 had been the rail's only ERC driver).
      ERC 0. The PCB was fully re-laid-out and routed (see PCB_HANDOFF.md), with DRC clean. Full log: review/PI_LINK_LOG.md.
- [x] 2026-10-15 DEEP DESIGN CHECK (every IC vs its datasheet: pinouts, support parts, placement, power paths). No blockers.
      Applied: more buck GND vias (U6/U9 pin 1), 3 extra vias each on the +5V/+3.3V buck outputs, solid plane connection on P1/C1/J7, and C37/C38 changed to 22uF 25V.
      Alex: the LCD BL on IO5 is OK (used before); TMC heat will be handled with heatsinks.
      Then (Alex: "let's do it"): C47 22uF 25V was added between U7/U8 as a VS ceramic, and the PARTS_LIST order-list values were applied to the schematic and board.
      The order-list values are R29/R30 27k/5.1k, R33/R34 47k/15k, R2-R4 330R, and voltage ratings on the caps. The KiCad BOM now matches PARTS_LIST.txt.
      PARTS_LIST.txt was then rewritten with the board as the authority (123 footprints: 76 BOM + 10 excluded + H1-4 + TP1-33).
      It was machine-cross-checked against the board: every ref once, in the right section, quantities matching, 0 problems.
- [x] 2026-10-15 VERSION: everything is now labelled **v0.8** (Alex): the schematic title blocks (were rev "8"), the board title block and silk (were v0.7), the root note, the pcb_build scripts and the PARTS_LIST headers.
      From here on "v0.8" names the current design; "rev 8" in the older entries above is the same schematic before today's fixes. ERC 0, DRC 0/0/parity 0.
      Details are in the top section of PCB_HANDOFF.md. ERC 0, DRC 0/0/parity 0.
- [x] 2026-10-15 POWER CHANGE (Alex: replace the AP62300WU, an extended part, with JLC basic parts): U6 is now a TPS5430DDAR 12V->5V buck and U9 an AMS1117-3.3 LDO fed from +5V.
      Removed: AP62300 x2, L3, C39-C42, R31-R34 (BUCK_EN divider + 3.3 V buck), TP32. Added: D8 SS34, C48-C54, R39. Changed: C36 10nF (boot), R29/R30 15k/4.7k (5.12 V), L2 15uH FXL0840-150-M.
      The all-ceramic output uses TI datasheet Fig 7-11 compensation (C52 1nF, C53 100pF, C54 100nF + R39 330R). 680pF/47pF were dropped because 680pF is extended at JLC.
      A loop model from the datasheet (Eq 15 internal comp, feed-forward gain 25) gives PM 57-72 deg and GM 24-30 dB.
      U6 EN is left open (TI: no resistor to GND). The layout follows TI Fig 7-12. In2 islands were redrawn, the silk title moved to (37, 12), and the BuckSW pattern is now /Power/5V_SW.
      ERC 0, DRC 0/0/parity 0 (35 lib warnings). PARTS_LIST rewritten (122 footprints, 76 BOM parts in 37 lines), cross-check 0 problems. Backups *.pre_tps5430. Details: top of PCB_HANDOFF.md.
- [x] 2026-10-15 Q1 AO3407A -> AO3401A (Alex). Every JLC AO3407A is extended; the AO3401A (C15127) is JLC basic and a drop-in (same SOT-23, G1 S2 D3; the symbol was already AO3401A).
      Vgs limit is ±12 V vs ±20 V; D6 (BZT52C10) holds Vgs at about -10 V, so it stays within rating with ~1.4 V margin. Value and Datasheet fields changed in the schematic and on the board.
      ERC 0, DRC 0/0/parity 0, PARTS_LIST cross-check 0 problems. Backups *.pre_ao3401a.
- [ ] Firmware: DEFERRED – Alex has not asked for software work yet. (Read-only notes: live firmware is on origin/Client/Rpi5, esp32-firmware/;
      it uses STEP/DIR/EN only, no TMC UART. When software is requested: U8 MS1=high gives standalone 1/32 vs U7 1/8, so the UART must set MRES;
      DISPLAY_RES is 12 in display.h but the board now uses IO4; LEDs/buttons now go through the MCP23017; enable the ENN pull-ups → drivers start disabled.)

## Verification outcomes (2026-10-13)
- B6 (CRFILT/PLLFILT 1uF) was **WRONG** (my memory was based on an older datasheet revision). DS00001692C says "up to 0.1 µF or leave unconnected" → downgraded to minor m1 (remove the 10nF caps).
- M3 (TMC STDBY floating) **resolved**: internal pull-down; leaving it floating is allowed.
- M10 OCS_N floating **OK**: internal pull-ups. Downstream VBUS current limit remains S8.
- M2 (ENN) **upgraded to blocker B9**: ENN has internal pull-DOWN, so the drivers are enabled at boot.
- B7 (sense R) confirmed: datasheet says 0.5 W 1206. 0.15 Ω = 1.35 A RMS.
- B8 (3.3V) confirmed: ESP32 module needs ≥0.5 A supply; SPX3819 θJA 191 °C/W.
- L2 value: AP62300 Table 1 for 5 V = 3.3 µH (Würth 7447789003 is correct); 5 V divider 52.3k/10k; 3.3 V = 31.6k/10k + 3.3 µH.
- AO3407A Vgs ±20 V confirmed (AO3401A ±12 V).
- USB2512B pins 6–9 and 18–21 are NC; internal 1 MΩ across the crystal; VBUS_DET→3.3V only allowed for a permanently attached host.
- BNO085: PS0=PS1=0 → I2C, SA0=0 → 0x4A; H_INTN should go to a GPIO; host must support clock stretching.
- JST ratings (PH 2 A / XH 3 A / SH 1 A / VH 10 A) come from catalog knowledge; the PDF digits are not machine-extractable (obfuscated font), so check them visually.
- Tooling gotcha: Git Bash converts args like "/JS" into paths. Use MSYS_NO_PATHCONV=1.

## Parts inventory (from netlist)
ICs: U1 MCP23017-ML (QFN28), U2 USB2512B/M2 (QFN36), U3 CH340C (SOIC16), U4 SPX3819M5-L-3-3 (SOT23-5),
U5 ESP32-WROOM-32UE, U6 AP62300WU (TSOT23-6), U7/U8 TMC2209-LA (QFN28)
Discretes: Q1 AO3401A, Q2/Q3 2N3904 (TO-92!), D1-D5 LEDs 0805, D6 zener (no value/fp), F1 polyfuse 2920 (no rating),
Y1 crystal (no value/fp), L2 (no value), C1 680uF THT, ~37 MLCCs, ~30 resistors
Connectors: P1 12V JST-VH, J1/J2 motor, J3/J4 Qwiic (JST-SH 4p), J6 USB-A stacked, S1 LCD (JST-SH 8p),
SW1-7 off-board buttons (JST-SH 2p), G1 BNO085 header (EXCLUDED FROM BOARD)

## Findings (first pass)
Severity: **B** = blocker (board won't work / damage), **M** = should fix, **m** = minor / nice-to-have

### Blockers
- B1 Y1 24 MHz crystal for USB2512B wired wrong: 18pF caps C23/C24 are in SERIES between chip and crystal; load caps must go crystal-pin→GND. Y1 GND pins (2,4) not tied to GND. No value/footprint.
- B2 U8 (TMC2209 #2) motor outputs OA1/OA2/OB1/OB2 unconnected; J2 unconnected and has no footprint.
- B3 G1 (BNO085) and C14 set "exclude from board" → would not appear on PCB.
- B4 J6 port 1 (USB-A) used as hub UPSTREAM with VBUS1 tied to board +5V → needs A-to-A cable and back-feeds host's VBUS.
- B5 MCP23017 GPA7 used as input for SELECT button – GPA7/GPB7 are output-only on current silicon.
- B6 USB2512B CRFILT/PLLFILT have 100nF+10nF; datasheet requires ~1uF each. (VERIFY)
- B7 TMC2209 sense resistors 0.15R in 0402 – ~0.27 W at 1.35 A RMS, 0402 rated ~0.063 W.
- B8 3.3V rail: SPX3819 (500 mA, SOT-23-5) from 5V – ESP32 Wi-Fi peaks + USB hub + LEDs + IMU likely exceed 500 mA and dissipation ~0.6–0.85 W in SOT-23-5 → thermal shutdown. (VERIFY currents)
- B9 L2 (buck inductor) has no value; symbol is L_Ferrite. D6 zener no value/footprint; F1 no rating.

### Should fix
- M1 Q1 AO3401A Vgs max ±12 V with 12 V input – relies entirely on D6; consider ±20 V part (e.g. AO3407A). Zener ~10 V needed.
- M2 TMC2209 ENN pins have no pull-ups → drivers enabled while ESP32 boots/resets. Add 10k to 3.3V.
- M3 TMC2209 STDBY pins floating. (VERIFY internal pull-down)
- M4 ESP32 IO2 drives LCD RES – IO2 must be low/floating for download mode; if LCD module pulls RES high, flashing fails.
- M5 BNO085 INT and RST not connected to ESP32 (INT goes nowhere, RST only RC). Free pins: IO4, IO36(VP), IO39(VN).
- M6 Q2/Q3 2N3904 in TO-92 THT – switch to MMBT3904 SOT-23 (use MMBT3904 symbol, different pinout).
- M7 J1 symbol 1x04 but footprint 6-pin JST-PH; PH rated 2 A. Pick one connector for both motors (e.g. JST-XH 4p).
- M8 USB2512B decoupling: 6 VDD pins need 100nF each (+bulk); only ~3 nearby (C14 is off-board).
- M9 USB2512B VBUS_DET tied to 3.3V (always attached) – fine only if upstream redesign accounts for it.
- M10 Downstream USB VBUS (J6 VBUS2) direct to +5V, no current limit/fuse; OCS_N1/2 floating. (VERIFY internal pull-ups)
- M11 Voltage ratings/MPNs missing on all passives; 12V caps need ≥25 V; 0402 2.2uF on TMC 5VOUT derates heavily.
- M12 AP62300 FB divider 12k/2.2k → 5.16 V (VERIFY Vref); EN divider 47k/6.8k → UVLO ≈ 9.3 V (VERIFY threshold).
- M13 LED resistors 100R from 3.3V: red/yellow ~13 mA, blue/white/green (Vf≈3 V) ~0–3 mA → very uneven brightness.

### Minor
- m1 ERC: VCC label merged with +3.3V; AD0 label merged with GND – rename labels.
- m2 Stray "VALUE" text bottom-left of sheet.
- m3 MCP23017 INTA/INTB unconnected → buttons must be polled.
- m4 No ESD protection on external USB ports / Qwiic.
- m5 ESP32: Espressif recommends 22uF bulk near module VDD (only 100nF local + 10uF at LDO).
- m6 TMC UART: separate UARTs AND distinct addresses – could share one UART (addr 0/1) and free 2 GPIOs.
- m7 LCD backlight on IO5 directly – check module (direct LED anode vs transistor).

## Datasheet download log
(see datasheets/MANIFEST.md once created)
