"""Placement for SELENO v0.7 (85 x 50 mm, all parts on top).

Coordinates are board-local (u right, v down, origin = top-left board corner), mm.
Rotation is KiCad footprint orientation in degrees.
"""
from common import rot

P = {}   # ref -> (u, v, rot)


def put(ref, u, v, r=0):
    r = r % 360
    P[ref] = (round(u, 3), round(v, 3), r if r <= 180 else r - 360)


def block(ou, ov, brot, items):
    """Place items given in block coordinates (du, dv, drot) around origin (ou, ov) rotated by brot."""
    for ref, du, dv, dr in items:
        x, y = rot(du, dv, brot)
        put(ref, ou + x, ov + y, dr + brot)


# ---------------------------------------------------------------- edges / connectors
put('J6', 19.0, 13.55, -90)                 # stacked USB-A, opening on the top edge (port 1 = Pi, port 2 = camera)
put('J4', 35.14, 2.75, 0)                   # Qwiic
put('J3', 43.24, 2.75, 0)                   # Qwiic
put('S1', 53.35, 2.75, 0)                   # LCD
put('J5', 65.95, 2.75, 0)                   # BNO085 cable
put('SW7', 75.46, 2.75, 0)                  # reset button cable

# bottom edge: button cables, ordered to match the MCP23017 pins
for ref, u in (('SW5', 40.34), ('SW6', 46.62), ('SW1', 52.9), ('SW2', 59.18), ('SW3', 65.46), ('SW4', 71.74)):
    put(ref, u, 47.25, 180)

put('P1', 4.45, 29.0, 90)                   # 12 V in, left edge (pin 2 = +12V at the top)


# ---------------------------------------------------------------- motor driver blocks (driver rotated 180, motor side down)
def driver(cx, cy, U, Rbra, Rbrb, Ccp, Cvcp, C5v, Cio, Cvs, J):
    block(cx, cy, 0, [
        (U, 0, 0, 180),
        (Rbra, -3.03, 4.6, 0),      # pad 2 (BRA) east, GND pad west
        (Rbrb, 3.03, 4.6, 180),     # pad 2 (BRB) west, GND pad east
        (Ccp, 3.7, -0.25, 90),      # CPO south / CPI north
        (Cvcp, 4.4, -1.9, 0),       # VCP west / VS east
        (C5v, 2.6, -3.95, 0),       # 5VOUT west / GND east
        (Cio, -4.2, -2.2, 180),     # +3V3 east / GND west
        (Cvs, -4.6, 2.3, 90),       # VS decoupling
        (J, -5.5, 8.5, 0),
    ])


driver(14.6, 38.05, 'U7', 'R25', 'R26', 'C28', 'C26', 'C27', 'C3', 'C2', 'J1')
driver(30.0, 38.05, 'U8', 'R27', 'R28', 'C30', 'C29', 'C31', 'C4', 'C32', 'J2')
# ENN pull-ups sit at the ESP32 end of 1EN / 2EN (keeps +3V3 pads out of the +12V plane area)
put('R35', 70.47, 28.1, 90)                 # under U5 pin 33: 1EN north, +3V3 south
put('R36', 62.85, 6.15, 0)                  # above U5 pin 12: +3V3 west (to J5 pin 1), 2EN east
put('C1', 41.0, 38.6, 0)                    # 680 uF bulk next to the drivers (+ west)

# ---------------------------------------------------------------- input protection
put('F1', 14.6, 26.5, 180)                  # pad 2 (from P1) west, pad 1 (to Q1) east
put('Q1', 21.5, 26.5, 180)                  # drain west, source NE, gate SE
put('D6', 21.6, 30.0, 0)                    # K (+12V) west, A (gate) east
put('R21', 21.5, 31.9, 180)
put('D7', 4.5, 36.0, -90)                   # TVS: K (+12V) north


# ---------------------------------------------------------------- buck converters (same layout twice)
def buck(cx, cy, U, Cin, Cbst, L, Co1, Co2, Rtop, Rbot):
    block(cx, cy, 0, [
        (U, 0, 0, 0),
        (Cbst, 0.0, -2.2, 0),       # SW west / BST east
        (Cin, 0.2, 3.0, 180),       # +12V west (under VIN), GND east
        (L, -6.5, 0, 180),          # pad 1 (SW) east, pad 2 (VOUT) west
        (Co1, -9.7, 5.8, -90),      # VOUT north, GND south
        (Co2, -7.5, 5.8, -90),
        (Rbot, 3.4, 1.2, -90),      # FB north, GND south
        (Rtop, 4.5, 1.2, 90),       # FB north, VOUT south
    ])


buck(45.0, 25.5, 'U6', 'C35', 'C36', 'L2', 'C37', 'C38', 'R29', 'R30')     # 5 V
buck(59.6, 34.0, 'U9', 'C39', 'C40', 'L3', 'C41', 'C42', 'R33', 'R34')     # 3.3 V
put('R31', 48.8, 23.4, 0)                   # BUCK_EN divider (shared by both bucks)
put('R32', 48.8, 22.3, 0)
put('C12', 18.3, 21.2, 0)                   # +5V rail by the USB connector
put('C15', 56.0, 40.6, 0)                   # +3.3V rail
put('C16', 59.0, 40.6, 0)

# ---------------------------------------------------------------- USB: ESD, hub, crystal, CH340
put('U10', 30.1, 10.6, 90)                  # Pi port ESD, pair flows north -> south through it
put('U11', 26.9, 23.0, 180)                 # camera port ESD, pair flows west -> east
put('F2', 22.3, 20.3, 0)                    # camera VBUS PTC (+5V west, VBUS_CAM east)
put('U2', 35.5, 14.0, 90)                   # USB2512B hub (upstream + crystal on the west side)
put('Y1', 28.5, 16.3, 90)                   # XO top-left, XI bottom-right
put('C23', 26.2, 15.6, -90)
put('C24', 29.35, 19.45, -90)
put('C19', 31.35, 19.0, 90)                 # PLLFILT
put('R9', 32.45, 19.6, -90)                 # RBIAS
put('C45', 30.3, 21.3, 180)                 # VDDA36
# hub straps above the chip (pin order left->right: RESET_N, HS_IND, SCL, VDD33, SDA)
for ref, u in (('R10', 33.2), ('R15', 34.3), ('R11', 35.4), ('C14', 36.5), ('R18', 37.6)):
    put(ref, u, 8.6, 90)
put('R13', 31.9, 7.4, 90)                   # SUSP_IND strap
put('C44', 39.05, 7.9, 0)                    # VDDA29 / spare 3V3 decoupling
put('C25', 29.45, 3.0, 0)                    # hub reset delay cap
# hub east side (pin order top->bottom: PRTPWR2, VDD33, CRFILT, PRTPWR1, VDDA10)
for ref, v in (('R12', 12.4), ('C21', 13.5), ('C17', 14.6), ('R14', 15.7), ('C22', 16.8)):
    put(ref, 40.6, v, 180 if ref == 'C17' else 0)
put('C46', 36.6, 19.0, -90)                 # hub bulk (VDDA5)

put('U3', 47.0, 15.0, 90)                   # CH340C: USB at the bottom, RTS/DTR at the top
put('C9', 42.6, 10.2, 0)
put('C13', 46.4, 19.9, -90)
put('Q3', 55.0, 13.0, 0)                    # RTS -> EN
put('Q2', 55.0, 17.0, 0)                    # DTR -> IO0
put('R22', 55.0, 10.6, 0)
put('R23', 55.0, 19.4, 0)

# ---------------------------------------------------------------- ESP32 and its support parts
put('U5', 68.41, 16.79, -90)                # antenna end (u.FL) towards the right edge
put('C11', 76.9, 6.25, 0)
put('R6', 73.5, 6.25, 0)
put('C43', 81.6, 10.0, 90)
put('C7', 81.6, 13.4, 90)
put('R7', 80.6, 16.2, 0)
put('R8', 80.6, 17.3, 0)
put('R37', 80.6, 18.4, 0)
put('R20', 80.6, 19.5, 0)
put('C34', 80.6, 20.6, 0)
put('R16', 67.3, 28.0, 180)

# ---------------------------------------------------------------- I/O expander, LEDs, buttons
put('U1', 70.5, 36.5, 180)
put('C6', 75.4, 34.6, 90)
put('R19', 68.6, 30.9, 90)
put('C33', 66.9, 30.9, 90)
put('R38', 65.6, 35.2, 0)
for i, (led, res) in enumerate((('D4', 'R4'), ('D5', 'R5'), ('D3', 'R3'), ('D2', 'R2'), ('D1', 'R1'))):
    v = 30.0 + 2.5 * i                      # top to bottom: blue, yellow, white, green, red
    put(led, 82.4, v, 180)                  # anode west, cathode (GND) east
    put(res, 79.6, v, 180)                  # LED side east

# ---------------------------------------------------------------- mounting holes and test pads (board-only)
HOLES = [('H1', 3.5, 3.5), ('H2', 81.5, 3.5), ('H3', 3.5, 46.5), ('H4', 81.5, 46.5)]
TESTPADS = [  # ref, net, u, v
    ('TP1', '+12V', 1.7, 34.0), ('TP5', 'GND', 1.7, 36.6),            # left edge by the TVS
    ('TP2', '+5V', 2.6, 10.9), ('TP4', 'GND', 2.6, 13.4),             # left edge by the USB ports
    ('TP3', '+3.3V', 83.45, 12.8),
    ('TP6', 'ESP32EN', 83.2, 15.0), ('TP7', 'GPIO0', 83.2, 17.5), ('TP8', 'UART1', 83.2, 20.0),
    ('TP9', 'UART2', 83.2, 22.5), ('TP10', 'GND', 83.2, 25.0),
    ('TP11', 'SCL', 26.0, 2.2), ('TP12', 'SDA', 26.0, 4.6), ('TP13', 'TMC_UART', 22.6, 35.6),   # between the drivers, on the shared TMC UART
]
