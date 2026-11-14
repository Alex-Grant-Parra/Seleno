# Hand checks: belt, pulleys, grub screws, motor shaft/bearing, brass worm stubs, every bolted joint,
# plus the PCB-vs-Pi relative movement at the J7 / GPIO header (from the FEA displacement field).
import numpy as np, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import HERE, BYTAG, MATERIALS
out = {}
T_HOLD = 0.45            # N*m
R16, R40 = 16 * 2 / (2 * np.pi) / 1000, 40 * 2 / (2 * np.pi) / 1000   # m
C = 0.05036; beta = np.arcsin((R40 - R16) / C)
Te = T_HOLD / R16
cases = {"15 N/span (recommended)": (15, 15), "40 N/span (over-tight)": (40, 40),
         "stall, 40 N/span": (max(40 + Te / 2, Te), max(40 - Te / 2, 0))}
belt = {}
for k, (t1, t2) in cases.items():
    hub = np.hypot((t1 + t2) * np.cos(beta), (t1 - t2) * np.sin(beta))
    belt[k] = dict(tight=t1, slack=t2, hub=hub)
out["belt"] = dict(cases=belt, Te=Te, wrap16=180 - 2 * np.degrees(beta), wrap40=180 + 2 * np.degrees(beta),
                   teeth16=16 * (180 - 2 * np.degrees(beta)) / 360, teeth40=40 * (180 + 2 * np.degrees(beta)) / 360,
                   working_6mm=27.0, break_6mm=520.0)

# motor shaft + bearing.  belt plane to flange: RA 11.5 mm, Dec 10.5 mm
shaft = {}
for ax, a in (("RA", 11.5), ("Dec", 10.5)):
    r = {}
    for k, b in belt.items():
        F = b["hub"]; M = F * a  # N*mm
        sig = 32 * M / (np.pi * 5 ** 3) * 1.3          # D-flat factor ~1.3
        tau = 16 * T_HOLD * 1000 / (np.pi * 5 ** 3) if "stall" in k else 0
        eq20 = F * (a + 25) / (20 + 25)                  # equivalent load at the 20 mm rating point (span ~25 mm)
        r[k] = dict(F=F, M=M, sigma=sig, tau=tau, sig_eq=np.hypot(sig, np.sqrt(3) * tau), F_eq_20mm=eq20)
    shaft[ax] = r
out["motor_shaft"] = dict(rating_N_at_20mm=28.0, steel_yield=300.0, endurance=200.0, axes=shaft)

# brass worm stub, 6 mm with D-flat. lever = belt plane to where the stub leaves the worm nut
stub = {}
for ax, L, eng in (("RA", 57.0 - 41.7, 52.8 - 45.5), ("Dec", 57.5 - 45.7, 56.8 - 46.0)):
    r = {}
    for k, b in belt.items():
        F = b["hub"]; M = F * L
        sig = 32 * M / (np.pi * 6 ** 3) * 1.3
        tau = 16 * (2.5 * T_HOLD * 1000) / (np.pi * 6 ** 3) if "stall" in k else 0
        r[k] = dict(F=F, M=M, sigma=sig, tau=tau, sig_eq=float(np.hypot(sig, np.sqrt(3) * tau)))
    stub[ax] = dict(lever=L, engagement=eng, cases=r)
out["worm_stub"] = dict(brass_yield=250.0, brass_fatigue=120.0, axes=stub)

# grub screws (M3 set screw, core dia 2.39 mm)
Acore = np.pi / 4 * 2.39 ** 2
g16 = T_HOLD * 1000 / 2.5; g40 = 2.5 * T_HOLD * 1000 / 3.0
Fa = 0.5 / (0.2 * 0.003)   # axial force from 0.5 N*m tightening
fric40 = 2 * 0.2 * Fa * 0.003
out["grub"] = dict(F16=g16, tau16=g16 / Acore, F40=g40, tau40=g40 / Acore, bearing40=g40 / (np.pi / 4 * 1.5 ** 2),
                   axial_from_0p5Nm=Fa, friction_torque40=fric40, T40=2.5 * T_HOLD)

# bolted joints: preload from 15 MPa sustained bearing on the smaller seat; torque K = 0.22
P_SUST = 15.0; K = 0.22; MU = 0.25
def area_head(dk, hole): return np.pi / 4 * (dk ** 2 - hole ** 2)
def area_nut(s, hole): return 0.866 * s ** 2 - np.pi / 4 * hole ** 2
def area_csk(): return np.pi / 4 * (4.7 ** 2 - 2.7 ** 2) / np.sin(np.radians(45))
J = []
def joint(name, n, d, Ah, An=None, Aw=None, into_metal=None, notes=""):
    A = min(Ah, An) if An else Ah
    FV = P_SUST * A
    FVw = P_SUST * min(Aw, An * 1.0 if An else Aw) if Aw else None
    As = {2: 2.07, 2.5: 3.39, 3: 5.03, 4: 8.78, 5: 14.2}[d]
    J.append(dict(name=name, n=n, d=d, A_head=Ah, A_nut=An, FV=FV, torque=K * d * FV / 1000,
                  FV_washer=FVw, torque_washer=(K * d * FVw / 1000) if FVw else None,
                  bolt_stress=FV / As, slip_per_bolt=MU * FV, into_metal=into_metal, notes=notes))
joint("Motor to bracket (RA + Dec)", 8, 3, area_head(5.5, 3.2), None, area_head(7.0, 3.2), "3.0 mm in motor aluminium",
      "M3x8; with a washer engagement drops to 2.5 mm")
joint("RA bracket to frame", 3, 3, area_head(5.5, 3.2), area_nut(5.5, 3.2), area_head(7.0, 3.2), None, "M3x30 + nut; nut side governs")
joint("RA strap top", 1, 4, area_head(7.0, 4.2), area_nut(7.0, 4.2), area_head(9.0, 4.2), None, "M4x20 + nut")
joint("RA flat-bit M5 (strap lugs / bracket tab)", 2, 5, area_head(8.5, 5.5), None, area_head(10.0, 5.5), "13 mm in CG-5 casting", "M5x25")
joint("Dec carrier M5 (bracket tab)", 2, 5, area_head(8.5, 5.5), None, area_head(10.0, 5.5), "CG-5 thread", "M5x25, head in 10 mm counterbore")
joint("Dec ring 1 + band 2 caps", 4, 4, area_head(7.0, 4.2), area_nut(7.0, 4.2), area_head(9.0, 4.2), None, "2x M4x25 + 2x M4x16 + nuts")
joint("Base plate to frame", 4, 3, area_head(5.6, 3.2), area_nut(5.5, 3.2), area_head(7.0, 3.2), None, "M3x10 pan + nut")
joint("PCB to frame hangers", 4, 2.5, area_head(5.0, 2.7), area_nut(5.0, 2.7), None, None, "M2.5x8 pan + nut")
joint("Pi to base", 4, 2.5, area_head(5.0, 2.7), area_nut(5.0, 2.7), None, None, "M2.5x8 pan + nut")
joint("USB panel socket", 2, 2.5, area_csk(), area_nut(5.0, 2.7), None, None, "M2.5x10 countersunk + nut on the socket flange")
out["joints"] = J
out["joint_consts"] = dict(P_SUST=P_SUST, K=K, MU=MU)

# J7 / GPIO header: PCB (frame hangers) vs Pi (base standoffs) relative movement
hang = [203, 205, 207, 209]; pi = [481, 483, 485, 487]
rel = {}
for mat in ("PLA", "PETG"):
    fn = os.path.join(HERE, f"res_RA_{mat}.npz")
    if not os.path.exists(fn): continue
    R = np.load(fn); names = [str(n) for n in R["names"]]; cs = [str(c) for c in R["cases"]]
    iF, iB = names.index("Frame"), names.index("Base_plate")
    XF, UF = R[f"p{iF}_xyz"], R[f"p{iF}_u"]; XB, UB = R[f"p{iB}_xyz"], R[f"p{iB}_u"]
    def u_at(X, U, tags):
        idx = [np.argsort(np.linalg.norm(X - np.array(BYTAG[t]["com"]), axis=1))[:8] for t in tags]
        return np.stack([U[:, i, :].mean(1) for i in idx], 1).mean(1)  # (ncase,3) mean of the 4 points
    du = u_at(XF, UF, hang) - u_at(XB, UB, pi)
    res = {}
    for conn, mag in (("GX16_socket", 50), ("GX12_1_socket", 50), ("GX12_2_socket", 50), ("DC_socket", 20), ("USB_socket", 20)):
        cols = [cs.index(f"{conn}:F{c}") for c in "xyz"]
        best = 0
        for th in np.linspace(0, np.pi, 13):
            for ph in np.linspace(0, 2 * np.pi, 24, endpoint=False):
                d = np.array([np.sin(th) * np.cos(ph), np.sin(th) * np.sin(ph), np.cos(th)])
                best = max(best, np.linalg.norm(sum(mag * d[k] * du[cols[k]] for k in range(3))))
        res[conn] = best
    res["self_weight_1g"] = float(np.linalg.norm(du[cs.index("self_weight_1g")]))
    rel[mat] = res
out["j7_relative_mm"] = rel
json.dump(out, open(os.path.join(HERE, "hand.json"), "w"), indent=1, default=float)
print(json.dumps(out, indent=1, default=lambda x: round(float(x), 3))[:6000])
