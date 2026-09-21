"""Import star names, Bayer designations and missing magnitudes into the database.

Why
---
HDSTARTable ships common names for only 40 stars, and the Henry Draper
catalogue records no magnitude for its variables - it stores the placeholders
20/30/40/50 instead. So Algol, Delta Cephei and 101 other naked-eye stars could
not be drawn at all, and familiar stars like Mizar, Alphard and Alpheratz could
not be found by name.

Sources
-------
IAU Catalog of Star Names (IAU-CSN), IAU Working Group on Star Names: the
official list of approved proper names, with Bayer designations, V magnitudes
and HD cross-identifiers. IAU material is released under Creative Commons
Attribution.

    https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt

Yale Bright Star Catalogue, 5th Revised Edition (Hoffleit & Warren 1991), CDS
catalogue V/50: every star to V = 6.5, with HD numbers, V magnitudes, Bayer /
Flamsteed designations and variable-star identifiers.

    https://cdsarc.cds.unistra.fr/ftp/V/50/catalog.gz

What it writes (HDSTARTable only)
---------------------------------
commonNames  IAU proper names, added as an alias. A name the row already carries
             (compared case-insensitively) is never added again, and existing
             names keep their place at the front, so the name the map displays
             only changes for rows that had none.
bayer        Bayer designation, e.g. "β Per" - only where currently empty.
variableId   Variable-star designation, e.g. "Bet Per" - only for the stars
             whose magnitude is being filled in, and only where empty.
V-Mag        Filled in only where the value is NULL or an HD placeholder. The IAU
             value is preferred, then the Bright Star Catalogue.
magSource    Written alongside every V-Mag it fills in, naming the source and
             keeping the original placeholder (e.g. "IAU-CSN; HD placeholder
             30.0") so the change can be reversed.

Nothing that already has a value is overwritten, so re-running is a no-op.

    venv/bin/python scripts/import_star_names.py            # import
    venv/bin/python scripts/import_star_names.py --dry-run  # report only
"""

import argparse
import gzip
import math
import os
import sys
import urllib.request

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, BASE_DIR)

DB_PATH = os.path.join(BASE_DIR, "instance", "Data.db")
CSN_URL = "https://www.pas.rochester.edu/~emamajek/WGSN/IAU-CSN.txt"
BSC_URL = "https://cdsarc.cds.unistra.fr/ftp/V/50/catalog.gz"

HD_PLACEHOLDER_MAGS = {20.0, 30.0, 40.0, 50.0}

# Bright stars, so a positional match should be very close; this is loose
# enough for catalogue-to-catalogue differences and far tighter than the
# spacing between naked-eye stars.
MATCH_TOLERANCE_DEG = 0.05

STAR_TABLES = ("HDSTARTable", "IndexTable", "NGCtable")

# Bright Star Catalogue abbreviations -> Greek letters
GREEK = {
    "Alp": "α", "Bet": "β", "Gam": "γ", "Del": "δ", "Eps": "ε", "Zet": "ζ",
    "Eta": "η", "The": "θ", "Iot": "ι", "Kap": "κ", "Lam": "λ", "Mu": "μ",
    "Nu": "ν", "Xi": "ξ", "Omi": "ο", "Pi": "π", "Rho": "ρ", "Sig": "σ",
    "Tau": "τ", "Ups": "υ", "Phi": "φ", "Chi": "χ", "Psi": "ψ", "Ome": "ω",
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_csn(text):
    """One dict per IAU-CSN entry.

    Fixed width up to the WDS identifier; everything after it (mag, band, HIP,
    HD, RA, Dec, date) is whitespace separated, with "_" for a missing value.
    """
    for line in text.splitlines():
        if not line.strip() or line[0] in "#$":
            continue
        name = line[0:18].strip()
        greek = line[55:61].strip()           # the "ID" column with diacritics, e.g. "θ1"
        constellation = line[61:65].strip()
        tail = line[82:].split()
        if not name or len(tail) < 6:
            continue
        mag, band, _hip, hd, ra, dec = tail[:6]
        yield {
            "name": name,
            "bayer": f"{greek} {constellation}" if greek not in ("", "_") and constellation else "",
            "mag": _number(mag) if band == "V" else None,
            "hd": None if hd in ("_", "") else hd,
            "ra": _number(ra),
            "dec": _number(dec),
        }


def parse_bsc(text):
    """(HD designation, record) for Bright Star Catalogue rows.

    Per the CDS V/50 byte-by-byte description: HD in bytes 26-31, the name in
    5-14 (Flamsteed number, Bayer letter, superscript, constellation), the
    variable-star identifier in 52-60 and the V magnitude in 103-107.
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
        var = " ".join(line[51:60].split())
        if var.isdigit():
            var = f"NSV {var}"  # bare numbers are New Suspected Variables catalogue entries
        yield f"HD{hd_number}", {
            "bayer": bsc_designation(line[4:14]),
            "var": var,
            "mag": mag,
        }


def bsc_designation(raw):
    """'26Bet Per' -> 'β Per', 'Gam1Sgr' -> 'γ1 Sgr', '13    Lyr' -> '13 Lyr'."""
    raw = raw.ljust(10)
    flamsteed = raw[0:3].strip()
    letter = raw[3:6].strip()
    superscript = raw[6:7].strip()
    constellation = raw[7:10].strip()
    if not constellation:
        return ""
    if letter:
        return f"{GREEK.get(letter, letter)}{superscript} {constellation}"
    if flamsteed:
        return f"{flamsteed} {constellation}"
    return ""


def _number(value):
    if value in ("_", "", "-"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def fetch_text(url, cache_dir, candidates, gzipped=False):
    for candidate in candidates if cache_dir else ():
        path = os.path.join(cache_dir, candidate)
        if os.path.exists(path):
            print(f"Reading {candidate} from {cache_dir}")
            with open(path, "rb") as handle:
                raw = handle.read()
            if path.endswith(".gz"):
                raw = gzip.decompress(raw)
            return raw.decode("latin-1" if gzipped else "utf-8")
    print(f"Fetching {url}")
    with urllib.request.urlopen(url, timeout=120) as response:
        raw = response.read()
    if gzipped:
        raw = gzip.decompress(raw)
        return raw.decode("latin-1")
    return raw.decode("utf-8")


# --------------------------------------------------------------------------
# Name aliases
# --------------------------------------------------------------------------
def split_aliases(cell):
    return [part.strip() for part in str(cell or "").split(",") if part.strip()]


def is_designation(alias):
    """Catalogue numbers such as 'HD 34029' or 'M31', as opposed to names."""
    upper = alias.upper().replace(" ", "")
    for prefix in ("HD", "NGC", "IC", "M"):
        if upper.startswith(prefix) and upper[len(prefix):].isdigit():
            return True
    return False


def merge_name(cell, new_name):
    """The cell with `new_name` added, or None if it is already there.

    Existing names keep their order; the new one goes after them and before any
    catalogue-number aliases, so the first (displayed) name only changes when
    the row had no name at all.
    """
    aliases = split_aliases(cell)
    if new_name.strip().lower() in {a.lower() for a in aliases}:
        return None
    names = [a for a in aliases if not is_designation(a)]
    numbers = [a for a in aliases if is_designation(a)]
    return ", ".join(names + [new_name.strip()] + numbers)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------
def angular_separation(ra1, dec1, ra2, dec2):
    p1, p2 = math.radians(dec1), math.radians(dec2)
    dl = math.radians(ra1 - ra2)
    cos_sep = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.acos(max(-1.0, min(1.0, cos_sep))))


def load_catalogue(conn):
    """HD rows keyed by designation, plus every star bucketed by declination."""
    from sqlalchemy import text

    hd_rows = {}
    buckets = {}
    # SQLite reads a double-quoted name that is not a column as a string
    # literal, so a missing column would come back as its own name rather than
    # an error. Select NULL for any the schema step has not added yet.
    hd_columns = {r[1] for r in conn.execute(text('PRAGMA table_info("HDSTARTable")'))}

    def column_or_null(column):
        return f'"{column}"' if column in hd_columns else "NULL"

    for table in STAR_TABLES:
        extra = ""
        if table == "HDSTARTable":
            extra = ", " + ", ".join(column_or_null(c) for c in
                                     ("commonNames", "bayer", "variableId", "magSource"))
        rows = conn.execute(text(f'SELECT Name, RA, DEC, "V-Mag"{extra} FROM "{table}"')).fetchall()
        for row in rows:
            name, ra, dec, mag = row[0], row[1], row[2], row[3]
            try:
                ra = float(ra)
                dec = float(dec)
            except (TypeError, ValueError):
                continue
            if table == "HDSTARTable":
                hd_rows[name.upper()] = {
                    "name": name, "mag": mag, "commonNames": row[4],
                    "bayer": row[5], "variableId": row[6], "magSource": row[7],
                }
            buckets.setdefault(int(math.floor(dec)), []).append((name, ra, dec, table))
    return hd_rows, buckets


def match_by_position(ra, dec, buckets):
    best, best_sep = None, MATCH_TOLERANCE_DEG
    base = int(math.floor(dec))
    for band in (base - 1, base, base + 1):
        for entry in buckets.get(band, ()):
            sep = angular_separation(ra, dec, entry[1], entry[2])
            if sep < best_sep:
                best_sep, best = sep, entry
    return best


def lacks_magnitude(mag):
    return mag is None or float(mag) in HD_PLACEHOLDER_MAGS


# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cache", help="directory holding IAU-CSN.txt and bsc5.dat / catalog.gz")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true", help="report what would change, write nothing")
    args = parser.parse_args()

    csn_text = fetch_text(CSN_URL, args.cache, ["IAU-CSN.txt"])
    bsc_text = fetch_text(BSC_URL, args.cache, ["bsc5.dat", "catalog", "catalog.gz", "bsc5.gz"], gzipped=True)
    iau = list(parse_csn(csn_text))
    bsc = dict(parse_bsc(bsc_text))
    print(f"  {len(iau)} IAU proper names, {len(bsc)} Bright Star Catalogue entries")

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
            hd_rows, buckets = load_catalogue(conn)

        updates = {}  # designation -> {column: value}

        def stage(designation, column, value):
            updates.setdefault(designation, {})[column] = value

        # ---- IAU proper names (+ their Bayer letters and magnitudes) -------
        names_added = names_present = unmatched = 0
        for entry in iau:
            row = hd_rows.get(f"HD{entry['hd']}") if entry["hd"] else None
            if row is None and entry["ra"] is not None and entry["dec"] is not None:
                hit = match_by_position(entry["ra"], entry["dec"], buckets)
                if hit and hit[3] == "HDSTARTable":
                    row = hd_rows.get(hit[0].upper())
            if row is None:
                unmatched += 1
                continue

            pending = updates.get(row["name"], {})
            merged = merge_name(pending.get("commonNames", row["commonNames"]), entry["name"])
            if merged is None:
                names_present += 1
            else:
                stage(row["name"], "commonNames", merged)
                names_added += 1

            if entry["bayer"] and not row["bayer"]:
                stage(row["name"], "bayer", entry["bayer"])
            if entry["mag"] is not None and lacks_magnitude(row["mag"]):
                stage(row["name"], "V-Mag", entry["mag"])
                stage(row["name"], "magSource", f"IAU-CSN; HD placeholder {row['mag']}")

        # ---- Bright Star Catalogue, for magnitudes HD never recorded -------
        for designation, record in bsc.items():
            row = hd_rows.get(designation.upper())
            if row is None or not lacks_magnitude(row["mag"]):
                continue
            pending = updates.get(row["name"], {})
            if "V-Mag" not in pending:
                stage(row["name"], "V-Mag", record["mag"])
                stage(row["name"], "magSource", f"BSC5; HD placeholder {row['mag']}")
            if record["var"] and not row["variableId"]:
                stage(row["name"], "variableId", record["var"])
            if record["bayer"] and not row["bayer"] and "bayer" not in pending:
                stage(row["name"], "bayer", record["bayer"])

        mags = [(d, u["V-Mag"]) for d, u in updates.items() if "V-Mag" in u]
        print(f"\nMatched IAU names: {names_added} new, {names_present} already present "
              f"(skipped - no duplicates), {unmatched} with no HDSTARTable row")
        print(f"Bayer designations to add: {sum(1 for u in updates.values() if 'bayer' in u)}")
        print(f"Variable-star IDs to add: {sum(1 for u in updates.values() if 'variableId' in u)}")
        print(f"Magnitudes to fill in:     {len(mags)}")
        print(f"Rows touched:              {len(updates)}")

        if args.dry_run:
            print("\n--dry-run: nothing written")
            return 0

        with db.engine.begin() as conn:
            for designation, columns in updates.items():
                assignments = ", ".join(f'"{col}" = :v{i}' for i, col in enumerate(columns))
                params = {f"v{i}": value for i, value in enumerate(columns.values())}
                params["name"] = designation
                conn.execute(text(f'UPDATE "HDSTARTable" SET {assignments} WHERE Name = :name'), params)

        print(f"\nWrote {len(updates)} HDSTARTable rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
