"""Build the star map's proper-name table from the IAU catalogue.

Why
---
The HDSTARTable ships common names for only 40 stars, and the Henry Draper
catalogue records no magnitude at all for its variables (it stores the
placeholders 20/30/40/50 instead). The upshot is that Algol and Eta Carinae -
both easy naked-eye stars - are absent from the map, and familiar stars like
Mizar, Alphard and Alpheratz cannot be searched for by name.

Sources
-------
IAU Catalog of Star Names (IAU-CSN), maintained by the IAU Working Group on
Star Names: the official list of approved proper names, with Bayer
designations, V magnitudes and HIP/HD cross-identifiers. IAU material is
released under Creative Commons Attribution.

    https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt

Yale Bright Star Catalogue, 5th Revised Edition (Hoffleit & Warren 1991),
CDS catalogue V/50: every star down to V = 6.5, with HD numbers, V magnitudes,
Bayer/Flamsteed designations and variable-star identifiers. It supplies
magnitudes for the naked-eye variables the Henry Draper catalogue left blank -
103 of them, Algol and Delta Cephei among them.

    https://cdsarc.cds.unistra.fr/ftp/V/50/catalog.gz

What this script does
---------------------
1. Parses both catalogues.
2. Matches entries to rows in this server's catalogue - by HD number first,
   then by J2000 position for entries whose HD number is absent or unlisted.
3. Writes static/data/star_names.json, keyed by catalogue designation, holding
   the proper name, Bayer designation, variable-star identifier and a fallback
   V magnitude.

Only stars that need something are included: those with a proper name, and
those whose magnitude the local catalogue is missing. The magnitude is used by
app/star_catalog.py to fill in *missing* catalogue magnitudes only; it never
overrides a magnitude the database already has.

    venv/bin/python scripts/build_star_names.py
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import gzip
import urllib.request

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
DB_PATH = os.path.join(BASE_DIR, "instance", "Data.db")
OUT_PATH = os.path.join(BASE_DIR, "static", "data", "star_names.json")
CSN_URL = "https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt"
BSC_URL = "https://cdsarc.cds.unistra.fr/ftp/V/50/catalog.gz"

# Bright stars, so a positional match should be very close; this is loose
# enough for catalogue-to-catalogue differences and far tighter than the
# spacing between naked-eye stars.
MATCH_TOLERANCE_DEG = 0.05

STAR_TABLES = ("HDSTARTable", "IndexTable", "NGCtable")

GREEK = {
    "alf": "α", "bet": "β", "gam": "γ", "del": "δ", "eps": "ε", "zet": "ζ",
    "eta": "η", "tet": "θ", "iot": "ι", "kap": "κ", "lam": "λ", "mu.": "μ",
    "nu.": "ν", "xi.": "ξ", "omi": "ο", "pi.": "π", "rho": "ρ", "sig": "σ",
    "tau": "τ", "ups": "υ", "phi": "φ", "chi": "χ", "psi": "ψ", "ome": "ω",
}


def parse_csn(text):
    """Yield one dict per IAU-CSN entry.

    The file is fixed width up to the WDS identifier; everything after it
    (mag, band, HIP, HD, RA, Dec, date) is whitespace separated, with "_"
    standing in for a missing value.
    """
    for line in text.splitlines():
        if not line.strip() or line[0] in "#$":
            continue
        name = line[0:18].strip()
        bayer_id = line[49:55].strip()
        constellation = line[61:65].strip()
        tail = line[82:].split()
        if not name or len(tail) < 6:
            continue
        mag, band, hip, hd, ra, dec = tail[:6]

        def number(value):
            if value in ("_", "", "-"):
                return None
            try:
                return float(value)
            except ValueError:
                return None

        greek = GREEK.get(bayer_id)
        bayer = f"{greek} {constellation}" if greek and constellation else ""

        yield {
            "name": name,
            "bayer": bayer,
            "con": constellation,
            "mag": number(mag) if band == "V" else None,
            "hd": None if hd in ("_", "") else hd,
            "ra": number(ra),
            "dec": number(dec),
        }


def parse_bsc(text):
    """Yield (hd_designation, record) for Bright Star Catalogue rows.

    Fixed-width, per the CDS V/50 byte-by-byte description: HD in bytes 26-31,
    Bayer/Flamsteed name in 5-14, variable-star identifier in 52-60 and the V
    magnitude in 103-107.
    """
    for line in text.splitlines():
        if len(line) < 107:
            continue
        hd = line[25:31].strip()
        vmag = line[102:107].strip()
        if not hd or not vmag:
            continue
        try:
            hd_number = int(hd)
            mag = float(vmag)
        except ValueError:
            continue
        yield f"HD{hd_number}", {
            "designation": tidy_bsc_name(line[4:14]),
            "var": line[51:60].strip(),
            "mag": mag,
        }


def tidy_bsc_name(raw):
    """'26Bet Per' -> 'Bet Per'; 'Mu  Cep' -> 'Mu Cep'."""
    name = raw.strip()
    # A leading Flamsteed number is dropped; the Bayer letter is the useful part
    digits = 0
    while digits < len(name) and name[digits].isdigit():
        digits += 1
    name = name[digits:].strip()
    return " ".join(name.split())


def fetch_text(url, cache, gzipped=False):
    if cache and os.path.exists(cache):
        print(f"Reading {os.path.basename(url)} from {cache}")
        mode = "rb" if gzipped else "r"
        if gzipped:
            with open(cache, mode) as handle:
                raw = handle.read()
            if cache.endswith(".gz"):
                raw = gzip.decompress(raw)
            return raw.decode("latin-1")
        return open(cache, encoding="utf-8").read()
    print(f"Fetching {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        raw = response.read()
    if gzipped:
        raw = gzip.decompress(raw)
        return raw.decode("latin-1")
    return raw.decode("utf-8")


def load_catalogue(db_path):
    """(by_hd, by_position_bucket) views of the catalogue tables."""
    by_hd = {}
    buckets = {}
    con = sqlite3.connect(db_path)
    try:
        for table in STAR_TABLES:
            try:
                rows = con.execute(f'SELECT Name, RA, DEC, "V-Mag" FROM "{table}"').fetchall()
            except sqlite3.Error as exc:
                print(f"  (could not read {table}: {exc})")
                continue
            for name, ra, dec, mag in rows:
                try:
                    ra = float(ra)
                    dec = float(dec)
                except (TypeError, ValueError):
                    continue
                entry = (name, ra, dec, mag, table)
                if table == "HDSTARTable":
                    by_hd[name.upper()] = entry
                buckets.setdefault(int(math.floor(dec)), []).append(entry)
    finally:
        con.close()
    return by_hd, buckets


def angular_separation(ra1, dec1, ra2, dec2):
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dl = math.radians(ra1 - ra2)
    cos_sep = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def match_by_position(ra, dec, buckets):
    best, best_sep = None, MATCH_TOLERANCE_DEG
    base = int(math.floor(dec))
    for band in (base - 1, base, base + 1):
        for entry in buckets.get(band, ()):
            sep = angular_separation(ra, dec, entry[1], entry[2])
            if sep < best_sep:
                best_sep, best = sep, entry
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", help="directory holding IAU-CSN.txt and bsc5.dat/catalog.gz")
    parser.add_argument("--out", default=OUT_PATH)
    parser.add_argument("--db", default=DB_PATH)
    args = parser.parse_args()

    cache_dir = args.cache
    csn_cache = os.path.join(cache_dir, "IAU-CSN.txt") if cache_dir else None
    bsc_cache = None
    if cache_dir:
        for candidate in ("bsc5.dat", "catalog", "catalog.gz", "bsc5.gz"):
            path = os.path.join(cache_dir, candidate)
            if os.path.exists(path):
                bsc_cache = path
                break

    csn_text = fetch_text(CSN_URL, csn_cache)
    bsc_text = fetch_text(BSC_URL, bsc_cache, gzipped=not (bsc_cache or "").endswith(".dat"))

    updated = ""
    for line in csn_text.splitlines()[:8]:
        if "Last updated" in line:
            updated = line.split("Last updated", 1)[1].strip().strip("(").split()[0]
            break

    iau_entries = list(parse_csn(csn_text))
    bsc_entries = dict(parse_bsc(bsc_text))
    print(f"  {len(iau_entries)} IAU proper names (updated {updated or 'unknown'})")
    print(f"  {len(bsc_entries)} Bright Star Catalogue entries with HD number and V magnitude")

    print("Matching against the local catalogue…")
    by_hd, buckets = load_catalogue(args.db)

    names = {}
    matched_hd = matched_pos = unmatched = 0
    placeholders = {20.0, 30.0, 40.0, 50.0}

    def lacks_magnitude(cat_mag, table):
        return cat_mag is None or (table == "HDSTARTable" and cat_mag in placeholders)

    # --- IAU proper names -------------------------------------------------
    for entry in iau_entries:
        target = None
        if entry["hd"]:
            target = by_hd.get(f"HD{entry['hd']}")
            if target:
                matched_hd += 1
        if target is None and entry["ra"] is not None and entry["dec"] is not None:
            target = match_by_position(entry["ra"], entry["dec"], buckets)
            if target:
                matched_pos += 1
        if target is None:
            unmatched += 1
            continue

        cat_name, _, _, cat_mag, table = target
        record = names.setdefault(cat_name, {})
        record["name"] = entry["name"]
        if entry["bayer"]:
            record["bayer"] = entry["bayer"]
        if entry["mag"] is not None and lacks_magnitude(cat_mag, table):
            record["mag"] = entry["mag"]
            record["magSource"] = "IAU"

    # --- Bright Star Catalogue, for the stars the HD catalogue left blank --
    filled = []
    for cat_name, bsc in bsc_entries.items():
        target = by_hd.get(cat_name)
        if target is None:
            continue
        _, _, _, cat_mag, table = target
        if not lacks_magnitude(cat_mag, table):
            continue
        record = names.setdefault(cat_name, {})
        record.setdefault("mag", bsc["mag"])
        record.setdefault("magSource", "BSC5")
        if bsc["var"]:
            record["var"] = bsc["var"]
        if bsc["designation"] and "bayer" not in record:
            record["bayer"] = bsc["designation"]
        filled.append((record["mag"], cat_name, record.get("name") or record.get("var") or bsc["designation"]))

    payload = {
        "sources": [
            "IAU Catalog of Star Names (IAU-CSN), IAU Working Group on Star Names "
            "(CC BY) - https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt",
            "Yale Bright Star Catalogue, 5th Revised Ed. (Hoffleit & Warren 1991), "
            "CDS V/50 - https://cdsarc.cds.unistra.fr/ftp/V/50/",
        ],
        "updated": updated,
        "note": "Keyed by catalogue designation. `mag` is a fallback V magnitude, used "
                "only where the local catalogue records none; it never overrides one.",
        "count": len(names),
        "stars": names,
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    named = sum(1 for r in names.values() if "name" in r)
    with_mag = sum(1 for r in names.values() if "mag" in r)
    print(f"\nWrote {args.out}")
    print(f"  {len(names)} entries: {named} with a proper name, "
          f"{with_mag} supplying a magnitude the catalogue lacks")
    print(f"  IAU names matched: {matched_hd} by HD number, {matched_pos} by position, "
          f"{unmatched} unmatched")
    print(f"  {os.path.getsize(args.out) / 1024:.1f} KB")
    filled.sort()
    print(f"\n  Stars that become visible (brightest 15 of {len(filled)}):")
    for mag, cat_name, label in filled[:15]:
        print(f"    V={mag:<6} {cat_name:<10} {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
