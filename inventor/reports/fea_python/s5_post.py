# Build the real load cases from the unit cases and summarise stresses, deflections and joint forces.
# Usage: s5_post.py GROUP MAT      -> post_GROUP_MAT.json
import numpy as np, sys, os, json
from scipy.spatial import cKDTree
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HERE, BYTAG, MATERIALS

G, MAT = sys.argv[1], sys.argv[2]
mat = MATERIALS[MAT]
MT = os.environ.get("MESHTAG", "")
R = np.load(os.path.join(HERE, f"res_{G}_{MAT}{MT}.npz"))
J = json.load(open(os.path.join(HERE, f"res_{G}_{MAT}{MT}.json")))
cases = [str(c) for c in R["cases"]]; NC = len(cases)
names = [str(n) for n in R["names"]]
cidx = {c: i for i, c in enumerate(cases)}

# ---------------- drive-train constants ----------------
T_HOLD = 450.0                 # N*mm, NEMA17 42x38 1.5 A holding torque (42-45 N*cm class)
R16, R40 = 16 * 2 / (2 * np.pi), 40 * 2 / (2 * np.pi)   # GT2 pitch radii, mm
W_MOTOR = 0.28 * 9.81          # N
T0_OVER, T0_OK = 40.0, 15.0    # belt pretension per span, N
KNOCK, PULL = 50.0, 50.0       # N on the motor
SNAG_GX, SNAG_SMALL, BUTTON = 50.0, 20.0, 20.0
if G == "RA":
    scr = np.array([BYTAG[t]["com"] for t in (524, 525, 526, 527)]); mot = BYTAG[520]; pul40 = BYTAG[521]
    zb, zback, patch = 57.0, 106.5, "RA_motor"
else:
    scr = np.array([BYTAG[t]["com"] for t in (546, 547, 548, 549)]); mot = BYTAG[542]; pul40 = BYTAG[543]
    zb, zback, patch = 57.5, 106.0, "Dec_motor"
P1 = scr[:, :2].mean(0); P2 = np.array(pul40["com"][:2])
xref = np.array(J["patches"][patch]["ref"])
C = np.linalg.norm(P2 - P1); e = (P2 - P1) / C; n = np.array([-e[1], e[0]])
beta = np.arcsin((R40 - R16) / C)


def remote(F, x):
    """(F at point x) -> 6 coefficients of the motor patch unit cases"""
    F = np.asarray(F, float); x = np.asarray(x, float)
    Mr = np.cross(x - xref, F)
    co = np.zeros(NC)
    for k, comp in enumerate("Fx Fy Fz Mx My Mz".split()):
        co[cidx[f"{patch}:{comp}"]] = (F[k] if k < 3 else Mr[k - 3])
    return co


def belt(Tp, Tm):
    co = np.zeros(NC); Fh = np.zeros(3)
    for s, T in ((1, Tp), (-1, Tm)):
        rho = s * n * np.cos(beta) - e * np.sin(beta)
        d = e * np.cos(beta) + s * n * np.sin(beta)
        tp = np.array([*(P1 + R16 * rho), zb]); f = T * np.array([d[0], d[1], 0.0])
        co += remote(f, tp); Fh += f
    return co, Fh


def weight():
    co = np.zeros(NC); co[cidx["self_weight_1g"]] = 1.0
    return co + remote([0, -W_MOTOR, 0], mot["com"])


Te = T_HOLD / R16
tight = max(T0_OVER + Te / 2, Te); slack = max(T0_OVER - Te / 2, 0.0)
LC = {}  # name -> list of coefficient vectors (envelope over the list) + meta
b_over, F_over = belt(T0_OVER, T0_OVER); b_ok, F_ok = belt(T0_OK, T0_OK)
LC["LC1 belt over-tight 40 N/span (sustained)"] = dict(co=[b_over + weight()], kind="sustained", hub=float(np.linalg.norm(F_over)), tight=T0_OVER)
LC["LC1r belt 15 N/span (sustained)"] = dict(co=[b_ok + weight()], kind="sustained", hub=float(np.linalg.norm(F_ok)), tight=T0_OK)
b1, F1 = belt(tight, slack); b2, F2 = belt(slack, tight)
LC["LC2 motor stall 0.45 N*m, both senses"] = dict(co=[b1 + weight(), b2 + weight()], kind="peak", hub=float(np.linalg.norm(F1)), tight=tight)
axis_pt = np.array([P1[0], P1[1], zback])
dirs = [np.array([np.cos(a), np.sin(a), 0.0]) for a in np.linspace(0, 2 * np.pi, 24, endpoint=False)]
LC["LC3 50 N knock on motor end, any side"] = dict(co=[b_ok + weight() + remote(KNOCK * d, axis_pt) for d in dirs], kind="peak", hub=float(np.linalg.norm(F_ok)), tight=T0_OK)
LC["LC3z 50 N pull/push on motor along its axis"] = dict(co=[b_ok + weight() + remote([0, 0, s * PULL], axis_pt) for s in (1, -1)], kind="peak", hub=float(np.linalg.norm(F_ok)), tight=T0_OK)
if G == "RA":
    sph = []
    for th in np.linspace(0, np.pi, 7):
        for ph in np.linspace(0, 2 * np.pi, max(1, int(round(12 * np.sin(th)))), endpoint=False):
            sph.append(np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)]))
    sph = np.unique(np.round(sph, 6), axis=0)
    def conn(pn, Fmag, twist=0.0):
        out = []
        for d in sph:
            co = np.zeros(NC); co[cidx["self_weight_1g"]] = 1.0
            for k, comp in enumerate("Fx Fy Fz".split()):
                co[cidx[f"{pn}:{comp}"]] = Fmag * d[k]
            if twist:
                co[cidx[f"{pn}:Mz"]] = twist
            out.append(co)
        return out
    def conn_dirs(pn, Fmag, dirs_):
        out = []
        for d in dirs_:
            co = np.zeros(NC); co[cidx["self_weight_1g"]] = 1.0
            for k, comp in enumerate("Fx Fy Fz".split()):
                co[cidx[f"{pn}:{comp}"]] = Fmag * d[k]
            out.append(co)
        return out
    lat = [np.array([np.cos(a), np.sin(a), 0.0]) for a in np.linspace(0, 2 * np.pi, 24, endpoint=False)]
    for tagn, pn in (("GX16", "GX16_socket"), ("GX12 #1", "GX12_1_socket"), ("GX12 #2", "GX12_2_socket")):
        LC[f"LC4p {tagn} straight pull-out 50 N"] = dict(co=conn_dirs(pn, SNAG_GX, [np.array([0, 0, -1.0])]), kind="peak", conn=pn)
        LC[f"LC4s {tagn} sideways snag 50 N"] = dict(co=conn_dirs(pn, SNAG_GX, lat), kind="peak", conn=pn)
    LC["LC5p DC plug straight pull-out 20 N"] = dict(co=conn_dirs("DC_socket", SNAG_SMALL, [np.array([0, 0, -1.0])]), kind="peak", conn="DC_socket")
    LC["LC5s DC plug sideways 20 N"] = dict(co=conn_dirs("DC_socket", SNAG_SMALL, lat), kind="peak", conn="DC_socket")
    LC["LC4a GX16 cable snag 50 N any direction"] = dict(co=conn("GX16_socket", SNAG_GX), kind="peak")
    LC["LC4b GX12 #1 cable snag 50 N any direction"] = dict(co=conn("GX12_1_socket", SNAG_GX), kind="peak")
    LC["LC4c GX12 #2 cable snag 50 N any direction"] = dict(co=conn("GX12_2_socket", SNAG_GX), kind="peak")
    LC["LC5 DC plug 20 N any direction + 0.3 N*m twist"] = dict(co=conn("DC_socket", SNAG_SMALL, 300.0), kind="peak")
    LC["LC6 USB plug 20 N any direction"] = dict(co=conn("USB_socket", SNAG_SMALL), kind="peak")
    co = np.zeros(NC); co[cidx["self_weight_1g"]] = 1.0; co[cidx["Button_Enter:Fz"]] = -BUTTON
    LC["LC7 button press 20 N"] = dict(co=[co], kind="peak")

# ---------------- bolt preload ----------------
P_SUST = 15.0  # MPa, sustained bearing pressure on printed plastic (about 30 % of strength, <= 30 C)
def seat_areas(label, d):
    hole = {2.5: 2.7, 3: 3.2, 4: 4.2, 5: 5.5}[d]
    dk = {2.5: 5.0 if "pan" in label or "x8" in label or "x10" in label and d == 3 else 4.5, 3: 5.5, 4: 7.0, 5: 8.5}[d]
    if d == 3 and "Base" in label: dk = 5.6  # ISO 7045 pan head
    s_nut = {2.5: 5.0, 3: 5.5, 4: 7.0, 5: 8.0}[d]
    Ah = np.pi / 4 * (dk ** 2 - hole ** 2); An = 0.866 * s_nut ** 2 - np.pi / 4 * hole ** 2
    return Ah, An
PRELOAD = {}
if "bolt_F" in R.files:
    for bi, bl in enumerate(R["bolt_label"]):
        bl = str(bl); d = float(R["bolt_d"][bi]); d = int(d) if d.is_integer() else d
        Ah, An = seat_areas(bl, d)
        FV = P_SUST * min(Ah, An)
        ci = cidx[f"preload:{bl}"]
        tu = float(R["bolt_F"][bi, :, ci] @ R["bolt_axis"][bi])
        PRELOAD[bl] = dict(FV=FV, Ah=Ah, An=An, scale=FV / tu, unitT=tu,
                           torque_Nm=0.22 * d * FV / 1000)
    co_lp = np.zeros(NC)
    for bl, p in PRELOAD.items():
        co_lp[cidx[f"preload:{bl}"]] = p["scale"]
    LC["LP all bolts at recommended preload (sustained)"] = dict(co=[co_lp + (np.eye(NC)[cidx["self_weight_1g"]])], kind="sustained")
    for k0 in list(LC):
        if k0.startswith(("LC2", "LC3 ")):
            LC["LP + " + k0] = dict(co=[c + co_lp for c in LC[k0]["co"]], kind="peak",
                                    **{k: LC[k0][k] for k in ("hub", "tight") if k in LC[k0]})
summary_preload = PRELOAD

# ---------------- evaluate ----------------
def vm_p1(sig):  # sig (..., 6) xx yy zz xy yz xz
    sx, sy, sz, txy, tyz, txz = [sig[..., i] for i in range(6)]
    vm = np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (txy ** 2 + tyz ** 2 + txz ** 2))
    Tm = np.stack([np.stack([sx, txy, txz], -1), np.stack([txy, sy, tyz], -1), np.stack([txz, tyz, sz], -1)], -2)
    p1 = np.linalg.eigvalsh(Tm)[..., -1]
    return vm, p1


fix_x, fix_lab, fix_pid = R["fix_x"], R["fix_lab"], R["fix_pid"]
cnames = [str(c) for c in R["contact_names"]]
summary = dict(group=G, mat=MAT, cases={}, drive=dict(C=C, beta_deg=float(np.degrees(beta)), Te=Te, tight=tight, slack=slack),
               preload=summary_preload)
# named features for locating peaks
FEAT = []
if "bolt_F" in R.files:
    for bi, bl in enumerate(R["bolt_label"]):
        FEAT.append((f"{bl} head seat", R["bolt_xA"][bi])); FEAT.append((f"{bl} nut seat", R["bolt_xB"][bi]))
for pn_, pdict in J["patches"].items():
    FEAT.append((f"{pn_} mounting", np.array(pdict["centroid"])))
for lab in np.unique(fix_lab):
    m = fix_lab == lab
    FEAT.append((f"{cnames[lab]} contact", None, fix_x[m]))
def where(x):
    best, bd = "", 1e9
    for f in FEAT:
        if f[1] is not None:
            d_ = np.linalg.norm(np.asarray(f[1]) - x)
        else:
            d_ = np.min(np.linalg.norm(f[2] - x, axis=1))
        if d_ < bd: best, bd = f[0], d_
    return f"{bd:.0f} mm from {best}"
envelope = {pid: dict(vm=0, p1=0) for pid in range(len(names))}
env_nodes = {}
for lname, L in LC.items():
    CO = np.array(L["co"])  # (ndir, NC)
    res = dict(kind=L["kind"], parts={}, bolts={}, supports={}, ties={})
    for k in ("hub", "tight"):
        if k in L: res[k] = L[k]
    for pid, pn in enumerate(names):
        xyzp = R[f"p{pid}_xyz"]; sig = R[f"p{pid}_sig"]; U = R[f"p{pid}_u"]
        # exclude nodes within 0.6 mm of a fixed support (support-edge singularities)
        fx = fix_x[fix_pid == pid]
        excl = np.zeros(len(xyzp), bool)
        if len(fx):
            excl = cKDTree(fx).query(xyzp, distance_upper_bound=0.6)[0] < 0.6
        if "bolt_F" in R.files:  # bolt seats: bearing checked by hand (connector loads a few seat nodes)
            for bi in range(len(R["bolt_label"])):
                rz = {2.5: 4.0, 3.0: 4.5, 4.0: 5.5}[float(R["bolt_d"][bi])]
                for xs in (R["bolt_xA"][bi], R["bolt_xB"][bi]):
                    excl |= np.linalg.norm(xyzp - xs, axis=1) < rz
        s = np.tensordot(CO, sig, axes=(1, 0))  # (ndir, nv, 6)
        vm, p1 = vm_p1(s)
        vmx = vm.max(0); p1x = p1.max(0)
        u = np.linalg.norm(np.tensordot(CO, U, axes=(1, 0)), axis=2).max(0)
        ok = ~excl
        i_vm = int(np.argmax(np.where(ok, vmx, -1))); i_p1 = int(np.argmax(np.where(ok, p1x, -1e9)))
        res["parts"][pn] = dict(vm_max=float(vmx[i_vm]), vm_at=xyzp[i_vm].round(1).tolist(),
                                p1_max=float(p1x[i_p1]), p1_at=xyzp[i_p1].round(1).tolist(),
                                vm_p99_9=float(np.percentile(vmx[ok], 99.9)), vm_all=float(vmx.max()),
                                u_max=float(u.max()), u_at=xyzp[int(np.argmax(u))].round(1).tolist(),
                                SF_xy=float(mat["S_xy"] / max(vmx[i_vm], 1e-9)), SF_z=float(mat["S_z"] / max(p1x[i_p1], 1e-9)),
                                vm_where=where(xyzp[i_vm]), p1_where=where(xyzp[i_p1]))
        env_nodes.setdefault(pid, []).append(vmx)
    # motor patch centroid displacement (pulley shift) for drive cases
    if "hub" in L:
        pidm = names.index("RA_bracket" if G == "RA" else "Dec_bracket")
        xyzp = R[f"p{pidm}_xyz"]; U = R[f"p{pidm}_u"]
        c0 = np.array(J["patches"][patch]["centroid"])
        near = np.argsort(np.linalg.norm(xyzp - c0, axis=1))[:30]
        du = np.tensordot(CO, U[:, near, :].mean(1), axes=(1, 0))  # (ndir,3)
        res["motor_face_disp_mm"] = float(np.linalg.norm(du, axis=1).max())
        res["motor_face_disp_along_belt_mm"] = float(np.abs(du[:, 0] * e[0] + du[:, 1] * e[1]).max())
    # bolts
    if "bolt_F" in R.files:
        BF = np.tensordot(CO, R["bolt_F"], axes=(1, 2))  # (ndir, nbolt, 3)
        for bi, bl in enumerate(R["bolt_label"]):
            a = R["bolt_axis"][bi]
            t = BF[:, bi, :] @ a; sh = np.linalg.norm(BF[:, bi, :] - np.outer(t, a), axis=1)
            res["bolts"][str(bl)] = dict(tension=float(t.max()), compression=float(-t.min()), shear=float(sh.max()),
                                         tension_min=float(t.min()))
    # supports: per (part, contact)
    FR = np.tensordot(CO, R["fix_R"], axes=(1, 2))  # (ndir, nfix, 3)
    for pid in np.unique(fix_pid):
        for lab in np.unique(fix_lab[fix_pid == pid]):
            m = (fix_pid == pid) & (fix_lab == lab)
            Rs = FR[:, m, :]
            tens = np.maximum(0, (Rs * R["fix_n"][m][None]).sum(2)).sum(1)
            tot = Rs.sum(1)
            res["supports"][f"{names[pid]} | {cnames[lab]}"] = dict(resultant=float(np.linalg.norm(tot, axis=1).max()),
                                                                   tension_sum=float(tens.max()), nodes=int(m.sum()))
    # ties
    if "tie_F" in R.files:
        TF = np.tensordot(CO, R["tie_F"], axes=(1, 2))  # (ndir, ntie, 3)
        tn = R["tie_n"]
        for A_ in np.unique(R["tie_A"]):
            for B_ in np.unique(R["tie_B"][R["tie_A"] == A_]):
                m = (R["tie_A"] == A_) & (R["tie_B"] == B_)
                f = TF[:, m, :]
                tens = np.maximum(0, (f * tn[m][None]).sum(2)).sum(1)
                res["ties"][f"{names[A_]} - {names[B_]}"] = dict(resultant=float(np.linalg.norm(f.sum(1), axis=1).max()),
                                                                tension_sum=float(tens.max()), nodes=int(m.sum()))
    if "bolt_F" in R.files:
        res["joints"] = {}
        pairs = {}
        for bi, bl in enumerate(R["bolt_label"]):
            pass
        from common import BOLTS
        for (bl, _, _, pa_n, pb_n, dn) in BOLTS[G]:
            pairs.setdefault((pa_n, pb_n), []).append(bl)
        for (X, Y), bls in pairs.items():
            iX, iY = names.index(X), names.index(Y)
            Fj = np.zeros((CO.shape[0], 3))
            if "tie_F" in R.files:
                TFc = np.tensordot(CO, R["tie_F"], axes=(1, 2))
                mXY = (R["tie_A"] == iX) & (R["tie_B"] == iY); mYX = (R["tie_A"] == iY) & (R["tie_B"] == iX)
                Fj += TFc[:, mXY, :].sum(1) - TFc[:, mYX, :].sum(1)
            bl_list = [str(b) for b in R["bolt_label"]]
            a = None
            for bl in bls:
                bi = bl_list.index(bl); a = R["bolt_axis"][bi]
                Fj += -BF[:, bi, :]  # spring force on the head-side part X
            a = a / np.linalg.norm(a)  # from nut side (Y) to head side (X)
            ten = -(Fj @ a); sh = np.linalg.norm(Fj - np.outer(Fj @ a, a), axis=1)
            res["joints"][f"{X} - {Y}"] = dict(bolts=bls, tension=float(ten.max()), shear=float(sh.max()))
    summary["cases"][lname] = res
# envelopes for plotting
np.savez_compressed(os.path.join(HERE, f"env_{G}_{MAT}{MT}.npz"),
                    **{f"p{pid}_vm_{i}": v for pid, lst in env_nodes.items() for i, v in enumerate(lst)},
                    lcnames=np.array(list(LC.keys())))
json.dump(summary, open(os.path.join(HERE, f"post_{G}_{MAT}{MT}.json"), "w"), indent=1)

# ---------------- print ----------------
for bl, p in PRELOAD.items():
    print(f"preload {bl:18s} FV {p['FV']:5.0f} N  (head {p['Ah']:.1f} / nut {p['An']:.1f} mm2)  torque {p['torque_Nm']:.2f} N*m")
print(f"== {G} {MAT}  belt C={C:.2f} beta={np.degrees(beta):.1f} deg  Te={Te:.1f} N  stall tight={tight:.1f} slack={slack:.1f}")
for lname, res in summary["cases"].items():
    extra = f" hub {res['hub']:.0f} N" if "hub" in res else ""
    extra += f" | motor face moves {res['motor_face_disp_mm']:.3f} mm" if "motor_face_disp_mm" in res else ""
    print(f"\n{lname}{extra}")
    for pn, r in res["parts"].items():
        print(f"   {pn:16s} vm {r['vm_max']:6.2f} (all {r['vm_all']:6.2f}, p99.9 {r['vm_p99_9']:5.2f}) MPa at {r['vm_at']}  p1 {r['p1_max']:6.2f}  "
              f"SFxy {r['SF_xy']:6.1f} SFz {r['SF_z']:6.1f}  u {r['u_max']:.3f} mm  [{r['vm_where']}]")
    for bl, b in res["bolts"].items():
        print(f"   bolt {bl:18s} T {b['tension']:6.1f}  C {b['compression']:6.1f}  V {b['shear']:6.1f} N")
    for k, s in res["supports"].items():
        print(f"   supp {k:40s} |R| {s['resultant']:6.1f}  tension {s['tension_sum']:6.1f} N  ({s['nodes']} nodes)")
    for k, s in res["ties"].items():
        print(f"   tie  {k:40s} |F| {s['resultant']:6.1f}  tension {s['tension_sum']:6.1f} N  ({s['nodes']} nodes)")
    for k, s in res.get("joints", {}).items():
        print(f"   JOINT {k:38s} tension {s['tension']:6.1f}  shear {s['shear']:6.1f} N")
