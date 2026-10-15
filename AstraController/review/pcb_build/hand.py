"""Hand routes for the critical areas (board-local mm). Everything else is left to route.py.

Each section adds tracks/vias to a Ctx. Coordinates for the repeated blocks (drivers, bucks) are written
relative to the block origin used in place.py.
"""
from field import F, B

NK = 0.25          # neck width out of 0.5 mm pitch QFN pads
VIA = (0.5, 0.3)
VIA_T = (0.45, 0.3)   # tight vias: same copper, 0.3 mm drill (no holes under 0.3 mm)
VIA_P = (0.6, 0.3)  # power vias (+12V, motor)


class H:
    def __init__(self, ctx):
        self.c = ctx

    def net(self, padname):
        return self.c.pad(padname).net

    def P(self, padname):
        return self.c.pad(padname).pos

    def t(self, net, layer, pts, w):
        self.c.polyline(net, layer, pts, w, 'hand')

    def v(self, net, p, size=VIA):
        self.c.add_via(net, p, size[0], size[1], 'hand')


def rel(cx, cy):
    return lambda pts: [(round(cx + u, 4), round(cy + v, 4)) for u, v in pts]


# ------------------------------------------------------------------ TMC2209 blocks
def driver(h, cx, cy, U, J, Rbra, Rbrb, C5v, Cio, Cvcp, Ccp, ms1_3v3):
    R = rel(cx, cy)
    n = lambda pin: h.net(f'{U}.{pin}')
    # motor outputs (neck out of the pad, then 0.6 mm)
    h.t(n(21), F, R([(-2.45, 1.5), (-3.0, 1.5), (-3.0, 1.9)]), NK)                     # OA2 -> J pin 3
    h.t(n(21), F, R([(-3.0, 1.9), (-3.0, 7.5), (-1.5, 9.0), (-1.5, 10.0)]), 0.6)
    h.t(n(1), F, R([(2.45, 1.5), (3.0, 1.5), (3.0, 1.9)]), NK)                         # OB2 -> J pin 6
    h.t(n(1), F, R([(3.0, 1.9), (3.0, 7.5), (4.5, 9.0), (4.5, 10.0)]), 0.6)
    h.t(n(26), F, R([(0.5, 2.45), (0.5, 3.6)]), NK)                                     # OB1 -> J pin 4
    h.t(n(26), F, R([(0.5, 3.6), (0.5, 5.6)]), 0.5)
    h.t(n(26), F, R([(0.5, 5.6), (0.5, 10.0)]), 0.6)
    h.t(n(24), F, R([(-0.5, 2.45), (-0.5, 3.75)]), NK)                                  # OA1 hops to B.Cu
    h.t(n(24), F, R([(-0.5, 3.75), (-0.5, 4.5)]), 0.5)
    for p in R([(-0.5, 3.75), (-0.5, 4.5)]):
        h.v(n(24), p)
    h.t(n(24), B, R([(-0.5, 3.75), (-0.5, 5.8), (-1.1, 6.4), (-4.9, 6.4), (-5.5, 7.0), (-5.5, 10.0)]), 0.6)
    # sense resistors
    h.t(n(23), F, R([(-1.0, 2.45), (-1.0, 3.3), (-1.3, 3.6)]), NK)                     # BRA
    h.t(n(23), F, R([(-1.3, 3.6), (-1.568, 4.4), (-1.568, 5.3)]), 0.5)
    h.t(n(27), F, R([(1.0, 2.45), (1.0, 3.3), (1.3, 3.6)]), NK)                        # BRB
    h.t(n(27), F, R([(1.3, 3.6), (1.568, 4.4), (1.568, 5.3)]), 0.5)
    for rr, s in ((Rbra, -1), (Rbrb, 1)):                                               # sense GND: 2 vias each
        g = h.P(f'{rr}.1')
        h.t('GND', F, [g, (cx + s * 5.75, g[1])], 0.6)
        for dv in (-0.4, 0.4):
            h.v('GND', (cx + s * 5.75, g[1] + dv))
    # VS pins: one via each into the In2 +12V island
    for s, pin in ((-1, 22), (1, 28)):
        h.t('+12V', F, R([(s * 1.5, 2.45), (s * 1.5, 2.75), (s * 2.15, 3.15)]), 0.3)
        h.v('+12V', R([(s * 2.15, 3.15)])[0])
    # GND pins into the exposed pad
    for pin, a, b in ((18, (-2.45, 0), (-1.6, 0)), (3, (2.45, 0.5), (1.6, 0.5)), (7, (2.45, -1.5), (1.6, -1.5)),
                      (13, (-1.0, -2.45), (-1.0, -1.6)), (10, (0.5, -2.45), (0.5, -1.6)), (9, (1.0, -2.45), (1.0, -1.6))):
        if n(pin) == 'GND':
            h.t('GND', F, R([a, b]), NK)
    # VCC_IO (15) -> Cio (above the HTSSOP envelope), VREF (17) via B.Cu to the Cio via (+3V3 band of In2)
    h.t('+3.3V', F, R([(-2.45, -1.5), (-3.15, -1.5), (-3.72, -2.07), (-3.72, -2.6)]), NK)
    h.t('+3.3V', F, R([(-3.72, -2.6), (-3.72, -4.62)]), 0.4)
    h.v('+3.3V', R([(-3.72, -3.0)])[0])
    h.t('+3.3V', F, R([(-2.45, -0.5), (-3.3, -0.5)]), NK)
    h.v('+3.3V', R([(-3.3, -0.5)])[0], VIA_T)
    h.t('+3.3V', B, R([(-3.3, -0.5), (-3.3, -2.58), (-3.72, -3.0)]), 0.3)
    if ms1_3v3:   # U8 pin 9 (MS1) = +3V3
        h.t('+3.3V', F, R([(1.0, -2.45), (1.0, -3.2), (0.85, -3.5)]), NK)
        h.v('+3.3V', R([(0.85, -3.5)])[0], VIA_T)
    # 5VOUT, charge pump, VCP (caps sit just outside the HTSSOP envelope)
    h.t(n(8), F, R([(1.5, -2.45), (1.5, -3.1), (1.825, -3.425), (1.825, -5.0)]), NK)
    h.t(n(6), F, R([(2.45, -1.0), (2.95, -1.0), (3.45, -1.5), (5.42, -1.5), (5.92, -2.0)]), 0.2)
    h.t(n(5), F, R([(2.45, -0.5), (3.2, -0.5), (3.43, -0.73), (6.0, -0.73)]), 0.2)
    h.t(n(4), F, R([(2.45, 0.0), (3.2, 0.0), (3.43, 0.23), (6.0, 0.23)]), 0.2)
    h.t('+12V', F, R([(6.88, -2.0), (6.88, 0.3)]), 0.4)                                 # VCP cap, VS side
    h.v('+12V', R([(6.88, 0.3)])[0])                                                   # (below the +3V3 band)
    # ENN: straight to a via, routed on from B.Cu
    h.t(n(2), F, R([(2.45, 1.0), (3.3, 1.0)]), 0.2)
    h.v(n(2), R([(3.3, 1.0)])[0], VIA_T)


# ------------------------------------------------------------------ AP62300 bucks
def buck(h, cx, cy, U, L, Cin, Cbst, Rtop, Rbot, Co1, Co2):
    R = rel(cx, cy)
    sw, vin, fb, bst = h.net(f'{U}.2'), h.net(f'{U}.3'), h.net(f'{U}.4'), h.net(f'{U}.6')
    vout = h.net(f'{L}.2')
    h.t(sw, F, R([(-1.138, 0), (-1.9, 0)]), 0.6)
    h.t(sw, F, R([(-1.9, 0), (-3.3, 0)]), 0.8)
    h.t(sw, F, R([(-2.25, 0), (-2.25, -1.85), (-1.9, -2.2), (-0.48, -2.2)]), 0.4)        # SW -> BST cap, north of GND pin
    h.t(bst, F, R([(1.138, -0.95), (1.138, -1.5), (0.6, -2.04), (0.48, -2.2)]), 0.3)
    h.t('GND', F, R([(-1.138, -0.95), (0.0, -0.95)]), 0.4)                               # GND pin: via under the body
    h.v('GND', R([(0.0, -0.95)])[0], VIA_T)
    h.t(vin, F, R([(-1.138, 0.95), (-1.138, 2.6)]), 0.6)                                 # VIN -> Cin
    h.t(fb, F, R([(1.138, 0.95), (2.6, 0.95), (2.86, 0.69), (4.5, 0.69)]), 0.2)
    h.t(vout, F, R([(-9.7, 0), (-9.7, 4.85), (-7.5, 4.85)]), 0.8)                       # L -> output caps
    cg = h.P(f'{Cin}.1')                                                                 # Cin GND: two vias
    for dv in (-0.45, 0.45):
        h.v('GND', (cg[0] + 0.95, cg[1] + dv))
    h.t('GND', F, [cg, (cg[0] + 0.95, cg[1])], 0.8)
    h.t('GND', F, [(cg[0] + 0.95, cg[1] - 0.45), (cg[0] + 0.95, cg[1] + 0.45)], 0.6)   # tie both vias


# ------------------------------------------------------------------ 12 V input and feeds
def power(h):
    raw = h.net('P1.2')
    h.t(raw, F, [h.P('P1.2'), (10.7, 25.04)], 1.5)                                         # P1 -> F1
    h.t(h.net('F1.1'), F, [h.P('F1.1'), h.P('Q1.3')], 1.0)                                # F1 -> Q1 drain
    q = h.P('Q1.2')                                                                        # Q1 source: 3 vias to In2
    h.t('+12V', F, [q, (q[0], 24.6)], 0.6)
    h.t('+12V', F, [(21.6, 24.6), (23.3, 24.6)], 0.6)
    for u in (21.6, 22.44, 23.3):
        h.v('+12V', (u, 24.6), VIA_P)
    # Both bucks (top-left power strip) take +12V from their In2 corridors: each Cin gets a fanout via there.


# ------------------------------------------------------------------ MCP23017 -> button connectors
def buttons(h):
    """Staircase bus on F.Cu: the leftmost destination runs on top, 0.45 mm pitch, steps down under C15/C16/C41/C42."""
    n = lambda pin: h.net(f'U1.{pin}')
    W = 0.2
    h.t(n(19), F, [(67.662, 37.15), (66.5, 37.15), (66.5, 39.75), (66.1, 40.15), (61.55, 40.15), (59.75, 41.95),
                   (41.24, 41.95), (40.84, 42.35), (40.84, 45.925)], W)                                          # SELECT
    h.t(n(25), F, [(70.5, 39.338), (70.5, 40.6), (62.0, 40.6), (60.2, 42.4), (47.52, 42.4), (47.12, 42.8),
                   (47.12, 45.925)], W)                                                                          # RETURN
    h.t(n(26), F, [(71.15, 39.338), (71.15, 41.05), (62.45, 41.05), (60.65, 42.85), (53.8, 42.85), (53.4, 43.25),
                   (53.4, 45.925)], W)                                                                           # TOP
    h.t(n(27), F, [(71.8, 39.338), (71.8, 41.5), (62.9, 41.5), (61.1, 43.3), (60.08, 43.3), (59.68, 43.7),
                   (59.68, 45.925)], W)                                                                          # BOTTOM
    h.t(n(28), F, [(72.45, 39.338), (72.45, 41.95), (66.36, 41.95), (65.96, 42.35), (65.96, 45.925)], W)        # LEFT
    h.t(n(1), F, [(73.338, 38.45), (74.3, 38.45), (74.7, 38.85), (74.7, 43.8), (74.3, 44.2), (72.64, 44.2),
                  (72.24, 44.6), (72.24, 45.925)], W)                                                            # RIGHT


def add_all(ctx):
    h = H(ctx)
    import place
    driver(h, 14.6, place.DRV_Y, 'U7', 'J1', 'R25', 'R26', 'C27', 'C3', 'C26', 'C28', False)
    driver(h, 30.0, place.DRV_Y, 'U8', 'J2', 'R27', 'R28', 'C31', 'C4', 'C29', 'C30', True)
    import place
    buck(h, *place.U6_AT, 'U6', 'L2', 'C35', 'C36', 'R29', 'R30', 'C37', 'C38')
    buck(h, *place.U9_AT, 'U9', 'L3', 'C39', 'C40', 'R33', 'R34', 'C41', 'C42')
    power(h)
    buttons(h)
