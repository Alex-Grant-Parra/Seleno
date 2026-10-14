"""Debug helpers: rebuild the post-fanout state and inspect one net."""
import sys, json, time
from ctx import Ctx
from field import LAYER_NAMES
import hand, route
from conn import components


def load_state(geom, fan_json=None):
    c = Ctx(geom)
    hand.add_all(c)
    route.ep_vias(c)
    if fan_json:
        d = json.load(open(fan_json))
        for t in d['tracks']:
            if t['src'] == 'fan':
                c.add_track(t['net'], LAYER_NAMES.index(t['layer']), t['a'], t['b'], t['w'], 'fan')
        for v in d['vias']:
            if v['src'] == 'fan':
                c.add_via(v['net'], v['p'], v['d'], v['drill'], 'fan')
    return c


def inspect(R, net, w=0.2, via=(0.5, 0.3)):
    c = R.c
    comps = components(c, net, None)
    print('==', net, 'components:', [[p.name for p in k['pads']] + [f"{len(k['tracks'])}t{len(k['vias'])}v"] for k in comps])
    ok_b, tight_b, vok_b, okm, vok = R.masks(net, w, via)
    for k in comps:
        nodes = R.comp_nodes(k, okm)
        print('   comp', [p.name for p in k['pads']], 'legal nodes', len(nodes))
        for p in k['pads']:
            for L in p.layers:
                print('      pad', p.name, LAYER_NAMES[L], 'nodes', len(c.pad_nodes(p, L, okm[L])), 'all', len(c.pad_nodes(p, L)))
    return comps


if __name__ == '__main__':
    c = load_state(sys.argv[1], sys.argv[2])
    R = route.Router(c)
    for net in sys.argv[3:]:
        comps = inspect(R, net)
        t = time.time()
        okr = route.route_net(R, net, 0.2, (0.5, 0.3))
        print('   route_net', okr, round(time.time() - t, 1), 's')
