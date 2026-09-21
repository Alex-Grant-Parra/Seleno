"""Build the star map's constellation line data.

Source
------
Line figures and constellation metadata come from d3-celestial by Olaf Frohn
(BSD-3-Clause), which publishes them as GeoJSON in J2000 equatorial
coordinates:

    https://github.com/ofrohn/d3-celestial
    data/constellations.lines.json   - MultiLineString figures
    data/constellations.json         - names, IAU designations, label anchors

What this script does
---------------------
1. Keeps only the prominent constellations. d3-celestial ranks every figure
   1-3 by prominence; rank 1 is 22 constellations, to which we add Ursa Minor
   and Crux because they carry the north and south pole markers.
2. Converts longitudes (-180..180) to right ascension (0..360).
3. Snaps each vertex onto the nearest catalogue star in this server's own
   database, when one sits within SNAP_TOLERANCE_DEG. The figures are drawn
   from the same coordinates the map plots, so the lines then terminate
   exactly on the star sprites instead of a fraction of an arcminute away.
4. Writes static/data/constellations.json.

Run it from the repository root:

    venv/bin/python scripts/build_constellations.py
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import urllib.request

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DB_PATH = os.path.join(BASE_DIR, "instance", "Data.db")
OUT_PATH = os.path.join(BASE_DIR, "static", "data", "constellations.json")

LINES_URL = "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/constellations.lines.json"
META_URL = "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/constellations.json"

# Prominence rank to keep, plus the two pole-finders that rank just below it.
KEEP_RANKS = {"1"}
ALWAYS_KEEP = {"UMi", "Cru"}

# A figure vertex is a real star, so a catalogue match should be within an
# arcminute or so; the allowance covers epoch/precision differences between
# catalogues without risking a snap onto the wrong star.
SNAP_TOLERANCE_DEG = 0.25
SNAP_MAX_MAG = 6.5

# Some figure vertices are stars the Henry Draper catalogue gives no magnitude
# for: it uses the placeholders 20/30/40/50 for variables, which is how Algol,
# eta Aquilae, zeta Geminorum and friends end up with no V-Mag. Those are matched
# in a second pass with a tighter tolerance, because the pool of
# unknown-magnitude objects is large and a loose match could grab the wrong one.
HD_PLACEHOLDER_MAGS = (20.0, 30.0, 40.0, 50.0)
SNAP_TOLERANCE_UNKNOWN_DEG = 0.1

STAR_TABLES = ("HDSTARTable", "IndexTable", "NGCtable")


def fetch_json(url, cache_dir=None):
    if cache_dir:
        cached = os.path.join(cache_dir, os.path.basename(url))
        if os.path.exists(cached):
            with open(cached, encoding="utf-8") as handle:
                return json.load(handle)
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def load_star_buckets(db_path):
    """Catalogue stars bucketed by whole degree of declination.

    Returns (bright, unknown): stars with a usable magnitude, and stars whose
    magnitude the catalogue never recorded.
    """
    bright, unknown = {}, {}
    if not os.path.exists(db_path):
        print(f"  (no database at {db_path}; skipping the snap step)")
        return bright, unknown
    placeholders = set(HD_PLACEHOLDER_MAGS)
    con = sqlite3.connect(db_path)
    try:
        for table in STAR_TABLES:
            try:
                rows = con.execute(
                    f'SELECT Name, RA, DEC, "V-Mag" FROM "{table}" '
                    f'WHERE "V-Mag" IS NULL OR "V-Mag" <= ? OR "V-Mag" IN (?, ?, ?, ?)',
                    (SNAP_MAX_MAG,) + HD_PLACEHOLDER_MAGS
                ).fetchall()
            except sqlite3.Error as exc:
                print(f"  (could not read {table}: {exc})")
                continue
            is_hd = table == "HDSTARTable"
            for name, ra, dec, mag in rows:
                try:
                    ra = float(ra)
                    dec = float(dec)
                except (TypeError, ValueError):
                    continue
                has_mag = mag is not None and not (is_hd and float(mag) in placeholders)
                target = bright if has_mag else unknown
                target.setdefault(int(math.floor(dec)), []).append((ra, dec, name, mag))
    finally:
        con.close()
    return bright, unknown


def angular_separation(ra1, dec1, ra2, dec2):
    """Great-circle separation in degrees."""
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dl = math.radians(ra1 - ra2)
    cos_sep = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def nearest_in(ra, dec, buckets, tolerance):
    """Nearest catalogue star within `tolerance` degrees, or None."""
    best = None
    best_sep = tolerance
    base = int(math.floor(dec))
    for band in (base - 1, base, base + 1):
        for star_ra, star_dec, name, mag in buckets.get(band, ()):
            sep = angular_separation(ra, dec, star_ra, star_dec)
            if sep < best_sep:
                best_sep = sep
                best = (star_ra, star_dec, name, mag)
    return best


def snap_to_star(ra, dec, bright, unknown):
    """Match a figure vertex to a catalogue star: bright stars first, then the
    unknown-magnitude ones (mostly variables) at a tighter tolerance."""
    hit = nearest_in(ra, dec, bright, SNAP_TOLERANCE_DEG)
    if hit:
        return hit, False
    hit = nearest_in(ra, dec, unknown, SNAP_TOLERANCE_UNKNOWN_DEG)
    if hit:
        return hit, True
    return None, False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", help="read the upstream GeoJSON from here instead of the network")
    parser.add_argument("--out", default=OUT_PATH)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    print("Fetching d3-celestial constellation data…")
    lines_doc = fetch_json(LINES_URL, args.cache_dir)
    meta_doc = fetch_json(META_URL, args.cache_dir)

    meta = {}
    for feature in meta_doc["features"]:
        props = feature["properties"]
        anchor = feature.get("geometry", {}).get("coordinates")
        meta[props["desig"]] = {
            "name": props.get("name") or props.get("en") or props["desig"],
            "genitive": props.get("gen", ""),
            "rank": props.get("rank", "3"),
            "anchor": anchor,
        }

    print(f"Loading catalogue stars (V-Mag <= {SNAP_MAX_MAG}, plus unrecorded magnitudes)…")
    bright, unknown = load_star_buckets(args.db)
    print(f"  {sum(len(v) for v in bright.values())} with a magnitude, "
          f"{sum(len(v) for v in unknown.values())} without")

    constellations = []
    total_vertices = snapped = snapped_unknown = 0
    variable_vertices = []

    for feature in lines_doc["features"]:
        desig = feature["id"]
        info = meta.get(desig, {})
        rank = info.get("rank", feature["properties"].get("rank", "3"))
        if rank not in KEEP_RANKS and desig not in ALWAYS_KEEP:
            continue

        paths = []
        for line in feature["geometry"]["coordinates"]:
            path = []
            for lon, lat in line:
                ra = lon + 360.0 if lon < 0 else lon
                total_vertices += 1
                hit, from_unknown = snap_to_star(ra, lat, bright, unknown)
                if hit:
                    ra, lat = hit[0], hit[1]
                    snapped += 1
                    if from_unknown:
                        snapped_unknown += 1
                        variable_vertices.append(f"{desig}:{hit[2]}")
                path.append([round(ra, 4), round(lat, 4)])
            if len(path) >= 2:
                paths.append(path)

        if not paths:
            continue

        anchor = info.get("anchor")
        if anchor:
            anchor_ra = anchor[0] + 360.0 if anchor[0] < 0 else anchor[0]
            anchor = [round(anchor_ra, 4), round(anchor[1], 4)]

        constellations.append({
            "id": desig,
            "name": info.get("name", desig),
            "anchor": anchor,
            "lines": paths,
        })

    constellations.sort(key=lambda c: c["name"])

    payload = {
        "epoch": "J2000",
        "source": "d3-celestial by Olaf Frohn (BSD-3-Clause) - "
                  "https://github.com/ofrohn/d3-celestial",
        "note": "Prominent constellations only (d3-celestial rank 1, plus Ursa Minor "
                "and Crux). Vertices snapped onto this server's catalogue stars where "
                f"one lies within {SNAP_TOLERANCE_DEG} deg.",
        "count": len(constellations),
        "constellations": constellations,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, separators=(",", ":"))

    segments = sum(len(p) - 1 for c in constellations for p in c["lines"])
    print(f"\nWrote {args.out}")
    print(f"  {len(constellations)} constellations, {segments} segments, {total_vertices} vertices")
    print(f"  {snapped}/{total_vertices} vertices snapped to catalogue stars "
          f"({100.0 * snapped / max(1, total_vertices):.0f}%)")
    if snapped_unknown:
        print(f"  {snapped_unknown} of those are stars with no recorded magnitude "
              f"(HD variables): {', '.join(sorted(set(variable_vertices)))}")
    print(f"  {os.path.getsize(args.out) / 1024:.1f} KB")
    print("  " + ", ".join(c["id"] for c in constellations))
    return 0


if __name__ == "__main__":
    sys.exit(main())
