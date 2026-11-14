"""Export SELENO.step for Inventor from the KiCad board, without touching SelenoController.

The board is copied to a temp folder, footprints with missing 3D models get the
closest KiCad library model (or a simple placeholder block), and kicad-cli
exports the STEP into this folder.

Run:  python make_seleno_step.py
Then import SELENO.step into Inventor (see README.md).
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(r"Z:/ASTRA/SelenoController")
KICAD_CLI = r"C:/Users/alexg/AppData/Local/Programs/KiCad/10.0/bin/kicad-cli.exe"

# Missing model -> closest model in the KiCad 10 library (same body size).
SUBSTITUTES = {
    "RF_Module.3dshapes/ESP32-WROOM-32UE.step": "RF_Module.3dshapes/ESP32-WROOM-32U.step",
    "Package_SO.3dshapes/HTSOP-8-1EP_3.9x4.9mm_Pitch1.27mm.step": "Package_SO.3dshapes/HTSOP-8-1EP_3.9x4.9mm_P1.27mm.step",
    "Package_DFN_QFN.3dshapes/VQFN-28-1EP_5x5mm_P0.5mm_EP3.7x3.7mm.step": "Package_DFN_QFN.3dshapes/QFN-28-1EP_5x5mm_P0.5mm_EP3.35x3.35mm.step",
}

# Missing model with no library equivalent -> simple model built from boxes.
# Each box in footprint coordinates (mm, KiCad Y down, Z up from board top):
# (x0, x1, y0, y1, z0, z1, (r, g, b)).
IVORY, TIN, PTC_BODY, ANODISED_BLUE = (0.93, 0.91, 0.84), (0.80, 0.80, 0.82), (0.22, 0.24, 0.23), (0.10, 0.42, 0.86)
PLACEHOLDERS = {
    # P1 JST B2P-VH 2-pin 3.96 mm top-entry header, from JST eVH datasheet p.3:
    # width B 7.86, depth 8.5 incl. lock hook, posts 10.9 above board / 3.7 tail,
    # 1.1 mm square posts. Base height, lock wall and hook scaled from the side view.
    "Connector_JST.3dshapes/JST_VH_B2P-VH_1x02_P3.96mm_Vertical.step": ("P1_JST_B2P-VH", [
        (-1.95, 5.91, -1.95, 4.85, 0.0, 3.26, IVORY),     # base block
        (-1.55, 5.51, -3.11, -1.95, 0.0, 7.30, IVORY),    # locking wall
        (-1.55, 5.51, -3.65, -2.00, 7.30, 9.30, IVORY),   # lock hook
        (-0.55, 0.55, -0.55, 0.55, -3.7, 10.9, TIN),      # post 1
        (3.41, 4.51, -0.55, 0.55, -3.7, 10.9, TIN),       # post 2
    ]),
    # F1 Littelfuse 2920L300/15DR (LCSC C207089): datasheet mid values
    # A 6.73-7.98, B 4.80-5.44, C 0.75-1.25 mm. Dark body, tin end terminals.
    "Fuse.3dshapes/Fuse_2920_7451Metric.step": ("F1_2920L300", [
        (-2.68, 2.68, -2.56, 2.56, 0.0, 1.0, PTC_BODY),
        (-3.68, -2.68, -2.56, 2.56, 0.0, 1.0, TIN),
        (2.68, 3.68, -2.56, 2.56, 0.0, 1.0, TIN),
    ]),
}


def _heatsink(z0, ox=0.0, oy=0.0, w=14.0, l=15.0, h=13.0, fins=7, fin_t=1.0, base_t=2.2, notch=1.0):
    """Finned heatsink centred at footprint coords (ox, oy), fins running along Y."""
    gap = (w - fins * fin_t) / (fins - 1)
    boxes = [
        (-w/2 + notch, w/2 - notch, -l/2, l/2, z0, z0 + notch, ANODISED_BLUE),   # base under the edge notches
        (-w/2, w/2, -l/2, l/2, z0 + notch, z0 + base_t, ANODISED_BLUE),          # base
    ]
    for i in range(fins):
        x = -w/2 + i * (fin_t + gap)
        boxes.append((x, x + fin_t, -l/2, l/2, z0 + base_t, z0 + h, ANODISED_BLUE))
    return [(x0 + ox, x1 + ox, y0 + oy, y1 + oy, *rest) for x0, x1, y0, y1, *rest in boxes]


# Extra models added to footprints: reference -> (name, boxes).
# 14 x 15 x 13 mm stick-on heatsinks on the TMC2209s; QFN top is 0.93 mm, plus a 0.5 mm
# thermal pad so they clear D6 (SOD-123, up to 1.35 mm tall).
# Shifted off-centre (chip still fully covered) to clear P1, the motor JST-XH connectors and C1,
# leaving ~0.3 mm gaps. U7/U8 are rotated 180 deg, so footprint offsets are the negated board
# offsets: U7 board +2.1 mm right / 0.65 mm up, U8 board +1.0 mm right / 0.65 mm up.
HS_Z = 0.93 + 0.5
EXTRA_MODELS = {
    "U7": ("Heatsink_14x15x13_U7", _heatsink(z0=HS_Z, ox=-2.1, oy=0.65)),
    "U8": ("Heatsink_14x15x13_U8", _heatsink(z0=HS_Z, ox=-1.0, oy=0.65)),
}

# Reference -> Z scale for its 3D model (library model height is 10 mm).
MODEL_Z_SCALE = {
    "C1": 1.6,  # 680 uF can is ~15-16 mm tall; use 16 mm for clearance
}

# Through-hole parts fitted from the underside instead of the top (same holes, model flipped
# under the 1.6 mm board). J7 points down so the Pi ribbon runs straight down to the Pi.
MODEL_UNDERSIDE = {"J7"}
BOARD_T = 1.6


def _flip_model_under(text, ref):
    i = text.find(f'(property "Reference" "{ref}"')
    start = text.rfind("\n\t(footprint ", 0, i)
    end = text.find("\n\t(footprint ", i)
    end = len(text) if end < 0 else end
    fp = text[start:end]
    new = re.sub(r"(\(model .*?\(offset\s*\(xyz) [-\d.]+ [-\d.]+ [-\d.]+\)",
                 lambda m: f"{m.group(1)} 0 0 {-BOARD_T})", fp, count=1, flags=re.S)
    # 180 deg about the footprint Y axis (the pin row), so the pins stay in their holes
    new = re.sub(r"(\(model .*?\(rotate\s*\(xyz) [-\d.]+ [-\d.]+ [-\d.]+\)",
                 lambda m: f"{m.group(1)} 0 180 0)", new, count=1, flags=re.S)
    if new == fp:
        raise RuntimeError(f"no model block found for {ref}")
    return text[:start] + new + text[end:]


def box_step(name, boxes):
    """Minimal AP214 STEP holding one solid per box. Footprint Y-down converted to model Y-up."""
    ents = []
    breps = []
    for box in boxes:
        breps.append((_box_brep(ents, name, *box[:6]), box[6] if len(box) > 6 else None))
    return _wrap(ents, name, breps)


def _box_brep(ents, name, x0, x1, y0, y1, z0, z1):
    ya, yb = -y1, -y0
    pts = [(x, y, z) for z in (z0, z1) for y in (ya, yb) for x in (x0, x1)]  # index = x + 2y + 4z

    def add(text):
        ents.append(text)
        return f"#{len(ents)}"

    def vec(a, b):
        return tuple(b[i] - a[i] for i in range(3))

    def cross(a, b):
        return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

    def fmt(p):
        return "(" + ",".join(f"{c:.6f}" for c in p) + ")"

    cp = [add(f"CARTESIAN_POINT('',{fmt(p)})") for p in pts]
    vp = [add(f"VERTEX_POINT('',{c})") for c in cp]

    edges = {}
    def edge(a, b):
        key = (min(a, b), max(a, b))
        if key not in edges:
            s, e = key
            d = vec(pts[s], pts[e]); length = sum(c*c for c in d) ** 0.5
            dn = add(f"DIRECTION('',{fmt(tuple(c/length for c in d))})")
            vv = add(f"VECTOR('',{dn},{length:.6f})")
            ln = add(f"LINE('',{cp[s]},{vv})")
            edges[key] = add(f"EDGE_CURVE('',{vp[s]},{vp[e]},{ln},.T.)")
        return edges[key], ".T." if (a, b) == key else ".F."

    faces_def = [  # (vertex loop, outward normal)
        ([0, 1, 3, 2], (0, 0, -1)), ([4, 5, 7, 6], (0, 0, 1)),
        ([0, 1, 5, 4], (0, -1, 0)), ([2, 3, 7, 6], (0, 1, 0)),
        ([0, 2, 6, 4], (-1, 0, 0)), ([1, 3, 7, 5], (1, 0, 0)),
    ]
    faces = []
    for loop, n in faces_def:
        a, b, c = pts[loop[0]], pts[loop[1]], pts[loop[2]]
        if sum(x*y for x, y in zip(cross(vec(a, b), vec(b, c)), n)) < 0:
            loop = loop[::-1]  # make loop counter-clockwise seen from outside
        oes = []
        for i in range(4):
            ec, sense = edge(loop[i], loop[(i+1) % 4])
            oes.append(add(f"ORIENTED_EDGE('',*,*,{ec},{sense})"))
        el = add(f"EDGE_LOOP('',({','.join(oes)}))")
        fb = add(f"FACE_OUTER_BOUND('',{el},.T.)")
        nd = add(f"DIRECTION('',{fmt(n)})")
        ref = (1, 0, 0) if n[0] == 0 else (0, 1, 0)
        rd = add(f"DIRECTION('',{fmt(ref)})")
        ax = add(f"AXIS2_PLACEMENT_3D('',{cp[loop[0]]},{nd},{rd})")
        pl = add(f"PLANE('',{ax})")
        faces.append(add(f"ADVANCED_FACE('',({fb}),{pl},.T.)"))
    sh = add(f"CLOSED_SHELL('',({','.join(faces)}))")
    return add(f"MANIFOLD_SOLID_BREP('{name}',{sh})")


def _wrap(ents, name, breps):
    def add(text):
        ents.append(text)
        return f"#{len(ents)}"

    br = ",".join(b for b, _ in breps)
    o = add("CARTESIAN_POINT('',(0.,0.,0.))")
    z = add("DIRECTION('',(0.,0.,1.))"); x = add("DIRECTION('',(1.,0.,0.))")
    origin = add(f"AXIS2_PLACEMENT_3D('',{o},{z},{x})")
    lu = add("( LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.) )")
    au = add("( NAMED_UNIT(*) PLANE_ANGLE_UNIT() SI_UNIT($,.RADIAN.) )")
    su = add("( NAMED_UNIT(*) SI_UNIT($,.STERADIAN.) SOLID_ANGLE_UNIT() )")
    unc = add(f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-07),{lu},'distance_accuracy_value','')")
    ctx = add(f"( GEOMETRIC_REPRESENTATION_CONTEXT(3) GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT(({unc})) "
              f"GLOBAL_UNIT_ASSIGNED_CONTEXT(({lu},{au},{su})) REPRESENTATION_CONTEXT('','') )")
    rep = add(f"ADVANCED_BREP_SHAPE_REPRESENTATION('',({origin},{br}),{ctx})")
    styled = []
    for b, rgb in breps:
        if rgb is None:
            continue
        c = add("COLOUR_RGB('',{:.3f},{:.3f},{:.3f})".format(*rgb))
        fc = add(f"FILL_AREA_STYLE_COLOUR('',{c})")
        fs = add(f"FILL_AREA_STYLE('',({fc}))")
        sf = add(f"SURFACE_STYLE_FILL_AREA({fs})")
        ss = add(f"SURFACE_SIDE_STYLE('',({sf}))")
        su_ = add(f"SURFACE_STYLE_USAGE(.BOTH.,{ss})")
        psa = add(f"PRESENTATION_STYLE_ASSIGNMENT(({su_}))")
        styled.append(add(f"STYLED_ITEM('color',({psa}),{b})"))
    if styled:
        add(f"MECHANICAL_DESIGN_GEOMETRIC_PRESENTATION_REPRESENTATION('',({','.join(styled)}),{ctx})")
    ac = add("APPLICATION_CONTEXT('automotive_design')")
    add(f"APPLICATION_PROTOCOL_DEFINITION('international standard','automotive_design',2000,{ac})")
    pc = add(f"PRODUCT_CONTEXT('',{ac},'mechanical')")
    pr = add(f"PRODUCT('{name}','{name}','',({pc}))")
    pdf = add(f"PRODUCT_DEFINITION_FORMATION('','',{pr})")
    pdc = add(f"PRODUCT_DEFINITION_CONTEXT('part definition',{ac},'design')")
    pd = add(f"PRODUCT_DEFINITION('design','',{pdf},{pdc})")
    pds = add(f"PRODUCT_DEFINITION_SHAPE('','',{pd})")
    add(f"SHAPE_DEFINITION_REPRESENTATION({pds},{rep})")

    body = "\n".join(f"#{i+1}={t};" for i, t in enumerate(ents))
    return ("ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('placeholder'),'2;1');\n"
            f"FILE_NAME('{name}.step','',(''),(''),'','','');\n"
            "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\n"
            f"DATA;\n{body}\nENDSEC;\nEND-ISO-10303-21;\n")


def _scale_model_z(text, ref, zs):
    i = text.find(f'(property "Reference" "{ref}"')
    start = text.rfind("\n\t(footprint ", 0, i)
    end = text.find("\n\t(footprint ", i)
    end = len(text) if end < 0 else end
    fp = text[start:end]
    new = re.sub(r"(\(model .*?\(scale\s*\(xyz) ([-\d.]+) ([-\d.]+) ([-\d.]+)\)",
                 lambda m: f"{m.group(1)} {m.group(2)} {m.group(3)} {zs})", fp, count=1, flags=re.S)
    if new == fp:
        raise RuntimeError(f"no model scale found for {ref}")
    return text[:start] + new + text[end:]


def _add_model(text, ref, path):
    """Add a second 3D model to a footprint, right after its existing model."""
    i = text.find(f'(property "Reference" "{ref}"')
    start = text.rfind("\n\t(footprint ", 0, i)
    m = text.find("(model ", start)
    depth, j = 0, m
    while True:
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    block = (f'\n\t\t(model "{path}"\n\t\t\t(offset (xyz 0 0 0))\n'
             f'\t\t\t(scale (xyz 1 1 1))\n\t\t\t(rotate (xyz 0 0 0))\n\t\t)')
    return text[:j + 1] + block + text[j + 1:]


def main():
    work = Path(tempfile.mkdtemp(prefix="seleno_step_"))
    try:
        for f in ("SELENO.kicad_pcb", "SELENO.kicad_pro"):
            shutil.copy2(SRC / f, work / f)
        pcb = work / "SELENO.kicad_pcb"
        text = pcb.read_text(encoding="utf-8")
        for old, new in SUBSTITUTES.items():
            text = text.replace("${KICAD10_3DMODEL_DIR}/" + old, "${KICAD10_3DMODEL_DIR}/" + new)
        for old, (name, boxes) in PLACEHOLDERS.items():
            path = work / f"{name}.step"
            path.write_text(box_step(name, boxes), encoding="ascii")
            text = text.replace("${KICAD10_3DMODEL_DIR}/" + old, path.as_posix())
        for ref, zs in MODEL_Z_SCALE.items():
            text = _scale_model_z(text, ref, zs)
        for ref in MODEL_UNDERSIDE:
            text = _flip_model_under(text, ref)
        for ref, (name, boxes) in EXTRA_MODELS.items():
            path = work / f"{name}.step"
            path.write_text(box_step(name, boxes), encoding="ascii")
            text = _add_model(text, ref, path.as_posix())
        pcb.write_text(text, encoding="utf-8")
        out = HERE / "SELENO.step"
        subprocess.run([KICAD_CLI, "pcb", "export", "step", "--subst-models", "--no-dnp", "--force",
                        "-o", out.as_posix(), pcb.as_posix()], check=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
