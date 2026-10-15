"""Final touches on the routed board: silkscreen labels, title text, and the JLC04161H-7628 stackup.

Run with KiCad's python:  python.exe finish.py [board.kicad_pcb]
"""
import os, sys, re
import pcbnew
from common import OX, OY, WORK

mm = pcbnew.FromMM
V = lambda u, v: pcbnew.VECTOR2I(mm(OX + u), mm(OY + v))

# JLCPCB JLC04161H-7628, 1.6 mm (outer 1 oz, inner 0.5 oz)
STACKUP = '''(stackup
			(layer "F.SilkS" (type "Top Silk Screen"))
			(layer "F.Paste" (type "Top Solder Paste"))
			(layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
			(layer "F.Cu" (type "copper") (thickness 0.035))
			(layer "dielectric 1" (type "prepreg") (color "FR4 natural") (thickness 0.2104) (material "7628") (epsilon_r 4.4) (loss_tangent 0.02))
			(layer "In1.Cu" (type "copper") (thickness 0.0152))
			(layer "dielectric 2" (type "core") (color "FR4 natural") (thickness 1.065) (material "FR4") (epsilon_r 4.6) (loss_tangent 0.02))
			(layer "In2.Cu" (type "copper") (thickness 0.0152))
			(layer "dielectric 3" (type "prepreg") (color "FR4 natural") (thickness 0.2104) (material "7628") (epsilon_r 4.4) (loss_tangent 0.02))
			(layer "B.Cu" (type "copper") (thickness 0.035))
			(layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
			(layer "B.Paste" (type "Bottom Solder Paste"))
			(layer "B.SilkS" (type "Bottom Silk Screen"))
			(copper_finish "HAL lead-free")
			(dielectric_constraints no)
		)'''

# (text, u, v, size mm, rotation)  -- connector / test-pad labels on F.SilkS (board-local mm)
LABELS = [
    # power input and motors
    ('12V+', 4.45, 21.9, 0.8, 0),
    ('M1', 5.6, 42.0, 1.0, 0), ('M2', 22.3, 43.4, 1.0, 0),
    # top-edge connectors
    ('PI: EVEN ROW, PIN 2 ... PIN 20', 19.83, 4.55, 0.8, 0),
    ('IMU', 43.24, 6.05, 0.8, 0), ('LCD', 53.35, 6.05, 0.8, 0), ('RST', 79.0, 3.0, 0.8, 90),
    # buttons (labels just above the connector bodies)
    ('SELECT', 40.34, 44.2, 0.8, 0), ('RETURN', 46.62, 44.2, 0.8, 0), ('TOP', 52.9, 44.2, 0.8, 0),
    ('BOTTOM', 59.18, 44.2, 0.8, 0), ('LEFT', 65.46, 44.2, 0.8, 0), ('RIGHT', 71.74, 44.2, 0.8, 0),
    # ESP32 programming / debug test-pad block
    ('ESP32 PROG', 58.9, 36.7, 0.8, 0),
    # LED colours, left of the series resistors
    ('B', 78.3, 30.0, 0.8, 0), ('Y', 78.3, 32.5, 0.8, 0), ('W', 78.3, 35.0, 0.8, 0),
    ('G', 78.3, 37.5, 0.8, 0), ('R', 78.3, 40.0, 0.8, 0),
]


def tp_labels():
    """Silk label per test pad from place.TESTPADS: side l / r = left / right, u / d1 = above / below,
    d = vertical text below the pad."""
    import place
    out = []
    for ref, net, u, v, lab, side in place.TESTPADS:
        w = len(lab) * 0.8 * 0.75
        if side == 'l':
            out.append((lab, u - 0.85 - w / 2, v, 0.8, 0))
        elif side == 'r':
            out.append((lab, u + 0.85 + w / 2, v, 0.8, 0))
        elif side == 'u':
            out.append((lab, u, v - 1.25, 0.8, 0))
        elif side == 'd1':
            out.append((lab, u - (0.25 if u < 3 else 0), v + 1.2 + (0.2 if u < 3 else 0), 0.8, 0))   # (left-edge pads: clear of D7)
        else:
            out.append((lab, u, v + 0.85 + w / 2, 0.8, 90))
    return out


TITLE = ('SELENO.v0.8', 28.5, 21.2, 1.5)


def text(b, s, u, v, size=0.8, rot=0, layer=pcbnew.F_SilkS, bold=False, thick=None):
    t = pcbnew.PCB_TEXT(b)
    t.SetText(s)
    t.SetLayer(layer)
    t.SetPosition(V(u, v))
    t.SetTextSize(pcbnew.VECTOR2I(mm(size), mm(size)))
    t.SetTextThickness(mm(thick if thick else 0.15))
    t.SetTextAngleDegrees(rot)
    t.SetBold(bold)
    b.Add(t)
    return t


HIDE_REF = ('R', 'C', 'TP', 'H')      # passives, test pads and holes: refs only on F.Fab
LED_REFS = ('D1', 'D2', 'D3', 'D4', 'D5')   # LEDs: colour letters


def _bb(item):
    r = item.GetBoundingBox()
    return (pcbnew.ToMM(r.GetLeft()), pcbnew.ToMM(r.GetTop()), pcbnew.ToMM(r.GetRight()), pcbnew.ToMM(r.GetBottom()))


def _hit(a, b, m=0.1):
    return not (a[2] + m <= b[0] or b[2] + m <= a[0] or a[3] + m <= b[1] or b[3] + m <= a[1])


def silk(b):
    obstacles = []                       # pads (all footprints) and silk drawn by footprints
    for fp in b.GetFootprints():
        ref = fp.GetReference()
        if ref.startswith('TP'):         # test pads: drop the silk ring so net labels fit between pads
            for g in fp.GraphicalItems():
                if g.GetLayer() == pcbnew.F_SilkS and g.Type() == pcbnew.PCB_SHAPE_T:
                    g.SetLayer(pcbnew.F_Fab)
        for p in fp.Pads():
            obstacles.append(_bb(p))
        for g in fp.GraphicalItems():
            if g.GetLayer() == pcbnew.F_SilkS and g.Type() == pcbnew.PCB_SHAPE_T:
                obstacles.append(_bb(g))
    placed = []
    for s, u, v, size, rot in LABELS + tp_labels():
        t = text(b, s, u, v, size, rot)
        placed.append(_bb(t))
    x0, y0, x1, y1 = OX + 0.4, OY + 0.4, OX + 84.6, OY + 49.6
    for fp in sorted(b.GetFootprints(), key=lambda f: f.GetReference()):
        ref = fp.GetReference()
        rf = fp.Reference()
        kind = ''.join(ch for ch in ref if ch.isalpha())
        if kind in HIDE_REF or ref in LED_REFS:
            rf.SetVisible(False)
            continue
        rf.SetTextSize(pcbnew.VECTOR2I(mm(0.8), mm(0.8)))
        rf.SetTextThickness(mm(0.15))
        rf.SetTextAngleDegrees(0)
        fp.BuildCourtyardCaches()
        cy = fp.GetCourtyard(pcbnew.F_CrtYd)
        cb = cy.BBox() if cy.OutlineCount() else fp.GetBoundingBox(False)
        L, T, Rr, Bm = (pcbnew.ToMM(cb.GetLeft()), pcbnew.ToMM(cb.GetTop()), pcbnew.ToMM(cb.GetRight()), pcbnew.ToMM(cb.GetBottom()))
        cxm, cym = (L + Rr) / 2, (T + Bm) / 2
        w = len(ref) * 0.8 * 0.75 + 0.2
        cands = [(cxm, T - 0.55), (cxm, Bm + 0.55), (L - w / 2 - 0.15, cym), (Rr + w / 2 + 0.15, cym),
                 (cxm, cym), (L + w / 2, T - 0.55), (Rr - w / 2, T - 0.55), (L + w / 2, Bm + 0.55), (Rr - w / 2, Bm + 0.55)]
        ok = False
        for (u, v) in cands:
            rf.SetPosition(pcbnew.VECTOR2I(mm(u), mm(v)))
            bb = _bb(rf)
            if bb[0] < x0 or bb[1] < y0 or bb[2] > x1 or bb[3] > y1:
                continue
            if any(_hit(bb, o) for o in obstacles) or any(_hit(bb, o) for o in placed):
                continue
            ok = True
            placed.append(bb)
            break
        if not ok:
            rf.SetVisible(False)         # no clean spot: keep it on F.Fab only
            print('  ref hidden (no room):', ref)
    t = text(b, TITLE[0], TITLE[1], TITLE[2], TITLE[3], 0, bold=True, thick=TITLE[3] * 0.18)


def replace_stackup(path):
    s = open(path, encoding='utf-8').read()
    i = s.find('(stackup')
    if i < 0:
        j = s.find('(setup')
        s = s[:j + len('(setup')] + '\n\t\t' + STACKUP + s[j + len('(setup'):]
    else:
        depth = 0
        for k in range(i, len(s)):
            if s[k] == '(':
                depth += 1
            elif s[k] == ')':
                depth -= 1
                if depth == 0:
                    break
        s = s[:i] + STACKUP + s[k + 1:]
    open(path, 'w', encoding='utf-8', newline='\n').write(s)


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(WORK, 'SELENO.kicad_pcb')
    b = pcbnew.LoadBoard(path)
    silk(b)
    pcbnew.SaveBoard(path, b)
    replace_stackup(path)
    print('silkscreen and stackup written')
