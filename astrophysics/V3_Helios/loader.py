"""
Write initial_conditions.json for V3 Helios from the JPL DE440 ephemeris.

SPICE is only used here, to sample the state (position + velocity) of each
body at one epoch and the DE440 gravitational parameters (GM).  Everything
after that is integrated by engine.py.

    python astrophysics/V3_Helios/loader.py                       # epoch = now
    python astrophysics/V3_Helios/loader.py --epoch 2026-01-01T00:00:00
    python astrophysics/V3_Helios/loader.py --small-bodies          # add/refresh small_bodies.py

Required kernels are downloaded from NAIF into data/ if they are missing.

--small-bodies adds the dwarf planets and asteroids of small_bodies.py to the
existing file, at its epoch, leaving every other body's entry untouched.
Their states come from JPL Horizons (barycentric, ecliptic and equinox of
J2000, which is SPICE's ECLIPJ2000) and their H and G magnitudes from JPL's
small-body database; both need internet.
"""

import argparse
import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import spiceypy as spice

try:
    from .constants import METERS_PER_KM, SECONDS_PER_DAY, JD_J2000
    from .small_bodies import SMALL_BODIES, DEFAULT_G
except ImportError:
    from constants import METERS_PER_KM, SECONDS_PER_DAY, JD_J2000
    from small_bodies import SMALL_BODIES, DEFAULT_G

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

        # SPICE returns kilometers and km/s; convert to meters for storage.
        state["bodies"][name] = {
            "naif_id": naifId,
            "position_m": [component * METERS_PER_KM for component in stateVec[:3]],
            "velocity_m_s": [component * METERS_PER_KM for component in stateVec[3:6]],
            "gm_m3_s2": gmKm3 * METERS_PER_KM ** 3,
        }

    with open(outputPath, "w") as f:
        json.dump(state, f, indent=2)
    print("Saved:", outputPath)

    # Release loaded kernels before exiting.
    spice.kclear()

    # A new epoch needs the small bodies' states at it too
    addSmallBodies(outputPath)


# ---------------------------------------------------------------------------
# Small bodies, from JPL Horizons and the small-body database
# ---------------------------------------------------------------------------

HORIZONS_API = "https://ssd.jpl.nasa.gov/api/horizons.api"
SBDB_API = "https://ssd-api.jpl.nasa.gov/sbdb.api"


def _getJson(url, params):
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": "helios-loader"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def horizonsState(record, et):
    """(position m, velocity m/s) of a Horizons record ("1;" = asteroid 1, "399" =
    the Earth) at TDB seconds past J2000: barycentric, ECLIPJ2000."""
    jdTdb = JD_J2000 + et / SECONDS_PER_DAY
    result = _getJson(HORIZONS_API, {
        "format": "json", "COMMAND": f"'{record}'", "OBJ_DATA": "'NO'", "MAKE_EPHEM": "'YES'",
        "EPHEM_TYPE": "'VECTORS'", "CENTER": "'500@0'", "REF_PLANE": "'ECLIPTIC'",
        "REF_SYSTEM": "'ICRF'", "TLIST": f"'{jdTdb:.10f}'", "TIME_TYPE": "'TDB'",
        "OUT_UNITS": "'KM-S'", "VEC_TABLE": "'2'", "CSV_FORMAT": "'YES'",
    })["result"]
    if "$$SOE" not in result:
        raise RuntimeError(f"Horizons gave no state for {record!r}:\n{result[-800:]}")
    row = result.split("$$SOE", 1)[1].split("$$EOE", 1)[0].strip().split(",")
    values = [float(x) for x in row[2:8]]
    return ([x * METERS_PER_KM for x in values[:3]], [x * METERS_PER_KM for x in values[3:]])


def photometry(number):
    """(H, G) from the small-body database; G is DEFAULT_G where it has none."""
    data = _getJson(SBDB_API, {"sstr": number, "phys-par": "1"})
    params = {p["name"]: p["value"] for p in data.get("phys_par", [])}
    if params.get("H") is None:
        raise RuntimeError(f"No H magnitude for {number} in the small-body database")
    g = params.get("G")
    return float(params["H"]), float(g) if g is not None else DEFAULT_G, data["object"]["fullname"]


def addSmallBodies(path=outputPath):
    """Add (or refresh) every body of small_bodies.py in initial_conditions.json."""
    state = json.loads(path.read_text())
    et = float(state["epoch_et"])
    print(f"Epoch {state['epoch_utc']} (ET {et})")

    # The frames must agree: Horizons' Earth against DE440's, from SPICE
    earthR, earthV = horizonsState("399", et)
    ours = state["bodies"]["earth"]
    dr = sum((a - b) ** 2 for a, b in zip(earthR, ours["position_m"])) ** 0.5
    dv = sum((a - b) ** 2 for a, b in zip(earthV, ours["velocity_m_s"])) ** 0.5
    print(f"Earth, Horizons vs DE440: {dr:.1f} m, {dv * 1000:.2f} mm/s")
    if dr > 1000.0 or dv > 0.01:
        raise RuntimeError("Horizons' frame or epoch doesn't match initial_conditions.json")

    for key, (name, number, kind, region) in SMALL_BODIES.items():
        r, v = horizonsState(f"{number};", et)
        h, g, fullname = photometry(number)
        state["bodies"][key] = {
            "horizons": f"{number};",
            "designation": fullname,
            "kind": kind,
            "region": region,
            "position_m": r,
            "velocity_m_s": v,
            "gm_m3_s2": 0.0,        # a test particle: see small_bodies.py
            "H": h,
            "G": g,
        }
        print(f"  {fullname:28s} {kind:13s} r = {sum(x * x for x in r) ** 0.5 / 1.495978707e11:6.2f} AU"
              f"  H {h:5.2f}  G {g:.2f}")
    state["small_bodies_source"] = "JPL Horizons (states), JPL SBDB (H, G)"

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
    print("Saved:", path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--epoch", help="UTC epoch, ISO 8601 (default: now)")
    parser.add_argument("--small-bodies", action="store_true",
                        help="add the bodies of small_bodies.py to the existing file, at its epoch")
    args = parser.parse_args()
    if args.small_bodies:
        addSmallBodies()
        return

    if args.epoch:
        epoch = datetime.fromisoformat(args.epoch.replace("Z", "+00:00"))
        epoch = epoch.astimezone(timezone.utc) if epoch.tzinfo else epoch
    else:
        epoch = datetime.now(timezone.utc)
    writeInitialConditions(epoch.strftime("%Y-%m-%dT%H:%M:%S.%f"))


if __name__ == "__main__":
    main()
