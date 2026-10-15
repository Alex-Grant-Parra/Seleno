# ASTRA Controller – Schematic Review (pre-PCB)

**Schematic:** `ASTRA.kicad_sch` rev 6 · **Reviewed:** 2026-10-13 · **Verified against:** datasheets in `../datasheets/`

**Verdict: not ready for layout yet.** There are 9 blockers and about 12 should-fix items. Most are quick schematic edits. The 3.3 V rail and the USB upstream port need a design decision from Alex first.

Severity:
- 🔴 **Blocker:** the board won't work, or something gets damaged.
- 🟠 **Should fix:** works badly, out of spec, or risky.
- 🟢 **Minor:** cleanup or nice-to-have.

---

## 🔴 Blockers

| # | Where | Problem | Fix | Source |
|---|---|---|---|---|
| B1 | Y1, C23, C24 (USB hub crystal) | **Crystal is miswired.** The 18 pF caps sit *in series* between the USB2512B and the crystal: XTALIN–C24–Y1–C23–XTALOUT. Y1's case pins 2 and 4 aren't grounded (one is flagged unconnected by ERC). Y1 also has no value or footprint. The hub won't clock, so USB won't work. | Connect Y1 pin 1 to XTALIN and pin 3 to XTALOUT directly. Run C24 from XTALIN to GND and C23 from XTALOUT to GND. Tie Y1 pins 2 and 4 to GND. Choose a 24 MHz fundamental-mode crystal, e.g. a 3225 4-pad part with CL ≈ 12 pF, which suits 18 pF caps. No external 1 MΩ is needed because it's internal. | USB251xB DS §7.1 |
| B2 | U8, J2 | **The second stepper isn't wired.** U8's OA1/OA2/OB1/OB2 are unconnected, and J2 has no connections and no footprint. | Wire U8 outputs to J2 the same way U7 is wired to J1, and give J2 a footprint (see S6). | ERC |
| B3 | G1 (BNO085), C14 | **Both are set to "Exclude from board"**, so neither would appear on the PCB. G1 is also excluded from the BOM. | Symbol Properties → untick *Exclude from board* (and *Exclude from BOM* on G1). | schematic |
| B4 | J6 port 1, USB2512B upstream | **The upstream port is a USB-A socket, and its VBUS1 is tied to the board's +5 V.** Connecting a PC needs an A-to-A cable, and the board's 5 V buck would then back-feed the PC's USB port. Separately, VBUS_DET tied to 3.3 V is only allowed for a *permanently attached* host. A cable-connected host needs a 100k/100k divider from upstream VBUS. | Make the upstream port a USB-C receptacle (5.1 k CC pull-downs) or a USB-B/micro-B. Don't connect its VBUS to +5 V; feed it only to VBUS_DET through the 100k/100k divider. Keep J6 as a single (or stacked) downstream USB-A. **Decision needed** (see questions). | USB251xB DS Table 3-2 (VBUS_DET) |
| B5 | U1 GPA7, SELECT button | **GPA7 and GPB7 are output-only on the MCP23017**, so the SELECT button can never be read. | Move SELECT to a free pin (GPA2–GPA6 are unused). GPB7 driving the WHITE LED is fine. | MCP23017 DS p.1 |
| B6 | R25–R28 (TMC2209 sense resistors) | **0.15 Ω in 0402.** At the 1.35 A RMS this value sets, each resistor dissipates about 0.27 W. An 0402 is rated about 0.063 W, so they'd burn up or drift. | Use **1206, 0.5 W, 1 %, low-inductance** (thick-film is fine). The datasheet says "0.5 W type sufficient up to 1.2 A RMS" and its reference design uses 1206 0.5 W. | TMC2209 DS §8 |
| B7 | U4 SPX3819 (3.3 V rail) | **The 3.3 V regulator is too small.** Espressif requires the 3.3 V supply to deliver **≥ 0.5 A for the ESP32 module alone**, but the whole SPX3819 is rated 500 mA. Rail loads: ESP32 (Wi-Fi peaks about 0.5 A), USB2512B, CH340C, MCP23017 plus LEDs (up to 65 mA), BNO085, LCD, and Qwiic devices. Thermally, (5 − 3.3) V × 0.4 A = 0.68 W × 191 °C/W gives about 130 °C of rise, which trips thermal shutdown. | Replace it with a **second AP62300 configured for 3.3 V, fed from +12 V**. That means the same IC and the same 3.3 µH inductor as U6. Values from datasheet Table 1: R1 = 31.6 k, R2 = 10 k, L = 3.3 µH, Cin = 10 µF/25 V, Cout = 2 × 22 µF, BST = 100 nF. You could also drop the EN divider and leave EN floating for auto-start. | ESP32-WROOM-32E DS Table 14; SPX3819 DS; AP62300 DS Table 1 |
| B8 | L2, D6, F1, Y1, J2 | **Parts with no value or footprint.** **L2** has no value and uses a ferrite-bead symbol. **D6** has no value and no footprint. **F1** has no current rating. Y1 and J2 are covered above. | **L2:** use **3.3 µH** (datasheet Table 1 for 5 V); the Würth 7447789003 already named in the symbol (3.3 µH, Isat 4.2 A) is correct. Change the symbol to `Device:L`. **D6:** 10 V zener, e.g. BZT52C10 (SOD-123). **F1:** 2920 PTC, about 2.5–3 A hold, ≥ 16 V. | AP62300 DS Table 1 |
| B9 | U7/U8 ENN | **The drivers are enabled at power-up.** ENN has an internal **pull-down**, and the ESP32 pins float during boot and reset. Both motors energise at the default full current (IRUN = 31) before firmware configures them. The datasheet says "Start with ENN=high!" | Add a 10 kΩ pull-up from each ENN (nets 1EN and 2EN) to +3.3 V. | TMC2209 DS pin table and Fig 3.1 |

## 🟠 Should fix

| # | Where | Issue | Recommendation |
|---|---|---|---|
| S1 | Q1 AO3401A (reverse-polarity PFET) | Vgs max is **±12 V** on a 12 V input, so all of the margin depends on D6. | Swap to **AO3407A**: same SOT-23 pinout, Vgs **±20 V**, −30 V, −4.3 A, < 48 mΩ. Keep D6 as a 10 V clamp. The FET orientation (drain to input, source to load) is correct. |
| S2 | ESP32 IO2 → LCD RES | IO2 is a boot strap and must be low or floating to flash. If the LCD module pulls RES high, auto-programming fails. | Move LCD RES to **IO4**. |
| S3 | BNO085 INT and RST | INT goes nowhere, and RST is only an RC (R20 and C34). CEVA says H_INTN "should be tied to a GPIO", and resetting the IMU from software is very useful. | INT → **IO36 (SENSOR_VP)**, which is input-only and fine for INT. RST → a free GPIO (see S10), then reduce C34 to 100 nF. |
| S4 | Q2/Q3 2N3904 TO-92 (through-hole) | These are the only through-hole semiconductors on an SMD board. | Use **MMBT3904 (SOT-23)**. Swap the **symbol** to `Transistor_BJT:MMBT3904`, not just the footprint, because the SOT-23 pinout differs from TO-92. |
| S5 | AP62300 FB divider (R29/R30) | 12k/2.2k gives **5.16 V nominal**. With 1 % parts and Vref tolerance the worst case is about 5.3 V, which is above the USB VBUS maximum of 5.25 V. | Use the datasheet values **R29 = 52.3 k, R30 = 10 k** (4.98 V). Optionally add a 10–100 pF feed-forward cap across R29. |
| S6 | J1, J2 motor connectors | J1's symbol is 1×04 but its footprint is a 6-pin JST-PH, so pads 5 and 6 are unconnected. JST-PH is rated 2 A, marginal for 1.35 A RMS with 1.9 A peaks. J2 has no footprint. | Use the **same 4-pin connector for both**, e.g. **JST-XH B4B-XH-A** (3 A, the 3D-printer standard). |
| S7 | Capacitor ratings | No voltage or dielectric is specified anywhere. | +12 V caps (C2, C32, C35, C1) **≥ 25 V**; C1 should be a low-ESR electrolytic. CPO–CPI caps C28/C30 are **22 nF 50 V** (TMC datasheet). TMC 5VOUT C27/C31: 0402 2.2 µF loses most of its capacitance at 5 V, so use **0603/0805, ≥ 10 V, X5R/X7R**. |
| S8 | J6 downstream VBUS | Downstream VBUS connects straight to +5 V, so a shorted USB device browns out the whole board. | Add a **PTC (about 0.75 A hold, 1206)** in series, or a power switch (e.g. AP2553) driven by PRTPWR1 with OCS_N1. OCS_N has an internal pull-up, so leaving it floating is OK. |
| S9 | USB2512B decoupling | There are 6 supply pins (VDD33 ×2, VDDA33 ×4). Only about 3 nearby 100 nF caps exist, one of which (C14) is off-board. | Use one 100 nF per supply pin plus one 4.7–10 µF bulk cap. Ground the exposed pad with several vias; it is the chip's only VSS. |
| S10 | GPIO budget | Fixing S2, S3 and m4 needs about 4 GPIOs, and only IO4, IO36 and IO39 are cleanly free (IO12 is a strap pin). | **Run both TMC2209s on one UART.** They already have distinct addresses (U7 = 0, U8 = 1). That frees **IO25 and IO26**. |
| S11 | ESP32 bulk capacitance | Espressif's reference design uses 22 µF + 0.1 µF at the module's 3V3 pin. Here there is only 100 nF locally, plus 10 µF at the LDO. | Add **22 µF** next to U5 pin 2. |
| S12 | LED resistors R1–R5 (100 Ω from 3.3 V) | Red and yellow (Vf about 2 V) run at about 13 mA. Blue, white and green (Vf about 3 V) get 0–3 mA. Brightness will be very uneven. | Choose per colour: about 150–220 Ω for red and yellow, 22–47 Ω for blue, white and green. Or confirm the LED part numbers first. The total stays under the MCP23017's 125 mA. |

## 🟢 Minor

- **m1 CRFILT/PLLFILT:** the datasheet allows "up to 0.1 µF" on each. You have 100 nF + 10 nF, which is slightly over. Delete C18 and C20.
- **m2 SPX3819 BP cap:** the datasheet value is 10 nF, not 100 nF. This doesn't matter if U4 is replaced (B7).
- **m3 ERC warnings:** the `VCC` label sits on the +3.3 V net and the `AD0` label sits on GND. Rename or delete them.
- **m4 MCP23017 INTA:** it's unconnected, so the buttons must be polled. Wire it to IO39, which is input-only, in push-pull INT mode.
- **m5 ESD:** add a USBLC6-2SC6 on the external USB data lines. Optionally add ESD protection on the Qwiic lines too.
- **m6 +12 V input TVS:** SMAJ15A or similar. Its clamp of about 24 V is below the TMC2209's 29 V VS maximum, which protects against hot-plug spikes.
- **m7 LCD backlight on IO5:** check the module. If BL is the LED anode itself, drive it through a small MOSFET or transistor.
- **m8 P1 symbol:** P1 uses a barrel-jack symbol on a JST-VH footprint, which is confusing. Change the symbol to `Conn_01x02` and confirm the harness has pin 1 = GND.
- **m9 Stray text:** delete the stray "VALUE" text at the bottom-left of the sheet.
- **m10 Part numbers:** none of the parts have MPNs or LCSC numbers. These are needed before ordering assembly.
- **m11 Firmware note:** the BNO085 uses I²C clock stretching, so set the ESP32 I²C timeout accordingly.

## ✅ Verified OK

These were checked against the datasheets:

**Power input and buck (U6)**
- The reverse-polarity PFET topology and orientation are correct.
- AP62300 pinout ✓. BST cap 100 nF ✓. Cin 10 µF and Cout 2 × 22 µF match Table 1 ✓.
- The EN divider gives a deliberate UVLO of about 9.4 V on and 8.4 V off ✓.

**ESP32 (U5)**
- EN RC is 10 k / 1 µF, as Espressif recommends ✓.
- The auto-program circuit (two cross-coupled NPNs on DTR/RTS) is correct ✓.
- IO12 (MTDI) is left floating ✓.
- DIAG lines go to the input-only IO34/IO35 ✓.

**USB-serial (U3, CH340C)**
- V3 is tied to VCC at 3.3 V, as the datasheet requires ✓.
- TX/RX are crossed to the ESP32's UART0 ✓.

**USB hub (U2, USB2512B)**
- RBIAS is 12 k (specify 1 %) ✓. TEST goes to GND ✓. RESET_N RC ✓.
- CFG_SEL = 00 (default config, self-powered) ✓. NON_REM = 00 ✓. BC_EN pulled low ✓.
- OCS_N has internal pull-ups ✓. Pins 6–9 and 18–21 are NC on the 2512B ✓.
- The 1 MΩ crystal resistor is internal ✓.

**Stepper drivers (U7/U8, TMC2209)**
- Charge-pump caps 22 nF / 100 nF ✓. 5VOUT 2.2 µF ✓.
- VREF = 3.3 V is fine because ≥ 2.5 V means full scale ✓.
- STDBY has an internal pull-down, so floating is OK ✓. CLK to GND ✓.
- Single-wire UART through 1 k ✓. UART addresses are 0 and 1 ✓.
- 0.15 Ω gives 1.35 A RMS, suitable for a 1.5 A NEMA17 ✓.

**I/O expander (U1, MCP23017)**
- Address is 0x20 ✓. RESET pull-up ✓. LED current is within per-pin and package limits ✓.

**I²C and sensors**
- I²C pull-ups are 4.7 k ✓.
- J3/J4 match the Qwiic pinout (GND, 3V3, SDA, SCL) ✓.
- BNO085 is strapped for I²C (PS0 = 0, SA0 = 0, address 0x4A) ✓.

---

## Questions for Alex (these decide B4, B7 and part of the BOM)

1. **USB host:** What plugs into the board as host (a Pi, a laptop)? Does it need the hub at all? If only the ESP32 needs USB, removing U2, Y1 and about 15 passives simplifies the board a lot.
2. **IMU:** Should the BNO085 breakout sit on this board (G1 header), or plug in through a Qwiic cable (J3/J4)?
3. **Second motor:** Is J2/U8 a real second axis (alt-az), or optional?
4. **Assembly:** Is this going to JLCPCB/PCBWay assembly? If so, I'll pick LCSC part numbers, preferring basic parts.
