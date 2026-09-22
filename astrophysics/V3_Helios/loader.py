"""
Write initial_conditions.json for V3 Helios from the JPL DE440 ephemeris.

SPICE is only used here, to sample the state (position + velocity) of each
body at one epoch and the DE440 gravitational parameters (GM).  Everything
after that is integrated by engine.py.

    python astrophysics/V3_Helios/loader.py                       # epoch = now
    python astrophysics/V3_Helios/loader.py --epoch 2026-01-01T00:00:00

Required kernels are downloaded from NAIF into data/ if they are missing.
"""

import argparse
import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import spiceypy as spice

base = Path(__file__).resolve().parent
dataPath = base / "data"
outputPath = base / "initial_conditions.json"

_NAIF = "https://naif.jpl.nasa.gov/pub/naif/generic_kernels"
# DE440 alone covers every body below (1550-2650); the satellite SPKs are not
# needed because planets with large moons are taken at their system barycentre.
KERNELS = {
    "naif0012.tls": f"{_NAIF}/lsk/naif0012.tls",
    "de440.bsp": f"{_NAIF}/spk/planets/de440.bsp",
    "gm_de440.tpc": f"{_NAIF}/pck/gm_de440.tpc",
}

# Body -> NAIF ID.  Mars..Pluto use the planet-system barycentre (4..9): a
# planet's centre wobbles around that barycentre because of its moons (Pluto by
# ~24 m/s from Charon), and that wobble would otherwise be integrated as if it
# were orbital motion.  The barycentre is <0.1" from the planet as seen from Earth.
bodies = {
    "sun": 10,
    "mercury": 199,
    "venus": 299,
    "earth": 399,
    "moon": 301,
    "mars": 4,
    "jupiter": 5,
    "saturn": 6,
    "uranus": 7,
    "neptune": 8,
    "pluto": 9,
}

frame = "ECLIPJ2000"

# SPICE returns kilometers and km/s; convert to meters for storage.
metersPerKilometer = 1000.0


def ensureKernels():
    """Download any missing kernel from NAIF; return the local paths."""
    dataPath.mkdir(exist_ok=True)
    paths = []
    for name, url in KERNELS.items():
        path = dataPath / name
        if not path.exists():
            print(f"Downloading {name} from {url} …")
            partial = path.with_suffix(path.suffix + ".part")
            urllib.request.urlretrieve(url, partial)
            partial.rename(path)
        paths.append(path)
    return paths


def writeInitialConditions(utcStr):
    spice.kclear()
    for kernelPath in ensureKernels():
        spice.furnsh(str(kernelPath))

    et = spice.utc2et(utcStr)
    print("Epoch UTC:", utcStr, " ET:", et)

    state = {
        "epoch_et": et,
        "epoch_utc": utcStr,
        "frame": frame,
        "source": "DE440 (states + GM)",
        "bodies": {}
    }

    for name, naifId in bodies.items():
        stateVec, _ = spice.spkezr(str(naifId), et, frame, "NONE", "SOLAR SYSTEM BARYCENTER")
        gmKm3 = spice.bodvcd(naifId, "GM", 1)[1][0]

        state["bodies"][name] = {
            "naif_id": naifId,
            "position_m": [component * metersPerKilometer for component in stateVec[:3]],
            "velocity_m_s": [component * metersPerKilometer for component in stateVec[3:6]],
            "gm_m3_s2": gmKm3 * metersPerKilometer ** 3,
        }

    with open(outputPath, "w") as f:
        json.dump(state, f, indent=2)
    print("Saved:", outputPath)

    # Release loaded kernels before exiting.
    spice.kclear()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--epoch", help="UTC epoch, ISO 8601 (default: now)")
    args = parser.parse_args()

    if args.epoch:
        epoch = datetime.fromisoformat(args.epoch.replace("Z", "+00:00"))
        epoch = epoch.astimezone(timezone.utc) if epoch.tzinfo else epoch
    else:
        epoch = datetime.now(timezone.utc)
    writeInitialConditions(epoch.strftime("%Y-%m-%dT%H:%M:%S.%f"))


if __name__ == "__main__":
    main()
