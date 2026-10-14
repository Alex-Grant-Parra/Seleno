"""Parse a KiCad s-expression netlist and dump components + nets."""
import sys, re, json

def tokenize(s):
    for m in re.finditer(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+', s):
        t = m.group(0)
        if t.startswith('"'):
            yield ('S', t[1:-1].replace('\\"', '"'))
        else:
            yield ('T', t)

def parse(s):
    stack = [[]]
    for kind, t in tokenize(s):
        if kind == 'T' and t == '(':
            stack.append([])
        elif kind == 'T' and t == ')':
            x = stack.pop()
            stack[-1].append(x)
        else:
            stack[-1].append(t)
    return stack[0][0]

def find(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]

def val(node, name, default=''):
    f = find(node, name)
    return f[0][1] if f and len(f[0]) > 1 else default

if __name__ == '__main__':
    tree = parse(open(sys.argv[1], encoding='utf-8').read())
    comps = {}
    for c in find(find(tree, 'components')[0], 'comp'):
        ref = val(c, 'ref')
        props = {p[1][1]: p[2][1] for p in find(c, 'property') if len(p) > 2}
        ls = find(c, 'libsource')
        lib = ''
        if ls:
            lib = val(ls[0], 'lib') + ':' + val(ls[0], 'part')
        comps[ref] = dict(value=val(c, 'value'), footprint=val(c, 'footprint'),
                          datasheet=val(c, 'datasheet'), lib=lib, props=props)
    nets = []
    for n in find(find(tree, 'nets')[0], 'net'):
        name = val(n, 'name')
        nodes = []
        for nd in find(n, 'node'):
            nodes.append((val(nd, 'ref'), val(nd, 'pin'), val(nd, 'pinfunction'), val(nd, 'pintype')))
        nets.append((name, nodes))

    out = sys.argv[2]
    with open(out + '_comps.txt', 'w', encoding='utf-8') as f:
        for r in sorted(comps, key=lambda x: (re.sub(r'\d', '', x), int(re.sub(r'\D', '', x) or 0))):
            c = comps[r]
            extra = {k: v for k, v in c['props'].items() if k not in ('Sheetname', 'Sheetfile', 'ki_description', 'ki_keywords', 'ki_fp_filters')}
            f.write(f"{r}\t{c['value']}\t{c['lib']}\t{c['footprint']}\t{c['datasheet']}\t{extra}\n")
    with open(out + '_nets.txt', 'w', encoding='utf-8') as f:
        for name, nodes in nets:
            f.write(f"NET {name}  ({len(nodes)})\n")
            for r, p, fn, t in nodes:
                f.write(f"    {r}.{p}  {fn}  [{t}]\n")
    # per-component pin map
    with open(out + '_pins.txt', 'w', encoding='utf-8') as f:
        pinmap = {}
        for name, nodes in nets:
            for r, p, fn, t in nodes:
                pinmap.setdefault(r, []).append((p, fn, t, name))
        for r in sorted(pinmap):
            f.write(f"== {r} ({comps.get(r, {}).get('value', '?')})\n")
            def k(x):
                try: return (0, int(x[0]))
                except: return (1, x[0])
            for p, fn, t, name in sorted(pinmap[r], key=k):
                f.write(f"   {p:>4} {fn:<14} {t:<14} -> {name}\n")
