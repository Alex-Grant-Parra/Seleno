"""Compare the three planetary models with JPL DE440 and store the results.

    python scripts/model_accuracy.py           # recompute every sample
    python scripts/model_accuracy.py --print   # print the comparison, don't write

Writes ModelAccuracySample rows (models/model_accuracy.py) into instance/Data.db,
replacing the old ones; /about/Astrophysics shows them. Takes a few seconds once
the Helios checkpoint cache is built.

Every position is geocentric, geometric (no light time or aberration), J2000
equator and equinox. DE440 is sampled at the same instant Helios uses for the
civil time (its own Delta T, timescales.utcToEt), so Helios' error is purely its
physics. V1 and V2 use civil time directly with no Delta T, which is part of
their error. Mars, Jupiter and Neptune are DE440's system barycentres, as in
Helios (under 0.1" from the planet centres as seen from Earth).
"""
import argparse
import contextlib
import io
import math
import os
import sys
from datetime import datetime, timezone

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BASE_DIR)

import numpy as np
import spiceypy as spice

KERNEL_DIR = os.path.join(BASE_DIR, "astrophysics", "V3_Helios", "data")

SAMPLES = [
    datetime(1600, 1, 1, tzinfo=timezone.utc),
    datetime(1900, 1, 1, tzinfo=timezone.utc),
    datetime(2026, 11, 16, tzinfo=timezone.utc),
    datetime(2100, 1, 1, tzinfo=timezone.utc),
    datetime(2500, 1, 1, tzinfo=timezone.utc),
]

# Body -> DE440 NAIF ID, in table order
BODIES = {"mercury": 199, "mars": 4, "jupiter": 5, "neptune": 8, "moon": 301}


def _unit(raDeg, decDeg):
    ra, dec = math.radians(raDeg), math.radians(decDeg)
    return np.array([math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra), math.sin(dec)])


def separationDeg(a, b):
    """Angle on the sky between two (RA, Dec) pairs, stable at small angles."""
    ua, ub = _unit(*a), _unit(*b)
    return math.degrees(math.atan2(np.linalg.norm(np.cross(ua, ub)), float(ua @ ub)))


def de440RaDec(naifId, et):
    pos, _ = spice.spkpos(str(naifId), et, "J2000", "NONE", "399")
    _, ra, dec = spice.recrad(pos)
    return math.degrees(ra), math.degrees(dec)


def compute():
    from astrophysics.V1_Keplarian import astroTools as v1
    from astrophysics.V1_Keplarian.convert import convert as v1convert
    from astrophysics.V2_VSOP87A.ephemeris import get_positions as v2Positions
    from astrophysics.V3_Helios.convert import getRaDecAtTime as v3Positions
    from astrophysics.V3_Helios.timescales import utcToEt

    spice.kclear()
    spice.furnsh(os.path.join(KERNEL_DIR, "naif0012.tls"))
    spice.furnsh(os.path.join(KERNEL_DIR, "de440.bsp"))

    def v1RaDec(body, when):
        # The Keplerian functions themselves: V1's getAllCelestialData delegates planets to VSOP87
        if body == "moon":
            ra, dec = v1.findMoon(when.year, when.month, when.day, when.hour, when.minute, when.second)
        else:
            ra, dec = v1.findPlanet(when.year, when.month, when.day, body, when.hour, when.minute, when.second)
        return v1convert.HrMinSecToDegrees(*ra) * 15.0, v1convert.HrMinSecToDegrees(*dec)

    rows = []
    for when in SAMPLES:
        et = utcToEt(when)
        v2 = v2Positions(when.year, when.month, when.day, when.hour, when.minute, when.second)
        v3 = v3Positions(when)
        for order, (body, naifId) in enumerate(BODIES.items()):
            ref = de440RaDec(naifId, et)
            row = {"sample_utc": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "body": body, "sort_order": order,
                   "de440_ra": ref[0], "de440_dec": ref[1]}
            models = {
                "v1": v1RaDec(body, when),
                "v2": (v2[body]["ra_deg"], v2[body]["dec_deg"]) if body in v2 else None,
                "v3": (v3[body]["ra_deg"], v3[body]["dec_deg"]),
            }
            for key, pos in models.items():
                row[f"{key}_ra"] = pos[0] if pos else None
                row[f"{key}_dec"] = pos[1] if pos else None
                row[f"{key}_err"] = separationDeg(ref, pos) if pos else None
            rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--print", action="store_true", help="print the comparison without writing it")
    args = parser.parse_args()

    with contextlib.redirect_stdout(io.StringIO()):   # Server prints its start-up log
        from Server import app
    from app.db import db
    from models.model_accuracy import ModelAccuracySample

    with app.app_context():
        rows = compute()   # V1 reads its orbital elements from the database
        for r in rows:
            errs = "  ".join(f"{k} {r[k + '_err'] * 3600:10.2f}\"" if r[k + "_err"] is not None else f"{k} {'-':>11}"
                             for k in ("v1", "v2", "v3"))
            print(f"{r['sample_utc'][:10]}  {r['body']:8}  {errs}")
        if args.print:
            return
        db.create_all()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ModelAccuracySample.query.delete()
        db.session.add_all(ModelAccuracySample(generated_at=now, **r) for r in rows)
        db.session.commit()
        print(f"Wrote {len(rows)} rows to model_accuracy_samples")


if __name__ == "__main__":
    main()
