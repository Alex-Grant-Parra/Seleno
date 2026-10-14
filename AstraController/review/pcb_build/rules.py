"""Net classes and design rules for SELENO v0.7 (also written into ASTRA.kicad_pro)."""
import json, fnmatch

# name: track, clearance, via diameter, via drill
CLASSES = {
    'Default': dict(track=0.2, clr=0.15, via=0.5, drill=0.3),
    'Power':   dict(track=0.4, clr=0.15, via=0.5, drill=0.3),
    'HV12':    dict(track=1.0, clr=0.2, via=0.6, drill=0.3),
    'Motor':   dict(track=0.6, clr=0.2, via=0.6, drill=0.3),
    'BuckSW':  dict(track=0.8, clr=0.2, via=0.6, drill=0.3),
    'USB':     dict(track=0.25, clr=0.15, via=0.5, drill=0.3, dp_gap=0.15),
}
PATTERNS = [   # first match wins
    ('+3.3V', 'Power'), ('+5V', 'Power'), ('GND', 'Power'), ('/USB & Serial/VBUS_CAM', 'Power'),
    ('+12V', 'HV12'), ('Net-(F1-Pad2)', 'HV12'), ('Net-(Q1-D)', 'HV12'),
    ('Net-(J1-Pin_*)', 'Motor'), ('Net-(J2-Pin_*)', 'Motor'),
    ('Net-(U7-BR*)', 'Motor'), ('Net-(U8-BR*)', 'Motor'),
    ('Net-(U6-SW)', 'BuckSW'), ('Net-(U9-SW)', 'BuckSW'),
    ('/USB & Serial/upstream D*', 'USB'), ('/USB & Serial/downstream* D*', 'USB'),
]
TIGHT_VIA = dict(via=0.45, drill=0.2)
PLANE_NETS = ('GND', '+3.3V', '+5V', '+12V')

# board-level minimums (JLCPCB 4-layer capable, with margin)
MIN = dict(clearance=0.15, track=0.15, via=0.45, drill=0.2, annular=0.1, edge=0.3, hole_clr=0.2, hole_hole=0.25)


def netclass(net):
    for pat, c in PATTERNS:
        if fnmatch.fnmatchcase(net, pat):
            return c
    return 'Default'


def width(net):
    return CLASSES[netclass(net)]['track']


def clearance(net):
    return CLASSES[netclass(net)]['clr']


def write_project(path):
    d = json.load(open(path, encoding='utf-8'))
    ns = d['net_settings']
    default = next(c for c in ns['classes'] if c['name'] == 'Default')
    out = []
    for i, (name, c) in enumerate(CLASSES.items()):
        k = dict(default)
        k.update(name=name, track_width=c['track'], clearance=c['clr'], via_diameter=c['via'], via_drill=c['drill'],
                 diff_pair_width=0.25, diff_pair_gap=c.get('dp_gap', 0.25), diff_pair_via_gap=0.25,
                 priority=2147483647 if name == 'Default' else i)
        out.append(k)
    ns['classes'] = out
    ns['netclass_patterns'] = [dict(netclass=c, pattern=p) for p, c in PATTERNS]
    r = d['board']['design_settings']['rules']
    r.update(min_clearance=MIN['clearance'], min_track_width=MIN['track'], min_via_diameter=MIN['via'],
             min_through_hole_diameter=MIN['drill'], min_via_annular_width=MIN['annular'],
             min_copper_edge_clearance=MIN['edge'], min_hole_clearance=MIN['hole_clr'], min_hole_to_hole=MIN['hole_hole'])
    ds = d['board']['design_settings']
    ds['track_widths'] = [0.0, 0.2, 0.25, 0.4, 0.6, 0.8, 1.0]
    ds['via_dimensions'] = [dict(diameter=0.0, drill=0.0), dict(diameter=0.45, drill=0.2), dict(diameter=0.5, drill=0.3), dict(diameter=0.6, drill=0.3)]
    ds['diff_pair_dimensions'] = [dict(gap=0.0, via_gap=0.0, width=0.0), dict(gap=0.15, via_gap=0.25, width=0.25)]
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(d, f, indent=2)
        f.write('\n')
