"""Shared helpers for the SELENO v0.8 board build (run with KiCad's python.exe)."""
import os, sys, math, json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from parse_net import parse, find, val  # noqa: E402

# scratch root: holds final/seleno.net and pcbwork/ (a copy of the project). Set SELENO_SCR to override.
SCR = os.environ.get('SELENO_SCR', r'C:\Users\alexg\AppData\Local\Temp\claude\z--ASTRA\eb408975-1396-4e9d-95b0-74fe29751b13\scratchpad')

FPDIR = r'C:\Users\alexg\AppData\Local\Programs\KiCad\10.0\share\kicad\footprints'
PRJ = r'Z:\ASTRA\SelenoController'
NETLIST = os.path.join(SCR, 'final', 'seleno.net')
WORK = os.path.join(SCR, 'pcbwork')          # scratch copy of the project used for building / DRC

# board: 85 x 50 mm, same outline as the old SELENO board
OX, OY = 76.5, 45.5
BW, BH = 85.0, 50.0


def lib_path(lib):
    if lib == 'SELENOfootprints':
        return os.path.join(PRJ, 'SELENOfootprints.pretty')
    return os.path.join(FPDIR, lib + '.pretty')


def rot(x, y, deg):
    """KiCad footprint rotation (y down, positive = counter-clockwise on screen)."""
    a = math.radians(deg)
    c, s = math.cos(a), math.sin(a)
    return x * c + y * s, -x * s + y * c


def load_netlist(path=NETLIST):
    t = parse(open(path, encoding='utf-8').read())
    comps = {}
    for c in find(find(t, 'components')[0], 'comp'):
        ref = val(c, 'ref')
        fields = {}
        for f in find(find(c, 'fields')[0], 'field') if find(c, 'fields') else []:
            fields[f[1][1]] = f[2] if len(f) > 2 and isinstance(f[2], str) else ''
        props = {}
        for p in find(c, 'property'):
            name = val(p, 'name')
            props[name] = val(p, 'value') if find(p, 'value') else True
        sp = find(c, 'sheetpath')[0]
        comps[ref] = dict(
            ref=ref, value=val(c, 'value'), footprint=val(c, 'footprint'),
            description=val(c, 'description'), datasheet=val(c, 'datasheet'),
            fields=fields, props=props,
            path=val(sp, 'tstamps') + val(c, 'tstamps'),
            sheetname=props.get('Sheetname', ''), sheetfile=props.get('Sheetfile', ''),
            in_bom=not props.get('exclude_from_bom', False),
            dnp=bool(props.get('dnp', False)),
        )
    pinnet = {}
    nets = []
    for n in find(find(t, 'nets')[0], 'net'):
        name = val(n, 'name')
        nets.append(name)
        for node in find(n, 'node'):
            pinnet[(val(node, 'ref'), val(node, 'pin'))] = name
    return comps, pinnet, nets
