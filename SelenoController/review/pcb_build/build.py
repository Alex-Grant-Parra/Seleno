"""Build the SELENO v0.8 board (footprints, nets, outline, holes, test pads) from the netlist + place.py.

Run with KiCad's python:  python.exe build.py [out.kicad_pcb]
Also writes geom.json (pads, courtyards) for the plotter / router.
"""
import os, sys, json, importlib
import pcbnew
from common import *          # noqa
import place
importlib.reload(place)

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(WORK, 'SELENO.kicad_pcb')
mm = pcbnew.FromMM
V = lambda u, v: pcbnew.VECTOR2I(mm(OX + u), mm(OY + v))


def new_board(path):
    if os.path.exists(path):
        os.remove(path)
    b = pcbnew.NewBoard(path)
    b.SetCopperLayerCount(4)
    ds = b.GetDesignSettings()
    ds.SetBoardThickness(mm(1.6))
    b.SetLayerType(pcbnew.In1_Cu, pcbnew.LT_POWER)
    b.SetLayerType(pcbnew.In2_Cu, pcbnew.LT_POWER)
    b.SetLayerName(pcbnew.In1_Cu, 'In1.Cu')
    b.SetLayerName(pcbnew.In2_Cu, 'In2.Cu')
    # manufacturing limits (JLCPCB 4-layer, with margin)
    ds.m_MinClearance = mm(0.15)
    ds.m_TrackMinWidth = mm(0.15)
    ds.m_ViasMinSize = mm(0.45)
    ds.m_ViasMinAnnularWidth = mm(0.075)
    ds.m_MinThroughDrill = mm(0.3)          # every hole >= 0.3 mm (JLC: 0.2 mm holes cost extra)
    ds.m_HoleToHoleMin = mm(0.25)
    ds.m_HoleClearance = mm(0.2)
    ds.m_CopperEdgeClearance = mm(0.3)
    ds.m_SilkClearance = mm(0.0)
    ds.m_MinSilkTextHeight = mm(0.8)
    ds.m_MinSilkTextThickness = mm(0.12)
    return b


def set_field(fp, name, value):
    if fp.HasField(name):
        fp.GetField(name).SetText(value)
    else:
        f = pcbnew.PCB_FIELD(fp, fp.GetNextFieldOrdinal(), name)
        f.SetText(value)
        f.SetVisible(False)
        f.SetLayer(pcbnew.F_Fab)
        fp.Add(f)


def load_fp(fpid):
    lib, name = fpid.split(':', 1)
    fp = pcbnew.FootprintLoad(lib_path(lib), name)
    if fp is None:
        raise RuntimeError('cannot load ' + fpid)
    fp.SetFPID(pcbnew.LIB_ID(lib, name))
    return fp


def place_fp(b, fp, u, v, r):
    b.Add(fp)
    fp.SetOrientationDegrees(r)
    fp.SetPosition(V(u, v))


def build(out=OUT):
    comps, pinnet, netnames = load_netlist()
    b = new_board(out)
    nets = {}
    for n in netnames:
        ni = pcbnew.NETINFO_ITEM(b, n)
        b.Add(ni)
        nets[n] = ni

    missing = [r for r in comps if r not in place.P]
    extra = [r for r in place.P if r not in comps]
    if missing or extra:
        print('PLACEMENT MISMATCH missing', missing, 'extra', extra)

    for ref, c in comps.items():
        fp = load_fp(c['footprint'])
        fp.SetReference(ref)
        fp.SetValue(c['value'])
        set_field(fp, 'Footprint', c['footprint'])
        if c['datasheet']:
            set_field(fp, 'Datasheet', c['datasheet'])
        if c['description']:
            set_field(fp, 'Description', c['description'])
        for k, val_ in c['fields'].items():
            if k in ('Footprint', 'Datasheet', 'Description'):
                continue                       # (empty fields too, so schematic parity matches)
            set_field(fp, k, val_)
        fp.SetPath(pcbnew.KIID_PATH(c['path']))
        fp.SetSheetname(c['sheetname'])
        fp.SetSheetfile(c['sheetfile'])
        attrs = fp.GetAttributes()
        if not c['in_bom']:
            attrs |= pcbnew.FP_EXCLUDE_FROM_BOM
        fp.SetAttributes(attrs)
        for pad in fp.Pads():
            n = pinnet.get((ref, pad.GetNumber()))
            if n:
                pad.SetNet(nets[n])
            # stock U5/U7/U8 thermal vias are 0.2 mm: open them to 0.3 (copper size unchanged)
            if pad.GetAttribute() == pcbnew.PAD_ATTRIB_PTH and 0 < pad.GetDrillSize().x < mm(0.3):
                pad.SetDrillSize(pcbnew.VECTOR2I(mm(0.3), mm(0.3)))
        u, v, r = place.P.get(ref, (BW / 2, BH + 10, 0))
        place_fp(b, fp, u, v, r)

    # mounting holes and test pads: board-only parts (not in the schematic, BOM or pick-and-place)
    for ref, u, v in place.HOLES:
        fp = load_fp('MountingHole:MountingHole_2.7mm_M2.5')
        fp.SetReference(ref)
        fp.SetValue('M2.5')
        fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
        place_fp(b, fp, u, v, 0)
    for ref, net, u, v, *_ in place.TESTPADS:
        fp = load_fp('TestPoint:TestPoint_Pad_D1.0mm')
        fp.SetReference(ref)
        fp.SetValue(net.lstrip('/'))
        fp.SetAttributes(fp.GetAttributes() | pcbnew.FP_BOARD_ONLY | pcbnew.FP_EXCLUDE_FROM_BOM | pcbnew.FP_EXCLUDE_FROM_POS_FILES)
        for pad in fp.Pads():
            pad.SetNet(nets[net])
        place_fp(b, fp, u, v, 0)

    # outline
    s = pcbnew.PCB_SHAPE(b)
    s.SetShape(pcbnew.SHAPE_T_RECT)
    s.SetStart(V(0, 0))
    s.SetEnd(V(BW, BH))
    s.SetLayer(pcbnew.Edge_Cuts)
    s.SetWidth(mm(0.1))
    b.Add(s)

    tb = b.GetTitleBlock()
    tb.SetTitle('SELENO controller')
    tb.SetRevision('v0.8')
    tb.SetDate('2026-10-13')
    tb.SetComment(0, '4 layers: F.Cu signals / In1 GND / In2 power / B.Cu signals, 1.6 mm')
    b.SetTitleBlock(tb)

    check_envelopes(b)
    pcbnew.SaveBoard(out, b)
    dump_geom(b, os.path.join(os.path.dirname(out), 'geom.json'))
    return b


def check_envelopes(b):
    """Warn if any other part's courtyard intrudes on a keep-clear envelope (place.ENVELOPES)."""
    bad = 0
    for owner, (u0, v0, u1, v1, what) in place.ENVELOPES.items():
        for fp in b.GetFootprints():
            if fp.GetReference() == owner:
                continue
            fp.BuildCourtyardCaches()
            cy = fp.GetCourtyard(pcbnew.F_CrtYd)
            bb = cy.BBox() if cy.OutlineCount() else fp.GetBoundingBox(False)
            L, T = pcbnew.ToMM(bb.GetLeft()) - OX, pcbnew.ToMM(bb.GetTop()) - OY
            R_, B_ = pcbnew.ToMM(bb.GetRight()) - OX, pcbnew.ToMM(bb.GetBottom()) - OY
            if L < u1 and R_ > u0 and T < v1 and B_ > v0:
                print('ENVELOPE %s (%s): %s intrudes' % (owner, what, fp.GetReference()))
                bad += 1
    print('envelopes: %d checked, %d intrusions' % (len(place.ENVELOPES), bad))


def poly_points(sps):
    out = []
    for i in range(sps.OutlineCount()):
        ol = sps.Outline(i)
        out.append([(pcbnew.ToMM(ol.CPoint(k).x) - OX, pcbnew.ToMM(ol.CPoint(k).y) - OY) for k in range(ol.PointCount())])
    return out


def dump_geom(b, path):
    fps = []
    for fp in b.GetFootprints():
        fp.BuildCourtyardCaches()
        cy = fp.GetCourtyard(pcbnew.F_CrtYd)
        pads = []
        for p in fp.Pads():
            layers = [l for l in (pcbnew.F_Cu, pcbnew.B_Cu) if p.IsOnLayer(l)]
            lay = p.GetLayer() if not layers else layers[0]
            poly = p.GetEffectivePolygon(layers[0] if layers else pcbnew.F_Cu, pcbnew.ERROR_INSIDE)
            pads.append(dict(num=p.GetNumber(), net=p.GetNetname(),
                             pos=(pcbnew.ToMM(p.GetPosition().x) - OX, pcbnew.ToMM(p.GetPosition().y) - OY),
                             layers=['F' if pcbnew.F_Cu in layers else '', 'B' if pcbnew.B_Cu in layers else ''],
                             tht=p.GetAttribute() in (pcbnew.PAD_ATTRIB_PTH, pcbnew.PAD_ATTRIB_NPTH),
                             npth=p.GetAttribute() == pcbnew.PAD_ATTRIB_NPTH,
                             drill=pcbnew.ToMM(p.GetDrillSize().x),
                             poly=poly_points(poly)))
        fps.append(dict(ref=fp.GetReference(), value=fp.GetValue(),
                        pos=(pcbnew.ToMM(fp.GetPosition().x) - OX, pcbnew.ToMM(fp.GetPosition().y) - OY),
                        rot=fp.GetOrientationDegrees(), crt=poly_points(cy), pads=pads))
    json.dump(dict(board=(BW, BH), footprints=fps), open(path, 'w'), indent=0)


if __name__ == '__main__':
    build()
    print('built', OUT)
