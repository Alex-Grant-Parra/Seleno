import pcbnew, os, sys
FP = r'C:\Users\alexg\AppData\Local\Programs\KiCad\10.0\share\kicad\footprints'
PRJ = r'Z:\ASTRA\SelenoController'
mm = pcbnew.ToMM
for fpid in sys.argv[1:]:
    lib, name = fpid.split(':')
    path = os.path.join(PRJ, 'SELENOfootprints.pretty') if lib == 'SELENOfootprints' else os.path.join(FP, lib + '.pretty')
    f = pcbnew.FootprintLoad(path, name)
    print('==', fpid)
    rows = {}
    for p in f.Pads():
        key = (p.GetNumber(), round(mm(p.GetSize(pcbnew.F_Cu).x),3), round(mm(p.GetSize(pcbnew.F_Cu).y),3), p.GetShape(pcbnew.F_Cu), p.GetAttribute(), round(mm(p.GetDrillSize().x),2))
        rows.setdefault(key, []).append((round(mm(p.GetPosition().x),3), round(mm(p.GetPosition().y),3)))
    for k, v in rows.items():
        print('  pad %-3s size %5.3fx%5.3f shape %d attr %d drill %.2f  n=%d  %s' % (k[0], k[1], k[2], k[3], k[4], k[5], len(v), v[:4] if len(v) > 1 else v))
