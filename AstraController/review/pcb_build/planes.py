"""Copper zones for SELENO v0.7 (board-local mm).

In1.Cu: solid GND.
In2.Cu: +3.3V everywhere (priority 0) with +12V and +5V islands on top (priority 1).
F.Cu / B.Cu: no pours (signals only).
"""
BW, BH = 85.0, 50.0
FULL = [(0, 0), (BW, 0), (BW, BH), (0, BH)]

# +12V: input (P1/F1/Q1/D6) + left column (D7) + both driver blocks and C1.
# The +3.3V band between v 33.0 and 36.5 feeds the drivers' VCC_IO side.
ISL_12V = [(0, 22.4), (24.2, 22.4), (24.2, 33.0), (9.4, 33.0), (9.4, 36.5), (44.6, 36.5), (44.6, BH), (0, BH)]
# +5V: USB connector / ESD / PTC area, plus a corridor down to the 5 V buck output (L2, C37, C38).
ISL_5V = [(0, 0), (31.0, 0), (31.0, 22.0), (39.6, 22.0), (39.6, 33.1), (24.7, 33.1), (24.7, 22.0), (0, 22.0)]

ISLANDS = {'+12V': ISL_12V, '+5V': ISL_5V}   # anything else on In2 is +3.3V

# rule areas: no F.Cu tracks and no vias under the ESP32 module body (its underside is a bare GND pad)
KEEPOUTS = [dict(name='ESP32 underside', layer='F.Cu', pts=[(59.95, 8.95), (77.9, 8.95), (77.9, 24.65), (59.95, 24.65)])]


def island_of(net):
    return ISLANDS.get(net, FULL if net == '+3.3V' else None)


def zones():
    z = [
        dict(net='GND', layer='In1.Cu', pts=FULL, priority=0, conn='tht', name='GND plane'),
        dict(net='+3.3V', layer='In2.Cu', pts=FULL, priority=0, name='+3V3 plane'),
        dict(net='+12V', layer='In2.Cu', pts=ISL_12V, priority=1, name='+12V island'),
        dict(net='+5V', layer='In2.Cu', pts=ISL_5V, priority=1, name='+5V island'),
        # no pours on F.Cu / B.Cu (Alex, 2026-10-14): every GND pad has its own via to In1
    ]
    return z
