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
