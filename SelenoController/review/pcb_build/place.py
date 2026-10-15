"""Placement for SELENO v0.8 (85 x 50 mm, all parts on top).

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
put('J7', 8.4, 2.2, 90)                     # Pi link 1x10 (pin 1 = 5V at the left, pins run east to u 31.26)
put('C12', 9.7, 5.4, 0)                     # +5V decoupling at J7 pins 1/2
put('J3', 43.24, 2.75, 0)                   # I2C cable to the IMU board
put('S1', 53.35, 2.75, 0)                   # LCD
put('SW7', 75.46, 2.75, 0)                  # reset button cable

# bottom edge: button cables, ordered to match the MCP23017 pins
for ref, u in (('SW5', 40.34), ('SW6', 46.62), ('SW1', 52.9), ('SW2', 59.18), ('SW3', 65.46), ('SW4', 71.74)):
    put(ref, u, 47.25, 180)

put('P1', 4.45, 29.0, 90)                   # 12 V in, left edge (pin 2 = +12V at the top)


# ---------------------------------------------------------------- motor driver blocks (driver rotated 180, motor side down)
# Every support part sits outside DRV_ENV, the footprint envelope of the leaded alternative (TMC2226-SA,
# HTSSOP-28 4.4x9.7 mm, long side horizontal, courtyard 10.3 x 7.8), so the QFN can be swapped later
# without moving anything else.
DRV_ENV = (5.3, 4.1)                # half-width, half-height around the driver centre
ENVELOPES = {}                      # ref -> (u0, v0, u1, v1, what): keep-clear areas for leaded replacements


def driver(cx, cy, U, Rbra, Rbrb, Ccp, Cvcp, C5v, Cio, Cvs, J):
    block(cx, cy, 0, [
        (U, 0, 0, 180),
        (Rbra, -3.03, 5.3, 0),      # pad 2 (BRA) east, GND pad west
        (Rbrb, 3.03, 5.3, 180),     # pad 2 (BRB) west, GND pad east
        (Ccp, 6.0, -0.25, 90),      # CPO south / CPI north
        (Cvcp, 6.4, -2.0, 0),       # VCP west / VS east
        (C5v, 2.6, -5.0, 0),        # 5VOUT west / GND east
        (Cio, -3.72, -5.1, 90),     # +3V3 south / GND north
        (Cvs, -5.9, 2.3, 90),       # VS decoupling
        (J, -5.5, 10.0, 0),
    ])
    ENVELOPES[U] = (cx - DRV_ENV[0], cy - DRV_ENV[1], cx + DRV_ENV[0], cy + DRV_ENV[1], 'TMC2226-SA HTSSOP-28')


DRV_Y = 36.55                       # (motor connectors stay at v 46.55)
driver(14.6, DRV_Y, 'U7', 'R25', 'R26', 'C28', 'C26', 'C27', 'C3', 'C2', 'J1')
driver(30.0, DRV_Y, 'U8', 'R27', 'R28', 'C30', 'C29', 'C31', 'C4', 'C32', 'J2')
put('C47', 21.9, 39.9, 90)                  # shared 22uF VS ceramic between the drivers (vias are hand-added on the board)
# ENN pull-ups sit at the ESP32 end of 1EN / 2EN (keeps +3V3 pads out of the +12V plane area)
put('R35', 72.47, 28.1, 90)                 # under U5 pin 33: 1EN north, +3V3 south
put('R36', 60.3, 6.2, 0)                    # 2EN pull-up, beside the module's top-left corner (keeps the pin-escape strip free)
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


U6_AT = (19.0, 11.5)                        # 5 V buck, top-left under J7 (input from the +12V corridor below)
U9_AT = (38.0, 11.5)                        # 3.3 V buck, beside U6: one power strip under J7 (fed by its own +12V corridor)
buck(*U6_AT, 'U6', 'C35', 'C36', 'L2', 'C37', 'C38', 'R29', 'R30')       # 5 V
buck(*U9_AT, 'U9', 'C39', 'C40', 'L3', 'C41', 'C42', 'R33', 'R34')       # 3.3 V
put('R31', 16.4, 18.0, 0)                   # BUCK_EN divider (shared by both bucks), in the +12V corridor
put('R32', 16.4, 19.1, 180)                 # BUCK_EN east, GND west
put('C15', 33.2, 17.3, -90)                 # +3.3V bulk at the 3.3 V buck output
put('C16', 35.0, 17.3, -90)

# ---------------------------------------------------------------- ESP32 programming / debug group (bottom-middle, under the
# module's IO0 / TX / RX corner): auto-program pair driven by the Pi (J7 DTR/RTS) + one 2x4 test-pad block
put('R22', 51.5, 30.6, 0)
put('Q3', 51.5, 33.0, 0)                    # RTS -> EN
put('Q2', 51.5, 37.0, 0)                    # DTR -> IO0
put('R23', 51.5, 39.4, 0)

# ---------------------------------------------------------------- ESP32 and its support parts
put('U5', 70.41, 16.79, -90)                # antenna end (u.FL) towards the right edge; 7 mm free channel on its left
put('C11', 76.9, 6.25, 0)
put('R6', 73.5, 6.25, 0)
put('C43', 81.6, 10.0, 90)
put('C7', 81.6, 13.4, 90)
put('R7', 81.8, 16.2, 0)
put('R8', 81.8, 17.3, 0)
put('R16', 69.3, 28.0, 180)               # TMC_TX / TMC_UART, under U5 pins 30/31

# ---------------------------------------------------------------- I/O expander, LEDs, buttons
put('U1', 70.5, 36.5, 180)
# keep-clear square for the leaded MCP23017-E/SS (SSOP-28 5.3x10.2, courtyard 9.5 x 10.8) in either rotation
ENVELOPES['U1'] = (70.5 - 5.4, 36.5 - 5.4, 70.5 + 5.4, 36.5 + 5.4, 'MCP23017-E/SS SSOP-28')
put('C6', 77.2, 34.6, 90)
put('R19', 69.2, 29.6, 90)                  # MCP reset pull-up / RC (C33 is 0805) and EXP_INT pull-up,
put('C33', 66.8, 29.9, 0)                   # all above the U1 envelope and below R16
put('R38', 64.0, 28.6, 0)
for i, (led, res) in enumerate((('D4', 'R4'), ('D5', 'R5'), ('D3', 'R3'), ('D2', 'R2'), ('D1', 'R1'))):
    v = 30.0 + 2.5 * i                      # top to bottom: blue, yellow, white, green, red
    put(led, 82.4, v, 180)                  # anode west, cathode (GND) east
    put(res, 79.6, v, 180)                  # LED side east

# ---------------------------------------------------------------- mounting holes and test pads (board-only)
HOLES = [('H1', 3.5, 3.5), ('H2', 81.5, 3.5), ('H3', 3.5, 46.5), ('H4', 81.5, 46.5)]
TESTPADS = [  # ref, net, u, v, label, label side (l / r = left / right, u / d1 = above / below, d = vertical below)
    # power: 12V + GND at the input, 5V + GND at the 5 V buck, 3V3 + GND at the 3.3 V buck
    ('TP1', '+12V', 1.7, 34.0, '12V', 'u'), ('TP2', 'GND', 1.7, 36.6, 'GND', 'd1'),
    ('TP3', '+5V', 2.6, 10.9, '5V', 'r'), ('TP4', 'GND', 2.6, 13.4, 'GND', 'r'),
    ('TP5', '+3.3V', 41.6, 17.6, '3V3', 'd1'), ('TP6', 'GND', 44.2, 17.6, 'GND', 'd1'),
    # I2C, beside J3
    ('TP7', 'SCL', 34.3, 2.2, 'SCL', 'd1'), ('TP8', 'SDA', 37.0, 2.2, 'SDA', 'd1'),
    # LCD column, left of the S1 -> ESP32 channel (not in it)
    ('TP9', 'SCK', 47.0, 9.6, 'SCK', 'r'), ('TP10', 'MOSI', 47.0, 11.8, 'MOSI', 'r'), ('TP11', 'DC', 47.0, 14.0, 'DC', 'r'),
    ('TP12', 'RES', 47.0, 16.2, 'RES', 'r'), ('TP13', 'BL', 47.0, 18.4, 'BL', 'r'),
    # ESP32 programming / debug block (2 x 4) next to Q2/Q3
    ('TP14', 'GND', 55.0, 31.0, 'GND', 'd1'), ('TP15', '+3.3V', 57.6, 31.0, '3V3', 'd1'),
    ('TP16', 'UART1', 60.2, 31.0, 'TX', 'd1'), ('TP17', 'UART2', 62.8, 31.0, 'RX', 'd1'),
    ('TP18', 'ESP32EN', 55.0, 34.0, 'EN', 'd1'), ('TP19', 'GPIO0', 57.6, 34.0, 'IO0', 'd1'),
    ('TP20', '/Pi Link/DTR', 60.2, 34.0, 'DTR', 'd1'), ('TP21', '/Pi Link/RTS', 62.8, 34.0, 'RTS', 'd1'),
    # motor block (2 x 5) above C1, beside the ESP32 -> driver bus
    ('TP22', '1STEP', 33.5, 26.8, '1STEP', 'u'), ('TP23', '1DIR', 36.9, 26.8, '1DIR', 'u'), ('TP24', '1EN', 40.3, 26.8, '1EN', 'u'),
    ('TP25', 'T1_DIAG', 43.7, 26.8, '1DIAG', 'u'), ('TP26', 'TMC_UART', 47.1, 26.8, 'UART', 'u'),
    ('TP27', '2STEP', 33.5, 29.2, '2STEP', 'd1'), ('TP28', '2DIR', 36.9, 29.2, '2DIR', 'd1'), ('TP29', '2EN', 40.3, 29.2, '2EN', 'd1'),
    ('TP30', 'T2_DIAG', 43.7, 29.2, '2DIAG', 'd1'), ('TP31', 'TMC_TX', 67.25, 27.75, 'TTX', 'l'),   # beside R16 (its block slot was walled in)
    # misc
    ('TP32', '/Power/BUCK_EN', 19.0, 18.55, 'BEN', 'r'), ('TP33', 'EXP_INT', 61.6, 28.6, 'INT', 'l'),
]
