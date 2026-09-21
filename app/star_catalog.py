"""In-memory star catalogue used by the star map.

The three catalogue tables (272k Henry Draper stars plus the NGC/IC indexes)
never change at runtime, so we read them once with a column-only query and keep
them as flat typed arrays sorted by magnitude. Every star-map request is then a
slice of those arrays instead of a full ORM scan, and the client can ask for the
data in a compact binary form rather than 25 MB of JSON.

Magnitude handling
------------------
`V-Mag` is NULL for ~49k rows, and HDSTARTable additionally uses the Henry
Draper placeholders 20.0/30.0/40.0/50.0 to mean "no magnitude recorded" (those
rows carry the same placeholder in the photographic magnitude column). Both are
treated as *unknown*, represented as NaN in the binary payload and as null in
JSON, and sorted to the end of the catalogue so a magnitude limit never has to
guess a value for them.

Where a magnitude is missing we first consult static/data/star_names.json, an
overlay built by scripts/build_star_names.py from the IAU star name catalogue
and the Yale Bright Star Catalogue. That recovers the 103 naked-eye stars whose
magnitude the Henry Draper catalogue never recorded - all of them variables,
Algol and Delta Cephei among them - which would otherwise be undrawable. The
overlay only ever fills a gap; it never overrides a magnitude the database has.
It also carries the proper names, which the database itself has for only 40
stars.
"""

import array
import gzip
import json
import math
import os
import struct
import threading
from bisect import bisect_left, bisect_right

from models.tables import HDSTARtable, IndexTable, NGCtable

# Magnitudes HDSTARTable uses as "not recorded" placeholders. IndexTable and
# NGCtable magnitudes are genuine all the way out to ~20.4, so the substitution
# is applied to the Henry Draper table only.
HD_PLACEHOLDER_MAGS = frozenset((20.0, 30.0, 40.0, 50.0))

# Binary payload: 20-byte header, then three float32 columns, then the names
# blob. The header length is a multiple of 4 so the client can wrap the columns
# in Float32Arrays without copying.
_MAGIC = b"SMAP"
_FORMAT_VERSION = 1
_HEADER = struct.Struct("<4sHHIII")  # magic, version, flags, count, namesLen, knownCount
FLAG_HAS_NAMES = 1 << 0

_MAX_CACHED_PAYLOADS = 24

# Proper names and fallback magnitudes; see scripts/build_star_names.py
_NAMES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "data", "star_names.json",
)

_lock = threading.RLock()
_catalog = None
_payload_cache = {}
_star_names = None


def get_star_names():
    """designation -> {name, bayer?, var?, mag?}, loaded once."""
    global _star_names
    if _star_names is None:
        try:
            with open(_NAMES_PATH, encoding="utf-8") as handle:
                _star_names = json.load(handle).get("stars", {})
        except (OSError, ValueError) as exc:
            print(f"star_catalog: no star name overlay ({exc})")
            _star_names = {}
    return _star_names


def star_name_record(designation):
    """Proper name/Bayer/variable record for a catalogue designation, or None."""
    if not designation:
        return None
    return get_star_names().get(designation)


def find_by_proper_name(query):
    """Catalogue designation for a proper name, matched case-insensitively.

    Falls back to a prefix match so "alpha cen" finds "Alpha Centauri A".
    """
    if not query:
        return None
    needle = query.strip().lower()
    if not needle:
        return None
    names = get_star_names()
    prefix_hit = None
    for designation, record in names.items():
        proper = (record.get("name") or "").lower()
        if not proper:
            continue
        if proper == needle:
            return designation
        if prefix_hit is None and proper.startswith(needle):
            prefix_hit = designation
    return prefix_hit


class StarCatalog:
    """Flat, magnitude-sorted view of every catalogue object."""

    __slots__ = ("names", "ra", "dec", "mag", "count", "known_count", "known_mags",
                 "min_mag", "max_mag", "version")

    def __init__(self, names, ra, dec, mag, known_count, version):
        self.names = names
        self.ra = ra
        self.dec = dec
        self.mag = mag
        self.count = len(names)
        self.known_count = known_count
        # Plain list of the known magnitudes, for bisect() band lookups
        self.known_mags = list(mag[:known_count])
        self.min_mag = self.known_mags[0] if known_count else -2.0
        self.max_mag = self.known_mags[-1] if known_count else 20.0
        self.version = version

    @property
    def unknown_count(self):
        return self.count - self.known_count

    def band_range(self, min_mag, max_mag):
        """Index range [lo, hi) of known-magnitude stars inside a band."""
        if not self.known_count:
            return 0, 0
        lo = bisect_left(self.known_mags, min_mag - 1e-9)
        hi = bisect_right(self.known_mags, max_mag + 1e-9)
        return lo, max(lo, hi)


def _catalog_version():
    """Fingerprint of the database file, used for ETags and cache keys."""
    try:
        from Server import app  # imported lazily: Server imports this package
        uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    except Exception:
        uri = ""
    path = uri[len("sqlite:///"):] if uri.startswith("sqlite:///") else ""
    try:
        st = os.stat(path)
        return f"{int(st.st_mtime)}-{st.st_size}-{_FORMAT_VERSION}"
    except OSError:
        return f"unknown-{_FORMAT_VERSION}"


def _rows_from_table(table, placeholders):
    """Yield (mag_or_None, name, ra, dec) using a column-only streaming query."""
    from app.db import db

    query = db.session.query(table.Name, table.RA, table.DEC, table.V_Mag)
    for name, ra_val, dec_val, mag_val in query.yield_per(20000):
        if name is None or ra_val is None or dec_val is None:
            continue
        try:
            ra = float(ra_val)
            dec = float(dec_val)
        except (TypeError, ValueError):
            continue
        if mag_val is None:
            mag = None
        else:
            try:
                mag = float(mag_val)
            except (TypeError, ValueError):
                mag = None
            else:
                if mag in placeholders or not math.isfinite(mag):
                    mag = None
        yield mag, name, ra, dec


def build_catalog():
    """Read all three tables into magnitude-sorted arrays (a few hundred ms)."""
    names_overlay = get_star_names()
    known = []
    unknown = []
    recovered = 0
    for table, placeholders in (
        (HDSTARtable, HD_PLACEHOLDER_MAGS),
        (IndexTable, frozenset()),
        (NGCtable, frozenset()),
    ):
        try:
            for mag, name, ra, dec in _rows_from_table(table, placeholders):
                if mag is None:
                    # The catalogue has no magnitude: fall back to the overlay
                    # before writing the object off as unplottable.
                    overlay = names_overlay.get(name)
                    fallback = overlay.get("mag") if overlay else None
                    if fallback is not None:
                        known.append((float(fallback), name, ra, dec))
                        recovered += 1
                    else:
                        unknown.append((name, ra, dec))
                else:
                    known.append((mag, name, ra, dec))
        except Exception as exc:  # a missing/renamed table must not break the map
            print(f"star_catalog: failed to read {getattr(table, '__tablename__', table)}: {exc}")

    if recovered:
        print(f"star_catalog: recovered magnitudes for {recovered} stars from the name overlay")

    known.sort(key=lambda row: row[0])

    total = len(known) + len(unknown)
    names = [""] * total
    ra_arr = array.array("f", bytes(4 * total))
    dec_arr = array.array("f", bytes(4 * total))
    mag_arr = array.array("f", bytes(4 * total))

    for i, (mag, name, ra, dec) in enumerate(known):
        names[i] = name
        ra_arr[i] = ra
        dec_arr[i] = dec
        mag_arr[i] = mag
    nan = float("nan")
    for j, (name, ra, dec) in enumerate(unknown, start=len(known)):
        names[j] = name
        ra_arr[j] = ra
        dec_arr[j] = dec
        mag_arr[j] = nan

    return StarCatalog(names, ra_arr, dec_arr, mag_arr, len(known), _catalog_version())


def get_catalog(force_reload=False):
    """Catalogue singleton; built on first use and reused afterwards."""
    global _catalog
    with _lock:
        if force_reload or _catalog is None or _catalog.version != _catalog_version():
            _catalog = build_catalog()
            _payload_cache.clear()
        return _catalog


def warm_cache():
    """Build the catalogue ahead of the first request (called at startup)."""
    catalog = get_catalog()
    return catalog.count


def encode_binary(catalog, lo, hi, include_unknown=False, include_names=True):
    """Pack a slice of the catalogue into the compact binary wire format."""
    pieces_ra = [catalog.ra[lo:hi]]
    pieces_dec = [catalog.dec[lo:hi]]
    pieces_mag = [catalog.mag[lo:hi]]
    name_slices = [catalog.names[lo:hi]]
    count = hi - lo

    if include_unknown and catalog.unknown_count:
        u0, u1 = catalog.known_count, catalog.count
        pieces_ra.append(catalog.ra[u0:u1])
        pieces_dec.append(catalog.dec[u0:u1])
        pieces_mag.append(catalog.mag[u0:u1])
        name_slices.append(catalog.names[u0:u1])
        count += u1 - u0

    if include_names:
        names_blob = "\n".join(name for chunk in name_slices for name in chunk).encode("utf-8")
    else:
        names_blob = b""

    flags = FLAG_HAS_NAMES if include_names else 0
    out = bytearray()
    out += _HEADER.pack(_MAGIC, _FORMAT_VERSION, flags, count, len(names_blob), hi - lo)
    for group in (pieces_ra, pieces_dec, pieces_mag):
        for piece in group:
            out += piece.tobytes()
    out += names_blob
    return bytes(out)


def cached_payload(key, builder, compress=False):
    """Memoise an encoded payload (and its gzipped twin) by request key."""
    cache_key = (key, bool(compress))
    with _lock:
        hit = _payload_cache.get(cache_key)
    if hit is not None:
        return hit

    raw = builder()
    if compress:
        raw = gzip.compress(raw, 5)
    with _lock:
        if len(_payload_cache) >= _MAX_CACHED_PAYLOADS:
            _payload_cache.clear()
        _payload_cache[cache_key] = raw
    return raw


def to_json_rows(catalog, lo, hi, include_unknown=False, limit=None):
    """Legacy JSON shape: a list of {name, ra, dec, mag, type} dicts."""
    rows = []
    names, ra, dec, mag = catalog.names, catalog.ra, catalog.dec, catalog.mag
    for i in range(lo, hi):
        rows.append({
            "name": names[i],
            "ra": ra[i],
            "dec": dec[i],
            "mag": mag[i],
            "type": "star",
        })
        if limit and len(rows) >= limit:
            return rows
    if include_unknown:
        for i in range(catalog.known_count, catalog.count):
            rows.append({
                "name": names[i],
                "ra": ra[i],
                "dec": dec[i],
                "mag": None,
                "magUnknown": True,
                "type": "star",
            })
            if limit and len(rows) >= limit:
                break
    return rows
