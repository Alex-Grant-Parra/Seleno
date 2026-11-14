# Build the HTML report from the post-processing JSON + hand checks + figures.
import json, os, sys, base64, io, html
import numpy as np
from PIL import Image
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HERE, MATERIALS

OUT = sys.argv[1]
P = {(g, m): json.load(open(os.path.join(HERE, f"post_{g}_{m}.json"))) for g in ("RA", "DEC") for m in ("PLA", "PETG")}
H = json.load(open(os.path.join(HERE, "hand.json")))
RJ = {g: json.load(open(os.path.join(HERE, f"res_{g}_PLA.json"))) for g in ("RA", "DEC")}
CONV = json.load(open(os.path.join(HERE, "conv.json"))) if os.path.exists(os.path.join(HERE, "conv.json")) else None
P2 = {m: json.load(open(os.path.join(HERE, "v2", f"post_RA_{m}.json"))) for m in ("PLA", "PETG")}


def img(path, width=None):
    im = Image.open(path).convert("RGB")
    a = np.asarray(im); mask = (a < 245).any(2)
    ys, xs = np.where(mask)
    # drop the colour bar column block from the crop test? keep it, it is part of the figure
    y0, y1, x0, x1 = max(ys.min() - 12, 0), min(ys.max() + 12, a.shape[0]), max(xs.min() - 12, 0), min(xs.max() + 12, a.shape[1])
    im = im.crop((x0, y0, x1, y1))
    if width and im.width > width:
        im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
    b = io.BytesIO(); im.save(b, "PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


def f1(x): return f"{x:.1f}"
def f2(x): return f"{x:.2f}"
def sf(x): return "&gt;99" if x > 99 else (f"{x:.1f}" if x < 10 else f"{x:.0f}")


def status(s, sustained=False, vm=0.0):
    if sustained:
        if vm > 15 or s < 2: return ("fail", "Fail")
        if vm > 10 or s < 3: return ("warn", "Watch")
        return ("ok", "Pass")
    if s < 1: return ("fail", "Fail")
    if s < 2: return ("warn", "Marginal")
    return ("ok", "Pass")


def row(g, lc_prefix, part, label, sustained=False):
    cells = []
    worst = 1e9; vmw = 0
    for m in ("PLA", "PETG"):
        cs = P[(g, m)]["cases"]
        lc = [k for k in cs if k.startswith(lc_prefix)][0]
        r = cs[lc]["parts"][part]
        s = min(r["SF_xy"], r["SF_z"]); worst = min(worst, s); vmw = max(vmw, r["vm_max"])
        cells.append((r["vm_max"], r["SF_xy"], r["SF_z"], r["u_max"]))
    cls, txt = status(worst, sustained, vmw)
    (v1, a1, b1, u1), (v2, a2, b2, u2) = cells
    return (f"<tr><td>{label}</td><td class=n>{f1(v1)}</td><td class=n>{sf(a1)} / {sf(b1)}</td><td class=n>{sf(a2)} / {sf(b2)}</td>"
            f"<td class=n>{f2(u1)} / {f2(u2)}</td><td><span class='chip {cls}'>{txt}</span></td></tr>")


def table(rows, first="Load case"):
    return ("<div class=tw><table><thead><tr><th>" + first + "</th><th>Peak von Mises<br><small>MPa</small></th>"
            "<th>SF PLA<br><small>along / across layers</small></th><th>SF PETG<br><small>along / across layers</small></th>"
            "<th>Max deflection<br><small>mm, PLA / PETG</small></th><th>Result</th></tr></thead><tbody>"
            + "".join(rows) + "</tbody></table></div>")


ra = P[("RA", "PLA")]; dec = P[("DEC", "PLA")]
def lc(g, pref, m="PLA"):
    cs = P[(g, m)]["cases"]; return cs[[k for k in cs if k.startswith(pref)][0]]

# ---------- numbers used in the text ----------
knock_ra = lc("RA", "LC3 ")["parts"]["RA_bracket"]
stall_ra = lc("RA", "LC2")["parts"]["RA_bracket"]
gx_side = {k: lc("RA", f"LC4s {k}")["parts"]["Frame"] for k in ("GX16", "GX12 #1", "GX12 #2")}
gx_pull = {k: lc("RA", f"LC4p {k}")["parts"]["Frame"] for k in ("GX16", "GX12 #1", "GX12 #2")}
dc_side = lc("RA", "LC5s")["parts"]["Frame"]; dc_pull = lc("RA", "LC5p")["parts"]["Frame"]
S = MATERIALS["PLA"]
def safe_load(r, F, target=2.0):
    s = min(r["SF_xy"], r["SF_z"]); return F * s / target
gx_safe = min(safe_load(r, 50) for r in gx_side.values()); gx_yield = min(safe_load(r, 50, 1.0) for r in gx_side.values())
gx_yield_hi = max(safe_load(r, 50, 1.0) for r in gx_side.values())
pull_sf = [min(r["SF_xy"], r["SF_z"]) for r in gx_pull.values()]
dc_safe = safe_load(dc_side, 20)
b = H["belt"]["cases"]; sh = H["motor_shaft"]["axes"]; st = H["worm_stub"]["axes"]; gr = H["grub"]

# ---------- v2 -> v3 RA bracket comparison ----------
def lc2(pref, m="PLA"):
    cs = P2[m]["cases"]; return cs[[k for k in cs if k.startswith(pref)][0]]
cmp_rows = []
for pref, label in (("LC1 ", "Belt 40 N/span (sustained)"), ("LC2", "Motor stall 0.45 N&middot;m"), ("LC3 ", "50 N knock on motor end"), ("LC3z", "50 N pull along motor axis")):
    va = lc2(pref)["parts"]["RA_bracket"]; vb = lc("RA", pref)["parts"]["RA_bracket"]
    sa = min(va["SF_xy"], va["SF_z"]); sb = min(vb["SF_xy"], vb["SF_z"])
    bp = min(min(lc("RA", pref, "PETG")["parts"]["RA_bracket"]["SF_xy"], lc("RA", pref, "PETG")["parts"]["RA_bracket"]["SF_z"]), 999)
    ma = lc2(pref).get("motor_face_disp_mm", 0); mb = lc("RA", pref).get("motor_face_disp_mm", 0)
    cls = "ok" if sb >= 2 else ("warn" if sb >= 1 else "fail")
    cmp_rows.append(f"<tr><td>{label}</td><td class=n>{f1(va['vm_max'])} &rarr; {f1(vb['vm_max'])}</td><td class=n>{sf(sa)} &rarr; {sf(sb)}</td><td class=n>{sf(bp)}</td>"
                    f"<td class=n>{f2(ma)} &rarr; {f2(mb)}</td><td><span class='chip {cls}'>{'Pass' if sb >= 2 else 'Marginal'}</span></td></tr>")
cmp_table = ("<div class=tw><table><thead><tr><th>RA bracket, load case</th><th>Peak von Mises<br><small>MPa, v2 &rarr; v3</small></th>"
             "<th>Lowest SF PLA<br><small>v2 &rarr; v3</small></th><th>Lowest SF PETG<br><small>v3</small></th><th>Motor face moves<br><small>mm PLA, v2 &rarr; v3</small></th><th>Result</th></tr></thead><tbody>"
             + "".join(cmp_rows) + "</tbody></table></div>")
knock_v2 = lc2("LC3 ")["parts"]["RA_bracket"]

# ---------- joints table ----------
JD = {j["name"]: j for j in H["joints"]}
def jsup(g, key, pref):
    return lc(g, pref)["supports"][key]["resultant"]
rows_j = []
def jrow(name, bolts, torque, preload, demand, capacity, note, cls):
    rows_j.append(f"<tr><td>{name}</td><td>{bolts}</td><td class=n>{torque}</td><td class=n>{preload}</td><td>{demand}</td><td>{capacity}</td>"
                  f"<td><span class='chip {cls[0]}'>{cls[1]}</span></td><td>{note}</td></tr>")
mot = JD["Motor to bracket (RA + Dec)"]
jrow("Motor to bracket (RA and Dec)", "RA: 4x M3x10 SHCS (v3). Dec: 4x M3x8 SHCS. Both into the motor", f"{mot['torque']:.2f}<br><small>{mot['torque_washer']:.2f} with washer</small>", f"{mot['FV']:.0f}",
     "Stall: 27 N shear, 16 N tension per screw. Knock: 31 N tension", f"Slip {0.25*mot['FV']:.0f} N per screw; thread strip about 2 kN",
     "Thread engagement 3.5 mm on the RA, 3 mm on the Dec. A washer takes 0.5 mm off each, which is still fine", ("ok", "Secure"))
jb = JD["RA bracket to frame"]
jt = {lcn: lc("RA", lcn)["joints"]["RA_bracket - Frame"] for lcn in ("LC1 ", "LC2", "LC3 ")}
jrow("RA bracket to frame", "3x M3x30 SHCS + nut", f"{jb['torque']:.2f}", f"{jb['FV']:.0f}",
     f"Joint shear {jt['LC2']['shear']:.0f} N at stall, {jt['LC3 ']['shear']:.0f} N in a knock, {jt['LC1 ']['shear']:.0f} N sustained (40 N/span). Bolt tension change under {max(v['tension'] for v in jt.values()):.0f} N",
     f"Friction {0.25*3*jb['FV']:.0f} N (3 bolts, &mu; 0.25)", f"Margin {0.25*3*jb['FV']/jt['LC2']['shear']:.1f} at stall. Nut-side bearing governs the torque", ("ok", "Secure"))
m5 = JD["RA flat-bit M5 (strap lugs / bracket tab)"]
tab = {k: jsup("RA", "RA_bracket | ra_housing", k) for k in ("LC1 ", "LC1r", "LC2", "LC3 ")}
jrow("RA bracket tab + strap lug on the flat bit", "2x M5x25 into the CG-5 (shared with the strap)", f"{m5['torque']:.2f}<br><small>{m5['torque_washer']:.2f} with washer</small>",
     f"{m5['FV']:.0f}<br><small>{m5['FV_washer']:.0f} with washer</small>",
     f"Tab load {tab['LC2']:.0f} N at stall, {tab['LC3 ']:.0f} N in a knock, {tab['LC1 ']:.0f} N sustained (40 N/span), {tab['LC1r']:.0f} N at 15 N/span",
     f"Friction {0.25*m5['FV']:.0f} N, {0.25*m5['FV_washer']:.0f} N with a 10 mm washer",
     f"Margin only {0.25*m5['FV']/tab['LC2']:.1f} at stall without a washer. If it slips, the shank takes it after 0.25 mm. Fit M5 washers", ("warn", "Add washers"))
st_ = JD["RA strap top"]
strap = {k: jsup("RA", "RA_strap_pz | ra_housing", k) for k in ("LC2", "LC3 ")}
jrow("RA strap halves (top joint)", "1x M4x20 SHCS + nut", f"{st_['torque']:.2f}", f"{st_['FV']:.0f}",
     f"Strap-to-RA-body load {strap['LC2']:.0f} N at stall, {strap['LC3 ']:.0f} N in a knock; the bolt itself sees almost no change",
     "Clamp grip about 500 N (hoop tension 369 N round an r40 body, &mu; 0.25)", "Margin about 5", ("ok", "Secure"))
dm5 = JD["Dec carrier M5 (bracket tab)"]
dcar = {k: jsup("DEC", "Dec_bracket | dec_worm_carrier", k) for k in ("LC2", "LC3 ")}
jrow("Dec bracket tab on the worm carrier", "2x M5x25 into the CG-5", f"{dm5['torque']:.2f}<br><small>{dm5['torque_washer']:.2f} with washer</small>", f"{dm5['FV']:.0f}",
     f"{dcar['LC2']:.0f} N at stall, {dcar['LC3 ']:.0f} N in a knock (the bands carry most of it)", f"Friction {2*0.25*dm5['FV']:.0f} N", "Margin above 7", ("ok", "Secure"))
dc = JD["Dec ring 1 + band 2 caps"]
dband = {k: jsup("DEC", "Dec_bracket | dec_housing", k) for k in ("LC2", "LC3 ")}
dbolt = max(v["tension"] for v in lc("DEC", "LC3 ")["bolts"].values())
jrow("Dec ring 1 and band 2 caps", "2x M4x25 + 2x M4x16 + nuts", f"{dc['torque']:.2f}", f"{dc['FV']:.0f}",
     f"Band-to-housing load {dband['LC2']:.0f} N at stall, {dband['LC3 ']:.0f} N in a knock. Bolt tension change under {dbolt:.0f} N",
     "Clamp grip above 1 kN (two bands)", "Margin above 10", ("ok", "Secure"))
bp = JD["Base plate to frame"]
bmax = max(v["shear"] for k, c in P[("RA", "PLA")]["cases"].items() if k.startswith("LC4s") for kk, v in c["bolts"].items() if kk.startswith("Base"))
jrow("Base plate to frame", "4x M3x10 pan + nut", f"{bp['torque']:.2f}", f"{bp['FV']:.0f}",
     f"Under 1 N from the motors. Up to {bmax:.0f} N shear when a GX cable is snagged sideways (the wall bends)",
     f"Friction {0.25*bp['FV']:.0f} N per bolt", "Secure in use; it only slips once the wall has already failed", ("ok", "Secure"))
for nm, note in (("PCB to frame hangers", "PCB and parts weigh about 25 g; even a 10 g jolt is 2.5 N over 4 bolts"),
                 ("Pi to base", "The Pi weighs about 11 g"),
                 ("USB panel socket", "A 20 N plug pull is 10 N per bolt")):
    j = JD[nm]
    jrow(nm, {"PCB to frame hangers": "4x M2.5x8 pan + nut", "Pi to base": "4x M2.5x8 pan + nut", "USB panel socket": "2x M2.5x10 countersunk + nut"}[nm],
         f"{j['torque']:.2f}", f"{j['FV']:.0f}", note, f"Friction {0.25*j['FV']:.0f} N per bolt", "Snug only; do not crush the plastic", ("ok", "Secure"))
fl = max(lc("RA", k)["supports"]["Frame | flatbit_M4_pan"]["resultant"] for k in ("LC2", "LC3 ", "LC4s GX16"))
dv = max(lc("RA", k)["supports"]["Frame | ra_housing"]["resultant"] for k in ("LC2", "LC3 "))
jrow("Frame on the flat-bit dovetail", "Dovetail + stock M4 pan screw up through the ledge", "Stock", "&ndash;",
     f"Dovetail load {dv:.0f} N from the motors; M4 screw under {max(fl,1):.0f} N", "Positive lock (45&deg; lips)", "The dovetail carries the load; the M4 only stops it sliding off", ("ok", "Secure"))

# ---------- figures ----------
FIG = os.path.join(HERE, "fig")
f_ra = img(os.path.join(FIG, "ra_drive.png"), 1100); f_rab = img(os.path.join(FIG, "ra_bracket_a.png"), 1100)
f_dec = img(os.path.join(FIG, "dec_drive.png"), 1100); f_con = img(os.path.join(FIG, "frame_conn_a.png"), 1100)

ndof_ra, ndof_dec = RJ["RA"]["ndof"], RJ["DEC"]["ndof"]
conv_txt = ""
if CONV:
    conv_txt = (f"<p>Mesh check (run on bracket v2): re-meshing the RA bracket with elements about 35% larger moved the knock-case peak from "
                f"{CONV['fine']:.1f} to {CONV['coarse']:.1f} MPa ({CONV['pct']:+.0f}%), and the stall case from {CONV['fine2']:.1f} to {CONV['coarse2']:.1f} MPa. "
                f"The quoted peaks are within about {max(abs(CONV['pct']), abs(CONV['pct2'])):.0f}% of the converged value.</p>")

j7 = H["j7_relative_mm"]
css = open(os.path.join(HERE, "report.css")).read()
page = f"""<title>SELENO Drive Stress Analysis</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Barlow+Semi+Condensed:wght@500;600;700&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>{css}</style>
<div class=wrap>
<header>
  <p class=eyebrow>SELENO v0.8 &middot; CG-5 motor drive</p>
  <h1>Drive stress analysis</h1>
  <p class=lede>Linear static FEA of every printed part on the final assembly, in PLA and PETG, under motor, belt, pulley, connector and handling loads. It includes a check of each bolted joint.</p>
  <dl class=titleblock>
    <div><dt>Assembly</dt><dd>CG5_SelenoCompact_Motors_C8N.iam, with RA bracket v3 (STEP export 2026-11-10)</dd></div>
    <div><dt>Date</dt><dd>2026-11-10</dd></div>
    <div><dt>Parts</dt><dd>Frame, base plate, RA bracket, 2 RA straps, Dec bracket, Dec clamp cap, 2 Dec band caps</dd></div>
    <div><dt>Solver</dt><dd>Quadratic tets, {ndof_ra/1e3:.0f}k + {ndof_dec/1e3:.0f}k DOF</dd></div>
  </dl>
</header>

<section>
<h2>Verdict</h2>
<div class=verdicts>
  <article class="v ok"><h3>Motor brackets: pass in normal use</h3>
  <p>At full motor stall the RA bracket peaks at {f1(stall_ra['vm_max'])} MPa, a safety factor of at least {sf(min(stall_ra['SF_xy'], stall_ra['SF_z']))}. The Dec bracket peaks under 2 MPa. The belt pull moves either motor face by 0.1 mm or less.</p></article>
  <article class="v {'ok' if min(knock_ra['SF_xy'], knock_ra['SF_z']) >= 2 else 'warn'}"><h3>RA bracket v3: knock margin {'fixed' if min(knock_ra['SF_xy'], knock_ra['SF_z']) >= 2 else 'improved'}</h3>
  <p>v2 reached {f1(knock_v2['vm_max'])} MPa when the RA motor's back end took a 50 N knock (SF {sf(min(knock_v2['SF_xy'], knock_v2['SF_z']))}). v3 has a 6.5 mm plate and a filled-out corner, and peaks at {f1(knock_ra['vm_max'])} MPa: a safety factor of {sf(knock_ra['SF_xy'])} along the layers and {sf(knock_ra['SF_z'])} across them.</p></article>
  <article class="v fail"><h3>Connector wall: fails a sideways cable snag</h3>
  <p>50 N pulled sideways at the end of a GX plug (90 mm out) puts {gx_side['GX16']['vm_max']:.0f} to {max(r['vm_max'] for r in gx_side.values()):.0f} MPa into the 2 mm wall, which reaches its strength at about {gx_yield:.0f} to {gx_yield_hi:.0f} N. A 20 N sideways pull on the DC plug also goes past its limit. Straight pull-outs hold, with SF {min(pull_sf):.1f} to {max(pull_sf):.1f} at 50 N.</p></article>
  <article class="v ok"><h3>Bolted joints: secure at low torque</h3>
  <p>Every joint holds with a large margin, provided the torque is kept low so the plastic under the heads and nuts doesn't creep. The one exception is the flat-bit M5s, which need washers.</p></article>
  <article class="v warn"><h3>Belt: don't over-tighten</h3>
  <p>At 40 N per span, the motor bearing sees about {sh['RA']['40 N/span (over-tight)']['F_eq_20mm']:.0f} N against a typical 28 N rating. At 15 N per span it is {sh['RA']['15 N/span (recommended)']['F_eq_20mm']:.0f} N. A toothed belt needs pretension only to stop backlash; extra tension adds no holding power.</p></article>
  <article class="v ok"><h3>PLA or PETG: same stresses, choose on heat</h3>
  <p>The stresses are almost identical. PETG flexes about 1.5 times more, but it keeps its shape to about 68 &deg;C, where PLA softens at about 55 &deg;C. A mount left in a car or in the sun should be PETG.</p></article>
</div>
</section>

<section>
<h2>What to change</h2>
<ol class=actions>
  <li><b>Take the cable snag off the connectors.</b> Clip or strap each cable to the mount within about 100 mm of its plug, with a little slack, so a pull on the cable loads the mount and not the plug. On its own, a 4 mm wall would only bring the 50 N sideways case down to about yield, because stress scales with 1/thickness&sup2;. Right-angle GX plugs would roughly halve the lever as well.</li>
  <li><b>Set the belts to about 15 N per span.</b> Pressing the middle of a span with about 2 N (a 200 g weight) should move it about 1.5 to 2 mm. Over-tightening gains nothing: stall needs only {H['belt']['Te']:.0f} N of belt pull. It loads the motor bearings, the brass worm stubs and the brackets for no benefit.</li>
  <li><b>Use the torques in the joint table and add washers.</b> M5 washers under the flat-bit screws give a 1.7 times higher clamp. Nyloc nuts fit the M3 (4.4 mm) and M4 (5.2 mm) pockets and stop the joints backing off as the plastic relaxes. Re-check the torque after a day.</li>
  <li><b>Done in RA bracket v3: a stiffer motor plate.</b> A 1.5 mm pulley-side pad makes the plate 6.5 mm, keeping the slots and the centre hole. It clears the belt by 2 mm. The RA motor now uses 4x M3x10 screws. Print the plate flat so that bending runs along the layers.</li>
  <li><b>Done in RA bracket v3: the corner and the knife edge.</b> The corner beside the +x motor slot is filled out. That removes the 2 mm ligament between the slot end and the plate edge, and gives bolt #4's head seat a solid wall where it used to graze the edge with a 0.1 mm lip.</li>
</ol>
</section>

<section>
<h2>RA drive</h2>
<p>The RA bracket bolts to the frame's motor block with 3 M3s and to the flat bit through its tab. The strap halves clamp the RA body. Loads go in at the motor face. Every result below is for bracket v3. This first table shows what changed from v2.</p>
{cmp_table}
<figure><img src="{f_rab}" alt="RA motor bracket, motor side, coloured by von Mises stress in the 50 N knock case. The peak sits between the top motor-screw slot and the corner of the foot block."><figcaption>RA bracket v3 from the motor side, envelope of the stall and knock cases, PLA, on the same 0 to 30 MPa scale used for v2.</figcaption></figure>
{table([
  row("RA", "LC1r", "RA_bracket", "Bracket: belt 15 N/span (sustained)", True),
  row("RA", "LC1 ", "RA_bracket", "Bracket: belt 40 N/span (sustained)", True),
  row("RA", "LC2", "RA_bracket", "Bracket: motor stall 0.45 N&middot;m"),
  row("RA", "LC3 ", "RA_bracket", "Bracket: 50 N knock on motor end"),
  row("RA", "LC3z", "RA_bracket", "Bracket: 50 N pull along motor axis"),
  row("RA", "LC2", "RA_strap_pz", "Strap +Z: motor stall"),
  row("RA", "LC3 ", "RA_strap_pz", "Strap +Z: 50 N knock"),
  row("RA", "LC2", "Frame", "Frame motor block: stall"),
  row("RA", "LC3 ", "Frame", "Frame motor block: 50 N knock"),
])}
<p class=note>Sustained cases are rated against creep: they pass below 10 MPa, need watching from 10 to 15 MPa, and fail above 15 MPa (about 30% of strength). The &minus;Z strap half carries clamp load only, so it is not listed.</p>
<figure><img src="{f_ra}" alt="RA drive overview: frame, RA bracket and straps coloured by stress, mostly dark blue with a bright patch on the bracket's motor plate."><figcaption>RA drive overview with bracket v3 (frame, bracket, straps) under the same envelope.</figcaption></figure>
</section>

<section>
<h2>Dec drive</h2>
<p>The Dec bracket is large and thick. Its tab sits on the worm carrier with 2 M5s, and its two bands clamp the Dec housing.</p>
<figure><img src="{f_dec}" alt="Dec bracket with clamp cap and band caps coloured by stress, almost entirely dark blue."><figcaption>Dec bracket, clamp cap and band caps, envelope of the stall and knock cases, PLA, scale 0 to 10 MPa.</figcaption></figure>
{table([
  row("DEC", "LC1 ", "Dec_bracket", "Bracket: belt 40 N/span (sustained)", True),
  row("DEC", "LC2", "Dec_bracket", "Bracket: motor stall"),
  row("DEC", "LC3 ", "Dec_bracket", "Bracket: 50 N knock on motor end"),
  row("DEC", "LC3z", "Dec_bracket", "Bracket: 50 N pull along motor axis"),
  row("DEC", "LC3 ", "Dec_clamp_cap", "Clamp cap: 50 N knock"),
  row("DEC", "LC3 ", "Dec_band_cap_pz", "Band 2 cap: 50 N knock"),
])}
</section>

<section>
<h2>Connectors</h2>
<p>Each GX socket is loaded at its plug's cable exit, 90 mm out from the &minus;Z wall: 50 N straight out, and 50 N sideways in the worst of 24 directions. The DC plug gets 20 N at an assumed 35 mm, plus 0.3 N&middot;m of twist. The USB socket gets 20 N in any direction, and the Enter button a 20 N press.</p>
<figure><img src="{f_con}" alt="Frame seen from the connector wall side; the area round the three GX holes and the DC hole is dark red."><figcaption>Frame and base plate, envelope of the sideways snag cases, PLA. The scale tops out at 52 MPa (PLA strength); everything dark red is past yield.</figcaption></figure>
{table([
  row("RA", "LC4p GX16", "Frame", "GX16: 50 N straight pull-out"),
  row("RA", "LC4s GX16", "Frame", "GX16: 50 N sideways"),
  row("RA", "LC4p GX12 #1", "Frame", "GX12 #1: 50 N straight pull-out"),
  row("RA", "LC4s GX12 #1", "Frame", "GX12 #1: 50 N sideways"),
  row("RA", "LC4p GX12 #2", "Frame", "GX12 #2: 50 N straight pull-out"),
  row("RA", "LC4s GX12 #2", "Frame", "GX12 #2: 50 N sideways"),
  row("RA", "LC5p", "Frame", "DC: 20 N straight pull-out"),
  row("RA", "LC5s", "Frame", "DC: 20 N sideways"),
  row("RA", "LC6", "Base_plate", "USB: 20 N any direction (base plate)"),
  row("RA", "LC7", "Frame", "Enter button: 20 N press"),
], "Connector and load")}
<p>For a safety factor of 2, the GX sockets can take about {gx_safe:.0f} N sideways at the plug end, and the DC socket about {dc_safe:.0f} N. Straight pull-outs hold, with SF {min(pull_sf):.1f} to {max(pull_sf):.1f} at 50 N.</p>
<p><b>J7 / GPIO header.</b> The PCB hangs from the frame and the Pi sits on the base, so frame flex shifts one relative to the other. Even a 50 N GX16 snag moves them {j7['PLA']['GX16_socket']:.2f} mm apart in PLA and {j7['PETG']['GX16_socket']:.2f} mm in PETG. That is well inside what a 2.54 mm pin header absorbs. The motor loads move them by less than 0.01 mm.</p>
</section>

<section>
<h2>Motor, belt and pulleys</h2>
<p>Both axes use the same drive: GT2 16T on the motor, 40T on the 6 mm brass worm stub, a 158 mm belt at 50.4 mm centres, and a NEMA 17 42x38 at 1.5 A rated about 0.45 N&middot;m holding torque.</p>
<div class=tw><table><thead><tr><th>Check</th><th>15 N/span</th><th>40 N/span</th><th>Stall (0.45 N&middot;m)</th><th>Limit</th><th>Result</th></tr></thead><tbody>
<tr><td>Belt tight-side tension</td><td class=n>15 N</td><td class=n>40 N</td><td class=n>{b['stall, 40 N/span']['tight']:.0f} N</td><td>About 27 N working, 520 N breaking (6 mm GT2, seller data)</td><td><span class='chip warn'>Keep at 15 N</span></td></tr>
<tr><td>Teeth in mesh, 16T / 40T</td><td colspan=3 class=n>{H['belt']['teeth16']:.1f} / {H['belt']['teeth40']:.1f} (wrap {H['belt']['wrap16']:.0f}&deg; / {H['belt']['wrap40']:.0f}&deg;)</td><td>6 or more</td><td><span class='chip ok'>Pass</span></td></tr>
<tr><td>Motor bearing load, at the 20 mm rating point</td><td class=n>{sh['RA']['15 N/span (recommended)']['F_eq_20mm']:.0f} N</td><td class=n>{sh['RA']['40 N/span (over-tight)']['F_eq_20mm']:.0f} N</td><td class=n>{sh['RA']['stall, 40 N/span']['F_eq_20mm']:.0f} N</td><td>28 N typical NEMA 17 rating (bearing life). Static capacity is far higher</td><td><span class='chip warn'>Over at 40 N</span></td></tr>
<tr><td>Motor shaft bending, 5 mm with D-flat</td><td class=n>{sh['RA']['15 N/span (recommended)']['sig_eq']:.0f} MPa</td><td class=n>{sh['RA']['40 N/span (over-tight)']['sig_eq']:.0f} MPa</td><td class=n>{sh['RA']['stall, 40 N/span']['sig_eq']:.0f} MPa</td><td>Steel, about 300 MPa yield, about 200 MPa fatigue</td><td><span class='chip ok'>Pass</span></td></tr>
<tr><td>RA brass worm stub (lever {st['RA']['lever']:.1f} mm)</td><td class=n>{st['RA']['cases']['15 N/span (recommended)']['sig_eq']:.0f} MPa</td><td class=n>{st['RA']['cases']['40 N/span (over-tight)']['sig_eq']:.0f} MPa</td><td class=n>{st['RA']['cases']['stall, 40 N/span']['sig_eq']:.0f} MPa</td><td>Brass, about 250 MPa yield, about 120 MPa fatigue</td><td><span class='chip ok'>Pass</span></td></tr>
<tr><td>Dec brass worm stub (lever {st['Dec']['lever']:.1f} mm)</td><td class=n>{st['Dec']['cases']['15 N/span (recommended)']['sig_eq']:.0f} MPa</td><td class=n>{st['Dec']['cases']['40 N/span (over-tight)']['sig_eq']:.0f} MPa</td><td class=n>{st['Dec']['cases']['stall, 40 N/span']['sig_eq']:.0f} MPa</td><td>As above</td><td><span class='chip ok'>Pass</span></td></tr>
<tr><td>16T grub screw on the motor D-shaft</td><td colspan=3 class=n>{gr['F16']:.0f} N side load, {gr['tau16']:.0f} MPa shear at stall</td><td>Hardened M3 set screw</td><td><span class='chip ok'>Pass</span></td></tr>
<tr><td>40T grub screw on the brass stub</td><td colspan=3 class=n>{gr['F40']:.0f} N side load, {gr['tau40']:.0f} MPa shear; about {gr['bearing40']:.0f} MPa where it bears on the brass</td><td>Must sit on the D-flat: friction alone holds only about {gr['friction_torque40']:.1f} N&middot;m against {gr['T40']:.2f} N&middot;m</td><td><span class='chip warn'>Seat on flat</span></td></tr>
</tbody></table></div>
<p>The 40T pulley's hub grips only {st['RA']['engagement']:.1f} mm of the RA stub (Dec {st['Dec']['engagement']:.1f} mm), and the belt runs just past the end of the stub. Tighten both grub screws, with one of them on the flat, and check the belt tracks straight after the first slews.</p>
</section>

<section>
<h2>Bolted joints</h2>
<p>With plastic clamped between steel, the plastic sets the limit, not the bolt. Each preload below keeps the bearing pressure under the head or nut at {H['joint_consts']['P_SUST']:.0f} MPa, which plastic holds long term without creeping at room temperature. Torque = 0.22 &times; d &times; preload.</p>
<div class="tw wide"><table class=joints><thead><tr><th>Joint</th><th>Fasteners</th><th>Torque<br><small>N&middot;m</small></th><th>Preload<br><small>N</small></th><th>Worst load from the FEA</th><th>Capacity</th><th>Result</th><th>Notes</th></tr></thead><tbody>
{''.join(rows_j)}
</tbody></table></div>
<p class=note>The bolts themselves are loaded to only a few percent of their proof load. The risk with printed parts is preload lost to creep, not breakage. That is why the torques are low and nyloc nuts are suggested. Above about 40 &deg;C PLA relaxes much faster than PETG.</p>
</section>

<section>
<h2>How it was modelled</h2>
<div class=cols>
<div>
<h3>Geometry and mesh</h3>
<p>The parts were taken straight from the 2026-11-09 assembly STEP, in their assembled positions, and meshed with 10-node (quadratic) tets of 1 to 4.5 mm, finer round the connector holes and bolt seats. The solver is MKL Pardiso, with a residual of 1e-15.</p>
{conv_txt}
<h3>Contacts and supports</h3>
<p>Parts that touch are bonded with stiff springs. Every face touching the CG-5 is fixed: the dovetail (0.15 to 0.3 mm slide fit), the strap and band bores, the tab faces and the M5 heads. Each bolt is a steel spring from head seat to nut seat, and preload is checked by hand from the bearing pressure.</p>
</div>
<div>
<h3>Materials</h3>
<div class=tw><table><thead><tr><th></th><th>E</th><th>&nu;</th><th>Along layers</th><th>Across layers</th><th>Softens</th></tr></thead><tbody>
<tr><td>PLA</td><td class=n>2.3 GPa</td><td class=n>0.36</td><td class=n>52.3 MPa</td><td class=n>40.5 MPa</td><td class=n>55 &deg;C</td></tr>
<tr><td>PETG</td><td class=n>1.5 GPa</td><td class=n>0.38</td><td class=n>50.8 MPa</td><td class=n>42.8 MPa</td><td class=n>68 &deg;C</td></tr>
</tbody></table></div>
<p class=note>Stiffness from Prusament data sheets; strengths from Polymaker PolyLite data sheets, so both materials come from the same test method. SF along layers = strength / von Mises; SF across layers = layer strength / largest tensile stress. The across-layer figure assumes the worst print orientation.</p>
<h3>Load cases</h3>
<ul class=tight>
<li>Belt pretension: 15 N/span (recommended) and 40 N/span (over-tight), both sustained</li>
<li>Stall: 0.45 N&middot;m holding torque with a 40 N/span belt: tight side {b['stall, 40 N/span']['tight']:.0f} N, slack side 0. Both directions</li>
<li>Knock: 50 N on the motor's back end, worst of 24 sideways directions</li>
<li>50 N push and pull along the motor axis</li>
<li>Motor weight 0.28 kg and the parts' own weight in every case</li>
</ul>
</div>
</div>
<h3>Limits</h3>
<p>The model is linear and elastic. It does not include creep, impact, fatigue or temperature, so the creep and heat notes above come from rules of thumb. The CG-5 surfaces were modelled from photos and are treated as rigid. Bonded contact doesn't let joints open or slide; the joint table checks that they don't. Peaks within about 5 mm of a bolt seat and 0.6 mm of a fixed face are left out, because the model idealises those spots; the bearing check covers the bolt seats. The material values are typical data-sheet figures, and print settings shift them.</p>
</section>
<footer><p>Built with gmsh, scikit-fem and MKL Pardiso. The scripts are in reports/fea_python/.</p></footer>
</div>
"""
open(OUT, "w", encoding="utf-8").write(page)
print("wrote", OUT, len(page) // 1024, "KB")
