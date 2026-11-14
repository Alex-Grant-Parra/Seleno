import json, os, numpy as np
HERE = os.path.dirname(os.path.abspath(__file__))
SOLIDS = json.load(open(os.path.join(HERE, "solids_all.json")))
BYTAG = {r["tag"]: r for r in SOLIDS}

# STEP tag -> role, per group. "printed" are meshed in 3D; "contact" are the mount / hardware
# surfaces the printed parts bear on (fixed support); "aux" are only used for load patches.
GROUPS = {
    "RA": dict(
        printed={84: "Frame", 211: "Base_plate", 519: "RA_bracket", 534: "RA_strap_pz", 535: "RA_strap_mz"},
        contact={25: "ra_housing", 32: "ra_worm_carrier", 33: "ra_worm_shim", 34: "ra_carrier_M5_pz",
                 35: "ra_carrier_M5_mz", 38: "flatbit_M4_pan", 536: "M5x25_pz", 537: "M5x25_mz",
                 28: "ra_worm_nut_pz", 29: "ra_worm_shaft_pz", 31: "ra_worm_shaft_mz"},
        aux={513: "GX16_socket", 515: "GX12_1_socket", 517: "GX12_2_socket", 91: "DC_socket",
             491: "USB_socket", 85: "Button_Enter", 520: "RA_motor"},
    ),
    "DEC": dict(
        printed={540: "Dec_bracket", 541: "Dec_clamp_cap", 552: "Dec_band_cap_pz", 553: "Dec_band_cap_mz"},
        contact={45: "dec_housing", 51: "dec_worm_carrier", 550: "M5x25_dec_pz", 551: "M5x25_dec_mz",
                 48: "dec_worm_nut", 49: "dec_worm_shaft"},
        aux={542: "Dec_motor"},
    ),
}
ALL_KEEP = set()
for g in GROUPS.values():
    for k in ("printed", "contact", "aux"):
        ALL_KEEP |= set(g[k])


def match_volumes(gmsh):
    """Map volume tags of the re-imported subset BREP back to STEP tags (by volume + COM)."""
    out = {}
    cands = [BYTAG[t] for t in ALL_KEEP]
    for _, v in gmsh.model.getEntities(3):
        vol = gmsh.model.occ.getMass(3, v)
        com = np.array(gmsh.model.occ.getCenterOfMass(3, v))
        best = min(cands, key=lambda r: np.linalg.norm(np.array(r["com"]) - com) + abs(r["vol"] - vol) / max(vol, 1))
        d = np.linalg.norm(np.array(best["com"]) - com)
        if d < 0.05 and abs(best["vol"] - vol) / vol < 1e-3:
            out[v] = best["tag"]
    return out


# ---------------- materials ----------------
# E, nu from Prusament TDS (printed specimens); strengths from Polymaker PolyLite wiki TDS
# (same supplier + test method for both, so the comparison is like for like).
MATERIALS = {
    "PLA": dict(E=2300.0, nu=0.36, rho=1.24e-9, S_xy=52.3, S_z=40.5, HDT=55),
    "PETG": dict(E=1500.0, nu=0.38, rho=1.27e-9, S_xy=50.8, S_z=42.8, HDT=68),
}

# Bolts modelled as stiff springs between the head seat (part A) and the nut seat (part B).
# (label, bolt STEP tag, nut STEP tag, head-side part, nut-side part, nominal d)
BOLTS = {
    "DEC": [("Ring 1 M4x25 +Z", 554, 555, "Dec_clamp_cap", "Dec_bracket", 4),
            ("Ring 1 M4x16 -Z", 556, 557, "Dec_clamp_cap", "Dec_bracket", 4),
            ("Band 2 M4x25 +Z", 558, 559, "Dec_band_cap_pz", "Dec_bracket", 4),
            ("Band 2 M4x16 -Z", 560, 561, "Dec_band_cap_mz", "Dec_bracket", 4)],
    "RA": [("Bracket M3x30 #2", 528, 529, "RA_bracket", "Frame", 3),
           ("Bracket M3x30 #3", 530, 531, "RA_bracket", "Frame", 3),
           ("Bracket M3x30 #4", 532, 533, "RA_bracket", "Frame", 3),
           ("Strap top M4x20", 538, 539, "RA_strap_pz", "RA_strap_mz", 4),
           ("Base M3x10 L1", 493, 492, "Frame", "Base_plate", 3),
           ("Base M3x10 L2", 495, 494, "Frame", "Base_plate", 3),
           ("Base M3x10 R", 497, 496, "Frame", "Base_plate", 3),
           ("Base M3x10 B", 499, 498, "Frame", "Base_plate", 3)],
}
BOLT_AS = {2.5: 3.39, 3: 5.03, 4: 8.78, 5: 14.2}      # tensile stress area mm2
SEAT_ROUT = {2.5: 3.0, 3: 3.4, 4: 4.3, 5: 5.0}       # max seat radius searched (head / nut bearing)
