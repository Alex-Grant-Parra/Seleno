"""Add routes.json (tracks, vias, zones) to the built board, fill zones and save.

Run with KiCad's python:  python.exe apply.py routes.json [board.kicad_pcb]
"""
import os, sys, json
import pcbnew
from common import OX, OY, WORK
import rules

mm = pcbnew.FromMM
V = lambda p: pcbnew.VECTOR2I(mm(OX + p[0]), mm(OY + p[1]))
LAYERS = {'F.Cu': pcbnew.F_Cu, 'B.Cu': pcbnew.B_Cu, 'In1.Cu': pcbnew.In1_Cu, 'In2.Cu': pcbnew.In2_Cu}


def apply(routes, path, fill=True):
    b = pcbnew.LoadBoard(path)

    def net(n):
        ni = b.FindNet(n)
        if ni is None:
            raise KeyError(n)
        return ni

    for t in routes.get('tracks', []):
        s = pcbnew.PCB_TRACK(b)
        s.SetStart(V(t['a'])); s.SetEnd(V(t['b']))
        s.SetWidth(mm(t['w'])); s.SetLayer(LAYERS[t['layer']]); s.SetNet(net(t['net']))
        b.Add(s)
    for v in routes.get('vias', []):
        s = pcbnew.PCB_VIA(b)
        s.SetPosition(V(v['p'])); s.SetWidth(mm(v['d'])); s.SetDrill(mm(v['drill']))
        s.SetViaType(pcbnew.VIATYPE_THROUGH); s.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        s.SetNet(net(v['net']))
        b.Add(s)
    zones = []
    for z in routes.get('zones', []):
        zo = pcbnew.ZONE(b)
        zo.SetLayer(LAYERS[z['layer']])
        zo.SetNet(net(z['net']))
        ol = zo.Outline()
        ol.NewOutline()
        for p in z['pts']:
            q = V(p)
            ol.Append(q.x, q.y)
        zo.SetLocalClearance(mm(z.get('clearance', 0.2)))
        zo.SetMinThickness(mm(z.get('min_width', 0.2)))
        zo.SetPadConnection({'solid': pcbnew.ZONE_CONNECTION_FULL, 'tht': pcbnew.ZONE_CONNECTION_THT_THERMAL}
                            .get(z.get('conn'), pcbnew.ZONE_CONNECTION_THERMAL))
        zo.SetThermalReliefGap(mm(z.get('th_gap', 0.25)))
        zo.SetThermalReliefSpokeWidth(mm(z.get('th_spoke', 0.4)))
        zo.SetAssignedPriority(z.get('priority', 0))
        zo.SetIslandRemovalMode(pcbnew.ISLAND_REMOVAL_MODE_ALWAYS)
        if z.get('name'):
            zo.SetZoneName(z['name'])
        b.Add(zo)
        zones.append(zo)
    for k in routes.get('keepouts', []):
        zo = pcbnew.ZONE(b)
        zo.SetIsRuleArea(True)
        zo.SetLayer(LAYERS[k['layer']])
        ol = zo.Outline()
        ol.NewOutline()
        for p in k['pts']:
            q = V(p)
            ol.Append(q.x, q.y)
        zo.SetDoNotAllowTracks(True); zo.SetDoNotAllowVias(True)
        zo.SetDoNotAllowPads(False); zo.SetDoNotAllowFootprints(False); zo.SetDoNotAllowZoneFills(False)
        zo.SetZoneName(k['name'])
        b.Add(zo)
    if fill and zones:
        filler = pcbnew.ZONE_FILLER(b)
        filler.Fill(b.Zones())
    pcbnew.SaveBoard(path, b)
    return b


if __name__ == '__main__':
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    routes = json.load(open(args[0]))
    path = args[1] if len(args) > 1 else os.path.join(WORK, 'ASTRA.kicad_pcb')
    rules.write_project(os.path.join(os.path.dirname(path), 'ASTRA.kicad_pro'))
    apply(routes, path, fill='--nofill' not in sys.argv)
    print('applied', len(routes.get('tracks', [])), 'tracks', len(routes.get('vias', [])), 'vias', len(routes.get('zones', [])), 'zones')
