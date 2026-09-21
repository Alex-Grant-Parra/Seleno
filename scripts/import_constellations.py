"""Import the star map's constellation figures into the database.

Source
------
Line figures and constellation metadata from d3-celestial by Olaf Frohn
(BSD-3-Clause), published as GeoJSON in J2000 coordinates:

    https://github.com/ofrohn/d3-celestial
    data/constellations.lines.json   - MultiLineString figures
    data/constellations.json         - names, IAU abbreviations, label anchors

What it does
------------
1. Keeps only the prominent constellations. d3-celestial ranks every figure
   1-3 by prominence; rank 1 is 22 constellations, to which we add Ursa Minor
   and Crux because they carry the north and south pole markers.
2. Matches every figure vertex to the catalogue star it was drawn from, so each
   line can be stored as a pair of star designations instead of coordinates.
   The line then always lands exactly on the star the map draws.
3. Replaces the contents of ConstellationsTable and ConstellationLinesTable in
   one transaction (the importer owns both tables, so re-running is safe).

Run it after scripts/import_star_names.py: that fills in magnitudes for the
variable stars some figures pass through (Algol, eta Aquilae...), which lets
them match on the first, stricter pass.

    venv/bin/python scripts/import_constellations.py
    venv/bin/python scripts/import_constellations.py --dry-run
"""

import argparse
import json
import math
import os
import sys
import urllib.request

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "instance", "Data.db")
LINES_URL = "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/constellations.lines.json"
META_URL = "https://raw.githubusercontent.com/ofrohn/d3-celestial/master/data/constellations.json"

# Prominence rank to keep, plus the two pole-finders that rank just below it.
KEEP_RANKS = {"1"}
ALWAYS_KEEP = {"UMi", "Cru"}

# A vertex is a real star, so its catalogue match should be within an arcminute
# or so. Stars with a magnitude are tried first; objects with none are a far
# larger pool, so they are only accepted at a much tighter distance.
SNAP_TOLERANCE_DEG = 0.25
SNAP_MAX_MAG = 6.5
SNAP_TOLERANCE_UNKNOWN_DEG = 0.1

HD_PLACEHOLDER_MAGS = {20.0, 30.0, 40.0, 50.0}
STAR_TABLES = ("HDSTARTable", "IndexTable", "NGCtable")


def fetch_json(url, cache_dir=None):
    if cache_dir:
        cached = os.path.join(cache_dir, os.path.basename(url))
        if os.path.exists(cached):
            print(f"Reading {os.path.basename(url)} from {cache_dir}")
            with open(cached, encoding="utf-8") as handle:
                return json.load(handle)
    print(f"Fetching {url}")
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def load_star_buckets(conn):
    """(bright, unknown): catalogue stars bucketed by whole degree of declination."""
    from sqlalchemy import text

    bright, unknown = {}, {}
    for table in STAR_TABLES:
        rows = conn.execute(text(
            f'SELECT Name, RA, DEC, "V-Mag" FROM "{table}" '
            f'WHERE "V-Mag" IS NULL OR "V-Mag" <= :limit OR "V-Mag" IN (20.0, 30.0, 40.0, 50.0)'
        ), {"limit": SNAP_MAX_MAG}).fetchall()
        is_hd = table == "HDSTARTable"
        for name, ra, dec, mag in rows:
            try:
                ra = float(ra)
                dec = float(dec)
            except (TypeError, ValueError):
                continue
            has_mag = mag is not None and not (is_hd and float(mag) in HD_PLACEHOLDER_MAGS)
            target = bright if has_mag else unknown
            target.setdefault(int(math.floor(dec)), []).append((ra, dec, name))
    return bright, unknown


def angular_separation(ra1, dec1, ra2, dec2):
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dl = math.radians(ra1 - ra2)
    cos_sep = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def nearest_in(ra, dec, buckets, tolerance):
    best, best_sep = None, tolerance
    base = int(math.floor(dec))
    for band in (base - 1, base, base + 1):
        for star_ra, star_dec, name in buckets.get(band, ()):
            sep = angular_separation(ra, dec, star_ra, star_dec)
            if sep < best_sep:
                best_sep, best = sep, name
    return best


def star_for_vertex(ra, dec, bright, unknown):
    return (nearest_in(ra, dec, bright, SNAP_TOLERANCE_DEG)
            or nearest_in(ra, dec, unknown, SNAP_TOLERANCE_UNKNOWN_DEG))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache-dir", help="read the upstream GeoJSON from here instead of the network")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true", help="report what would be written, write nothing")
    args = parser.parse_args()

    lines_doc = fetch_json(LINES_URL, args.cache_dir)
    meta_doc = fetch_json(META_URL, args.cache_dir)

    meta = {}
    for feature in meta_doc["features"]:
        props = feature["properties"]
        anchor = feature.get("geometry", {}).get("coordinates")
        meta[props["desig"]] = {
            "name": props.get("name") or props.get("en") or props["desig"],
            "rank": props.get("rank", "3"),
            "anchor": anchor,
        }

    from flask import Flask
    from sqlalchemy import text
    from app.db import db
    from models.tables import ensure_star_catalogue_schema

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{args.db}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)

    with app.app_context():
        if not args.dry_run:
            ensure_star_catalogue_schema()
        with db.engine.connect() as conn:
            bright, unknown = load_star_buckets(conn)

        figures = []
        vertices = matched = 0
        missed = []

        for feature in lines_doc["features"]:
            abbr = feature["id"]
            info = meta.get(abbr, {})
            rank = info.get("rank", feature["properties"].get("rank", "3"))
            if rank not in KEEP_RANKS and abbr not in ALWAYS_KEEP:
                continue

            edges = []
            seen = set()
            for path in feature["geometry"]["coordinates"]:
                stars = []
                for lon, lat in path:
                    ra = lon + 360.0 if lon < 0 else lon
                    vertices += 1
                    star = star_for_vertex(ra, lat, bright, unknown)
                    if star:
                        matched += 1
                    else:
                        missed.append(f"{abbr} ({ra:.3f}, {lat:.3f})")
                    stars.append(star)
                # Consecutive vertices make a line; skip gaps and repeats
                for a, b in zip(stars, stars[1:]):
                    if not a or not b or a == b:
                        continue
                    key = frozenset((a, b))
                    if key in seen:
                        continue
                    seen.add(key)
                    edges.append((a, b))

            anchor = info.get("anchor")
            label_ra = label_dec = None
            if anchor:
                label_ra = anchor[0] + 360.0 if anchor[0] < 0 else anchor[0]
                label_dec = anchor[1]
            figures.append({"abbr": abbr, "name": info.get("name", abbr),
                            "label_ra": label_ra, "label_dec": label_dec, "edges": edges})

        figures.sort(key=lambda f: f["name"])
        total_edges = sum(len(f["edges"]) for f in figures)
        print(f"\n{len(figures)} constellations, {total_edges} lines, "
              f"{matched}/{vertices} vertices matched to catalogue stars")
        if missed:
            print(f"  unmatched vertices (their lines are dropped): {', '.join(missed)}")
        print("  " + ", ".join(f["abbr"] for f in figures))

        if args.dry_run:
            print("\n--dry-run: nothing written")
            return 0

        with db.engine.begin() as conn:
            conn.execute(text('DELETE FROM "ConstellationLinesTable"'))
            conn.execute(text('DELETE FROM "ConstellationsTable"'))
            for figure in figures:
                conn.execute(text(
                    'INSERT INTO "ConstellationsTable" (Abbr, Name, LabelRA, LabelDEC) '
                    'VALUES (:abbr, :name, :ra, :dec)'
                ), {"abbr": figure["abbr"], "name": figure["name"],
                    "ra": figure["label_ra"], "dec": figure["label_dec"]})
                for a, b in figure["edges"]:
                    conn.execute(text(
                        'INSERT INTO "ConstellationLinesTable" (Constellation, StarA, StarB) '
                        'VALUES (:abbr, :a, :b)'
                    ), {"abbr": figure["abbr"], "a": a, "b": b})
        print(f"\nWrote {len(figures)} constellations and {total_edges} lines.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
