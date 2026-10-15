import pcbnew, sys, re
sys.path.insert(0, r'C:\Users\alexg\AppData\Local\Temp\claude\z--ASTRA\a7d97d99-9fe0-411d-bfa9-89de7e274382\scratchpad')
from parse_net import parse, find, val
FP = r'C:\Users\alexg\AppData\Local\Programs\KiCad\10.0\share\kicad\footprints'
PRJ = r'Z:\ASTRA\SelenoController'
def libpath(lib):
    import os
    return os.path.join(PRJ, 'SELENOfootprints.pretty') if lib == 'SELENOfootprints' else os.path.join(FP, lib + '.pretty')
t = parse(open(r'C:\Users\alexg\AppData\Local\Temp\claude\z--ASTRA\a7d97d99-9fe0-411d-bfa9-89de7e274382\scratchpad\final\seleno.net', encoding='utf-8').read())
fps = {}
for c in find(find(t,'components')[0],'comp'):
    fps.setdefault(val(c,'footprint'), []).append(val(c,'ref'))
extra = ['MountingHole:MountingHole_2.7mm_M2.5', 'TestPoint:TestPoint_Pad_D1.5mm', 'TestPoint:TestPoint_Pad_D1.0mm']
for e in extra: fps.setdefault(e, ['-'])
mm = pcbnew.ToMM
for fpid, refs in sorted(fps.items()):
    lib, name = fpid.split(':')
    f = pcbnew.FootprintLoad(libpath(lib), name)
    cy = f.GetCourtyard(pcbnew.F_CrtYd)
    bb = cy.BBox() if cy.OutlineCount() else f.GetBoundingBox(False)
    pads = [(p.GetNumber(), round(mm(p.GetPosition().x),2), round(mm(p.GetPosition().y),2)) for p in f.Pads()]
    print('%-62s crt x[%6.2f,%6.2f] y[%6.2f,%6.2f]  %s' % (fpid[:62], mm(bb.GetLeft()), mm(bb.GetRight()), mm(bb.GetTop()), mm(bb.GetBottom()), ','.join(refs)[:40]))
    if len(pads) <= 12 and not fpid.startswith(('Resistor', 'Capacitor_SMD', 'LED')):
        print('      pads', pads)
