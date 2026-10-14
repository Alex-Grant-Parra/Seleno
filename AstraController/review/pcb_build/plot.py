"""Render geom.json (+ optional routes.json) to a PNG for placement / routing review.

python.exe plot.py geom.json out.png [routes.json|-] [opts] [u0,v0,u1,v1] [px_per_mm]
opts: r = ratsnest (signals), p = include power nets in ratsnest, z = zones, l = pad net labels,
      F / B = show only that copper layer's tracks
"""
import json, sys, math, os
from PIL import Image, ImageDraw, ImageFont

geom_path, out = sys.argv[1], sys.argv[2]
rp = sys.argv[3] if len(sys.argv) > 3 else '-'
routes = json.load(open(rp)) if rp not in ('-', '') and os.path.exists(rp) else None
opts = sys.argv[4] if len(sys.argv) > 4 else ''
G = json.load(open(geom_path))
BW, BH = G['board']
crop = tuple(map(float, sys.argv[5].split(','))) if len(sys.argv) > 5 and sys.argv[5] not in ('-', '') else (0, 0, BW, BH)
S = float(sys.argv[6]) if len(sys.argv) > 6 else (16.0 if crop == (0, 0, BW, BH) else 60.0)
M = 20
u0, v0, u1, v1 = crop
img = Image.new('RGB', (int((u1 - u0) * S + 2 * M), int((v1 - v0) * S + 2 * M)), (20, 22, 26))
d = ImageDraw.Draw(img, 'RGBA')
try:
    font = ImageFont.truetype('arial.ttf', max(9, int(S / 6)) if S > 20 else 11)
    small = ImageFont.truetype('arial.ttf', max(8, int(S / 9)))
    bigf = ImageFont.truetype('arialbd.ttf', 13 if S <= 20 else max(13, int(S / 4)))
except Exception:
    font = small = bigf = ImageFont.load_default()
X = lambda u: M + (u - u0) * S
Y = lambda v: M + (v - v0) * S
PWR = {'+3.3V': (230, 120, 40), '+5V': (230, 60, 160), '+12V': (240, 50, 50), 'GND': (60, 170, 80)}

d.rectangle([X(0), Y(0), X(BW), Y(BH)], outline=(230, 230, 120), width=2, fill=(28, 34, 30))


def pcolor(net):
    if net in PWR:
        return PWR[net]
    if not net or net.startswith('unconnected'):
        return (110, 110, 110)
    return (90, 140, 230)


if routes and 'z' in opts:
    for z in routes.get('zones', []):
        if z['layer'] in ('F.Cu', 'B.Cu') and z['net'] == 'GND':
            continue
        col = (*PWR.get(z['net'], (150, 150, 150)), 45)
        d.polygon([(X(u), Y(v)) for u, v in z['pts']], fill=col, outline=(*col[:3], 200))

for fp in G['footprints']:
    for poly in fp['crt']:
        pts = [(X(u), Y(v)) for u, v in poly]
        if len(pts) > 2:
            d.polygon(pts, outline=(200, 200, 200, 110))
for fp in G['footprints']:
    for p in fp['pads']:
        for poly in p['poly']:
            pts = [(X(u), Y(v)) for u, v in poly]
            if len(pts) > 2:
                c = pcolor(p['net'])
                d.polygon(pts, fill=(*c, 170 if p['tht'] else 210))
        if p['tht']:
            r = p['drill'] / 2 * S
            u, v = p['pos']
            d.ellipse([X(u) - r, Y(v) - r, X(u) + r, Y(v) + r], fill=(10, 10, 10))

if routes:
    LC = {'F.Cu': (220, 60, 60, 200), 'B.Cu': (60, 130, 250, 150)}
    show = [l for l in ('B.Cu', 'F.Cu') if not ('F' in opts or 'B' in opts) or l[0] in opts]
    for layer in show:
        for t in routes.get('tracks', []):
            if t['layer'] != layer:
                continue
            (a1, b1), (a2, b2) = t['a'], t['b']
            w = max(1, int(round(t['w'] * S)))
            d.line([X(a1), Y(b1), X(a2), Y(b2)], fill=LC[layer], width=w)
            r = t['w'] / 2 * S
            for a, b_ in ((a1, b1), (a2, b2)):
                d.ellipse([X(a) - r, Y(b_) - r, X(a) + r, Y(b_) + r], fill=LC[layer])
    for via in routes.get('vias', []):
        a, b_ = via['p']
        r = via['d'] / 2 * S
        c = PWR.get(via['net'], (200, 200, 200))
        d.ellipse([X(a) - r, Y(b_) - r, X(a) + r, Y(b_) + r], fill=c, outline=(240, 240, 240))
        r2 = via['drill'] / 2 * S
        d.ellipse([X(a) - r2, Y(b_) - r2, X(a) + r2, Y(b_) + r2], fill=(30, 30, 30))

if 'r' in opts:
    nets = {}
    for fp in G['footprints']:
        for p in fp['pads']:
            if p['net'] and not p['net'].startswith('unconnected'):
                nets.setdefault(p['net'], []).append(tuple(p['pos']))
    for net, pts in nets.items():
        if net in PWR and 'p' not in opts:
            continue
        pts = list(dict.fromkeys(pts))
        inside = {0}
        while len(pts) > 1 and len(inside) < len(pts):
            best = min(((math.dist(pts[i], pts[j]), i, j) for i in inside for j in range(len(pts)) if j not in inside))
            _, i, j = best
            inside.add(j)
            c = (255, 255, 0, 150) if net not in PWR else (*PWR[net], 120)
            d.line([X(pts[i][0]), Y(pts[i][1]), X(pts[j][0]), Y(pts[j][1])], fill=c, width=1)

if 'l' in opts:
    for fp in G['footprints']:
        for p in fp['pads']:
            u, v = p['pos']
            if not (u0 - 1 < u < u1 + 1 and v0 - 1 < v < v1 + 1):
                continue
            n = p['net'].split('/')[-1].replace('Net-(', '').replace(')', '')
            if n.startswith('unconnected'):
                n = 'nc'
            txt = f"{p['num']}:{n[:10]}"
            tw = d.textlength(txt, font=small)
            d.text((X(u) - tw / 2, Y(v) - 5), txt, fill=(255, 255, 255), font=small, stroke_width=1, stroke_fill=(0, 0, 0))

for fp in G['footprints']:
    u, v = fp['pos']
    ref = fp['ref']
    f = bigf if ref[0] in 'UJPSH' or ref.startswith(('SW', 'TP')) else font
    tw = d.textlength(ref, font=f)
    yy = Y(v) - 6 if 'l' not in opts else Y(v) + 4
    d.text((X(u) - tw / 2, yy), ref, fill=(255, 255, 160), font=f, stroke_width=2, stroke_fill=(0, 0, 0))

step = 5 if S <= 20 else 1
for k in range(0, int(BW) + 1, step):
    if u0 <= k <= u1:
        d.line([X(k), Y(v0) - 6, X(k), Y(v0)], fill=(230, 230, 120))
        d.text((X(k) - 5, 2), str(k), fill=(200, 200, 200), font=font)
for k in range(0, int(BH) + 1, step):
    if v0 <= k <= v1:
        d.line([X(u0) - 6, Y(k), X(u0), Y(k)], fill=(230, 230, 120))
        d.text((1, Y(k) - 6), str(k), fill=(200, 200, 200), font=font)
img.save(out)
print('saved', out)
