"""In-memory star catalogue used by the star map.

The three catalogue tables (272k Henry Draper stars plus the NGC/IC indexes)
never change at runtime, so we read them once with a column-only query and keep
them as flat typed arrays sorted by magnitude. Every star-map request is then a
slice of those arrays instead of a full ORM scan, and the client can ask for the
data in a compact binary form rather than 25 MB of JSON. Single-object lookups
(star info, search) go through the same arrays via a designation index, so no
request reads the catalogue tables.

The arrays hold float64 so lookups return the database's values exactly; the
binary payload narrows them to float32.

Magnitude handling
------------------
`V-Mag` is NULL for ~49k rows, and HDSTARTable additionally uses the Henry
Draper placeholders 20.0/30.0/40.0/50.0 to mean "no magnitude recorded" (those
rows carry the same placeholder in the photographic magnitude column). Both are
treated as *unknown*, represented as NaN in the binary payload and as null in
JSON, and sorted to the end of the catalogue so a magnitude limit never has to
guess a value for them.

Names and figures
-----------------
Proper names come from the catalogue tables' own name columns (HDSTARTable
`commonNames`, NGC/IC `Common names`), plus the `bayer`, `variableId` and
`magSource` columns that scripts/import_star_names.py adds to HDSTARTable
(ensure_star_catalogue_schema() creates them at startup, before the warm-up).
Constellation figures come from ConstellationsTable/ConstellationLinesTable
(scripts/import_constellations.py), whose lines reference stars by designation.
Both are built together with the arrays, so they share the catalogue's cache
and are picked up on the next server start after an import.

Names in other languages come from ObjectNamesTable (see models/tables.py). A
row there replaces the displayed name for its language and adds search terms
for everyone, so "Sirio" finds Sirius whatever language the visitor uses.
Objects without a row keep their English name. They are read with the rest of
the catalogue, so edits show after a server restart.
"""

import array
import gzip
import math
import os
import re
import struct
import threading
import unicodedata
from bisect import bisect_left, bisect_right

from sqlalchemy import or_

from app.db import db
from models.tables import (ConstellationLinesTable, ConstellationsTable,
                           HDSTARtable, IndexTable, NGCtable, ObjectNamesTable)

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

_lock = threading.RLock()
_catalog = None
_payload_cache = {}

# Per catalogue table: its names column, and extra columns as (record key, column)
_NAME_SOURCES = (
    (HDSTARtable, HDSTARtable.commonNames, (
        ("bayer", HDSTARtable.bayer),
        ("var", HDSTARtable.variableId),
        ("magSource", HDSTARtable.magSource),
    )),
    (NGCtable, NGCtable.Common_names, ()),
    (IndexTable, IndexTable.Common_names, ()),
)


# Designation spellings that should find the same object: "HD 48915", "hd48915",
# "NGC 224", "NGC0224"
_DESIGNATION = re.compile(r"(HD|NGC|IC)0*(\d.*)")


def normalize_designation(text):
    """Index key for a designation: upper case, no spaces, no zero padding."""
    key = str(text).upper().replace(" ", "")
    match = _DESIGNATION.fullmatch(key)
    return match.group(1) + match.group(2) if match else key


def split_aliases(cell):
    """A comma-separated names cell as a list of trimmed aliases."""
    return [part.strip() for part in str(cell or "").split(",") if part.strip()]


def fold(text):
    """Lower case without accents, for matching: "orion" finds "Orión"."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def catalogue_table(designation):
    """The catalogue table a designation comes from, as ObjectNamesTable names it."""
    upper = str(designation or "").upper()
    for prefix, table in (("HD", "HDSTARTable"), ("NGC", "NGCtable"), ("IC", "IndexTable")):
        if upper.startswith(prefix):
            return table
    return None


def is_designation(alias):
    """Catalogue numbers such as 'HD 34029' or 'M31', as opposed to names."""
    upper = alias.upper().replace(" ", "")
    for prefix in ("HD", "NGC", "IC", "M"):
        if upper.startswith(prefix) and upper[len(prefix):].isdigit():
            return True
    return False


def get_star_names():
    """designation -> {name, aliases, bayer?, var?, magSource?, messier?} for
    every named or annotated catalogue object. Shares the catalogue's cache."""
    return get_catalog().name_records


def star_name_record(designation):
    """Name/Bayer/variable record for a catalogue designation, or None."""
    if not designation:
        return None
    return get_star_names().get(designation)


def find_by_proper_name(query):
    """Catalogue designation for a name, matched ignoring case and accents.

    Any alias in any language counts as an exact match ("Toliman" finds the
    star displayed as "Rigil Kentaurus B", "Sirio" finds Sirius); failing that,
    a prefix of a displayed name does ("alpha cen" finds "Alpha Centauri A"),
    and failing that, any alias containing the query ("andromeda" finds
    "Andromeda Galaxy"). Henry Draper stars are tried before NGC, and NGC
    before IC.
    """
    needle = fold(query)
    if not needle:
        return None
    prefix_hit = substring_hit = None
    for designation, names, aliases in get_catalog().search_index:
        if needle in aliases:
            return designation
        if prefix_hit is None and any(name.startswith(needle) for name in names):
            prefix_hit = designation
        if substring_hit is None and any(needle in alias for alias in aliases):
            substring_hit = designation
    return prefix_hit or substring_hit


def localized_name(table, key, lang):
    """An object's name in `lang` from ObjectNamesTable, or None to keep English."""
    entry = get_catalog().translations.get(lang, {}).get((table, key))
    return entry["name"] if entry else None


def display_name(designation, lang):
    """A catalogue object's displayed name in `lang`, falling back to English."""
    record = star_name_record(designation) or {}
    return localized_name(catalogue_table(designation), designation, lang) or record.get("name")


def body_name(key, lang):
    """A solar-system body's name in `lang` ("moon" -> "Luna"), or None."""
    return localized_name("SolarSystem", str(key or "").lower(), lang)


def find_body(query):
    """The ephemeris key ("moon") for a body's name in any language, or None."""
    needle = fold(query)
    if not needle:
        return None
    for names in get_catalog().translations.values():
        for (table, key), entry in names.items():
            if table == "SolarSystem" and needle in [fold(n) for n in [entry["name"], *entry["aliases"]]]:
                return key
    return None


def lookup(query):
    """The object a designation names, in any common spelling ("HD 48915",
    "NGC224", "M31"), as {name, ra, dec, mag, record}. `mag` is None when the
    catalogue has no magnitude; `record` is its name record, possibly empty.
    None when nothing matches."""
    catalog = get_catalog()
    i = catalog.find(query)
    if i is None:
        return None
    name, mag = catalog.names[i], catalog.mag[i]
    return {
        "name": name,
        "ra": catalog.ra[i],
        "dec": catalog.dec[i],
        "mag": None if math.isnan(mag) else mag,
        "record": catalog.name_records.get(name, {}),
    }


def get_constellations(lang=None):
    """Constellation figures with their lines resolved to coordinates, named
    in `lang` where ObjectNamesTable has a name for it."""
    figures = get_catalog().constellations
    if not lang:
        return figures
    named = []
    for figure in figures:
        name = localized_name("ConstellationsTable", figure["id"], lang)
        named.append({**figure, "name": name} if name else figure)
    return named


def _load_name_records():
    """Names and annotations from the catalogue tables' own columns."""
    records = {}
    for table, name_column, extras in _NAME_SOURCES:
        columns = [name_column] + [column for _, column in extras]
        try:
            rows = (db.session.query(table.Name, *columns)
                    .filter(or_(*(column.isnot(None) for column in columns)))
                    .all())
        except Exception as exc:
            print(f"star_catalog: could not read names from {table.__tablename__}: {exc}")
            continue
        for row in rows:
            aliases = split_aliases(row[1])
            names = [a for a in aliases if not is_designation(a)]
            record = {"aliases": aliases}
            if names:
                record["name"] = names[0]
            for (key, _), value in zip(extras, row[2:]):
                if value:
                    record[key] = value
            if len(record) > 1 or aliases:
                records[row[0]] = record
    return records


def _load_translations():
    """lang -> {(ObjectTable, ObjectKey): {name, aliases}} from ObjectNamesTable."""
    translations = {}
    try:
        rows = db.session.query(ObjectNamesTable.ObjectTable, ObjectNamesTable.ObjectKey,
                                ObjectNamesTable.Lang, ObjectNamesTable.Name,
                                ObjectNamesTable.Aliases).all()
    except Exception as exc:
        print(f"star_catalog: could not read ObjectNamesTable: {exc}")
        return translations
    for table, key, lang, name, aliases in rows:
        translations.setdefault(lang, {})[(table, key)] = {"name": name, "aliases": split_aliases(aliases)}
    return translations


def _build_search_index(name_records, translations):
    """[(designation, folded displayed names, folded aliases)] for name search,
    every language's names included, in name_records order (HD, NGC, IC)."""
    extra = {}
    for names in translations.values():
        for (table, key), entry in names.items():
            if table in ("HDSTARTable", "NGCtable", "IndexTable"):
                extra.setdefault(key, []).append(entry)
    index = []
    for designation in list(name_records) + [d for d in extra if d not in name_records]:
        record = name_records.get(designation, {})
        names = [record["name"]] if record.get("name") else []
        aliases = list(record.get("aliases", ()))
        for entry in extra.get(designation, ()):
            names.append(entry["name"])
            aliases += [entry["name"], *entry["aliases"]]
        index.append((designation, [fold(n) for n in names], [fold(a) for a in aliases]))
    return index


def _load_messier():
    """Messier number -> catalogue designation. The numeric `M` column is the
    fuller source; NGCtable's `Messier` text column only fills its gaps."""
    numbers = {}
    for table in (NGCtable, IndexTable):
        try:
            for name, number in db.session.query(table.Name, table.M).filter(table.M.isnot(None)):
                numbers.setdefault(int(number), name)
        except Exception as exc:
            print(f"star_catalog: could not read Messier numbers from {table.__tablename__}: {exc}")
    try:
        for name, label in (db.session.query(NGCtable.Name, NGCtable.Messier)
                            .filter(NGCtable.Messier.isnot(None))):
            digits = str(label).upper().replace(" ", "").lstrip("M")
            if digits.isdigit():
                numbers.setdefault(int(digits), name)
    except Exception as exc:
        print(f"star_catalog: could not read Messier labels from NGCtable: {exc}")
    return numbers


def _load_constellation_rows():
    """(figures, lines) straight from the constellation tables, or empty."""
    try:
        figures = (db.session.query(ConstellationsTable.Abbr, ConstellationsTable.Name,
                                    ConstellationsTable.LabelRA, ConstellationsTable.LabelDEC)
                   .order_by(ConstellationsTable.Name)
                   .all())
        lines = (db.session.query(ConstellationLinesTable.Constellation,
                                  ConstellationLinesTable.StarA, ConstellationLinesTable.StarB)
                 .order_by(ConstellationLinesTable.id)
                 .all())
    except Exception:
        return [], []  # tables not created yet: no figures, not an error
    return figures, lines


def _resolve_constellations(figures, lines, coordinates):
    """Figures in the shape the client draws: lines as [[ra, dec], [ra, dec]]."""
    by_abbr = {}
    for abbr, name, label_ra, label_dec in figures:
        by_abbr[abbr] = {
            "id": abbr,
            "name": name,
            "anchor": None if label_ra is None else [round(label_ra, 4), round(label_dec, 4)],
            "lines": [],
        }
    for abbr, star_a, star_b in lines:
        figure = by_abbr.get(abbr)
        a, b = coordinates.get(star_a), coordinates.get(star_b)
        if figure is None or a is None or b is None:
            continue
        figure["lines"].append([[round(a[0], 5), round(a[1], 5)], [round(b[0], 5), round(b[1], 5)]])
    return [f for f in by_abbr.values() if f["lines"]]


class StarCatalog:
    """Flat, magnitude-sorted view of every catalogue object."""

    __slots__ = ("names", "ra", "dec", "mag", "count", "known_count", "min_mag",
                 "max_mag", "version", "name_records", "constellations", "index",
                 "translations", "search_index")

    def __init__(self, names, ra, dec, mag, known_count, version,
                 name_records=None, constellations=None, messier=None, translations=None):
        self.names = names
        self.ra = ra
        self.dec = dec
        self.mag = mag
        self.count = len(names)
        self.known_count = known_count
        self.min_mag = mag[0] if known_count else -2.0
        self.max_mag = mag[known_count - 1] if known_count else 20.0
        self.version = version
        self.name_records = name_records or {}
        self.constellations = constellations or []

        # normalize_designation(name) -> position. Most names are already
        # normalised; reusing the name string then saves a copy of each.
        self.index = {}
        for i, name in enumerate(names):
            key = normalize_designation(name)
            self.index[name if key == name else key] = i
        for number, designation in (messier or {}).items():
            i = self.index.get(normalize_designation(designation))
            if i is not None:
                self.index[f"M{number}"] = i
                self.name_records.setdefault(designation, {"aliases": []})["messier"] = f"M{number}"
        self.translations = translations or {}
        self.search_index = _build_search_index(self.name_records, self.translations)

    def find(self, query):
        """Position of the object a designation names, or None."""
        if not query:
            return None
        return self.index.get(normalize_designation(query))

    @property
    def unknown_count(self):
        return self.count - self.known_count

    def band_range(self, min_mag, max_mag):
        """Index range [lo, hi) of known-magnitude stars inside a band."""
        if not self.known_count:
            return 0, 0
        lo = bisect_left(self.mag, min_mag - 1e-9, 0, self.known_count)
        hi = bisect_right(self.mag, max_mag + 1e-9, 0, self.known_count)
        return lo, max(lo, hi)


def _catalog_version():
    """Fingerprint of the database file when the catalogue was built, used for
    ETags so browsers refetch after a restart that follows an import."""
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
    figures, figure_lines = _load_constellation_rows()
    figure_stars = {star for _, a, b in figure_lines for star in (a, b)}
    figure_coordinates = {}

    known = []
    unknown = []
    for table, placeholders in (
        (HDSTARtable, HD_PLACEHOLDER_MAGS),
        (IndexTable, frozenset()),
        (NGCtable, frozenset()),
    ):
        try:
            for mag, name, ra, dec in _rows_from_table(table, placeholders):
                if mag is None:
                    unknown.append((name, ra, dec))
                else:
                    known.append((mag, name, ra, dec))
                if name in figure_stars:
                    figure_coordinates[name] = (ra, dec)
        except Exception as exc:  # a missing/renamed table must not break the map
            print(f"star_catalog: failed to read {getattr(table, '__tablename__', table)}: {exc}")

    known.sort(key=lambda row: row[0])

    total = len(known) + len(unknown)
    names = [""] * total
    ra_arr = array.array("d", bytes(8 * total))
    dec_arr = array.array("d", bytes(8 * total))
    mag_arr = array.array("d", bytes(8 * total))

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

    return StarCatalog(
        names, ra_arr, dec_arr, mag_arr, len(known), _catalog_version(),
        name_records=_load_name_records(),
        constellations=_resolve_constellations(figures, figure_lines, figure_coordinates),
        messier=_load_messier(),
        translations=_load_translations(),
    )


def get_catalog(force_reload=False):
    """Catalogue singleton; built on first use and reused afterwards.

    Deliberately not rebuilt when Data.db changes on disk: request logging
    writes to it on every request, which made every search and band request
    pay for a full ~1.7 s rebuild. The catalogue tables only change through
    the offline import scripts, so restart the server (or pass force_reload)
    after running one.
    """
    global _catalog
    with _lock:
        if force_reload or _catalog is None:
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
            out += array.array("f", piece).tobytes()  # float64 -> wire float32
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
