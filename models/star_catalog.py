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

Search
------
search() scores a query against every name in every language (see the "Name
search" section): exact names first, then the start of a name, its words in
any order, part of it, and finally close spellings ("Betelguese"). Catalogue
numbers only ever match exactly. find_by_proper_name() is the strict version,
without close spellings, for resolving telescope targets.
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


# ---------------------------------------------------------------------------
# Name search
#
# One index holds every name an object answers to, in every language: the
# catalogue tables' names and aliases, ObjectNamesTable's names, the planets'
# and the constellations'. A query is scored against each name (exact, start
# of the name, start of its words in any order, part of it, then a close
# spelling), and each object keeps its best score. Nothing here is specific to
# a language: a new language only needs its rows in ObjectNamesTable (and,
# optionally, its small words in SEARCH_STOPWORDS).
#
# Catalogue numbers (HD 48915, NGC 224, IC 434, M31) never take part: they
# only ever match exactly, through lookup(), so "M13" can't turn into M31.
# ---------------------------------------------------------------------------

# English names of the bodies the ephemeris knows, by its keys
SOLAR_SYSTEM = {"sun": "Sun", "moon": "Moon", "mercury": "Mercury", "venus": "Venus",
                "mars": "Mars", "jupiter": "Jupiter", "saturn": "Saturn", "uranus": "Uranus",
                "neptune": "Neptune", "pluto": "Pluto"}

# Rough brightness, used only to rank the bodies among equally good matches
_BODY_MAGS = {"sun": -26.7, "moon": -12.7, "venus": -4.4, "jupiter": -2.5, "mars": -1.5,
              "mercury": -0.5, "saturn": 0.5, "uranus": 5.7, "neptune": 7.8, "pluto": 14.4}
_CONSTELLATION_MAG = 0.0

# Small words that don't have to match ("galaxia andromeda" finds "Galaxia de
# Andrómeda"). Every language's list applies to every query, since people
# search in more than one language.
SEARCH_STOPWORDS = {
    "en": {"the", "of", "a"},
    "es": {"el", "la", "los", "las", "de", "del", "y"},
}
_STOPWORDS = frozenset(word for words in SEARCH_STOPWORDS.values() for word in words)

# Match kinds, best first, with their scores. A close spelling scores
# SCORE_FUZZY, less FUZZY_STEP for each letter changed and FUZZY_EXTRA_WORD
# for each word of the name the query left out, so "jupter" is Jupiter rather
# than Jupiter's Ghost Nebula.
SCORES = {"exact": 100, "prefix": 90, "words": 80, "substring": 70}
SCORE_FUZZY = 60
FUZZY_STEP = 5
FUZZY_EXTRA_WORD = 2

# Queries that are a catalogue number, in any spelling: "M31", "m 31",
# "Messier 31", "NGC0224", "hd 48915", "IC434"
_NUMBER_QUERY = re.compile(r"(hd|ngc|ic|m|messier)\s*-?\s*0*(\d+)\s*([a-z]?)")


def search_text(text):
    """A name as the search compares it: folded, apostrophes dropped, other
    punctuation as spaces ("Barnard's Star" -> "barnards star")."""
    folded = re.sub(r"['’`]", "", fold(text))
    return " ".join(re.sub(r"[\W_]+", " ", folded).split())


def _content_words(text):
    words = text.split()
    return [w for w in words if w not in _STOPWORDS] or words


def catalogue_number(query):
    """The designation lookup() takes for a catalogue-number query
    ("Messier 31" -> "M31"), or None when the query is a name."""
    match = _NUMBER_QUERY.fullmatch(fold(query))
    if not match:
        return None
    prefix = "M" if match.group(1) in ("m", "messier") else match.group(1).upper()
    return f"{prefix}{match.group(2)}{match.group(3).upper()}"


def allowed_typos(word):
    """Letters a query word may get wrong: none up to 4 letters, then 1, then 2
    from 9. Words with digits ("47", "61") must be exact."""
    if any(c.isdigit() for c in word):
        return 0
    n = len(word)
    return 0 if n <= 4 else 1 if n <= 8 else 2


def edit_distance(a, b, limit):
    """Letters changed, added, removed or swapped (two neighbours) to turn `a`
    into `b`; anything over `limit` comes back as limit + 1."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    if a == b:
        return 0
    before, row = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            best = min(row[j] + 1, current[j - 1] + 1, row[j - 1] + cost)
            if (before is not None and i > 1 and j > 1
                    and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]):
                best = min(best, before[j - 2] + 1)
            current[j] = best
        if min(current) > limit:
            return limit + 1
        before, row = row, current
    return min(row[-1], limit + 1)


class SearchIndex:
    """Every searchable name, plus each word's spellings for typo matching."""

    __slots__ = ("entries", "words_by_length")

    def __init__(self, entries):
        # entries: [(kind, key, original name, lang, rank)]; rank orders
        # equally good matches (brighter first)
        self.entries = []
        seen = set()
        vocabulary = set()
        for order, (kind, key, name, lang, rank) in enumerate(entries):
            text = search_text(name)
            if not text or (kind, key, text, lang) in seen:
                continue
            seen.add((kind, key, text, lang))
            words = _content_words(text)
            vocabulary.update(words)
            self.entries.append((kind, key, name, lang, rank, order, text, tuple(words)))
        self.words_by_length = {}
        for word in vocabulary:
            self.words_by_length.setdefault(len(word), []).append(word)

    def close_words(self, word):
        """{indexed word: letters changed} for the words within reach of `word`."""
        limit = allowed_typos(word)
        close = {}
        if not limit:
            return close
        for length in range(len(word) - limit, len(word) + limit + 1):
            for candidate in self.words_by_length.get(length, ()):
                d = edit_distance(word, candidate, limit)
                if d <= limit:
                    close[candidate] = d
        return close


def _match_words(query_words, words, test):
    """Pair each query word with a different word of the name; the sum of
    test()'s costs, or None if some query word has no partner."""
    used = set()
    total = 0
    for q in query_words:
        best = None
        for j, word in enumerate(words):
            if j in used:
                continue
            cost = test(q, word)
            if cost is not None and (best is None or cost < best[1]):
                best = (j, cost)
                if cost == 0:
                    break
        if best is None:
            return None
        used.add(best[0])
        total += best[1]
    return total


def _starts(q, word):
    if any(c.isdigit() for c in q):
        return 0 if q == word else None
    return 0 if word.startswith(q) else None


def _score(query, query_words, close, text, words, fuzzy):
    """(score, kind of match) for one indexed name, or None."""
    if text == query:
        return SCORES["exact"], "exact"
    # A number at the end of the query is a whole number: "caldwell 1" is
    # not the start of "caldwell 14"
    numeric = query[-1].isdigit()
    if text.startswith(query + " " if numeric else query):
        return SCORES["prefix"], "prefix"
    if _match_words(query_words, words, _starts) is not None:
        return SCORES["words"], "words"
    if len(query) >= 3 and (f" {query} " in f" {text} " if numeric else query in text):
        return SCORES["substring"], "substring"
    if not fuzzy:
        return None

    def near(q, word):
        if _starts(q, word) is not None:
            return 0
        return close[q].get(word)

    typos = _match_words(query_words, words, near)
    if typos:
        extra = max(0, len(words) - len(query_words))
        return SCORE_FUZZY - FUZZY_STEP * typos - FUZZY_EXTRA_WORD * extra, "fuzzy"
    return None


def object_label(kind, key, lang):
    """An object's displayed name in `lang`: a catalogue designation's name
    (or the designation itself), a body's ("moon" -> "Luna"), a constellation's."""
    if kind == "body":
        return body_name(key, lang) or SOLAR_SYSTEM.get(key, str(key).capitalize())
    if kind == "constellation":
        name = localized_name("ConstellationsTable", key, lang)
        if name:
            return name
        for figure in get_catalog().constellations:
            if figure["id"] == key:
                return figure["name"]
        return key
    return display_name(key, lang) or key


def search(query, lang=None, limit=8, fuzzy=True, kinds=None):
    """The objects a name points to, best first, as dicts:

        kind      "catalog", "body" or "constellation"
        key       the designation (HD48915), ephemeris key (moon) or abbreviation (Ori)
        name      the object's displayed name in `lang`
        matched   the name the query matched, in whatever language it is
        match     number (a catalogue number) | exact | prefix | words | substring | fuzzy
        score     as in SCORES; a close spelling scores less per typo

    A catalogue-number query only ever returns that object (or nothing).
    Equally good matches put names in `lang` first, then brighter objects.
    `fuzzy=False` leaves out close spellings; `kinds` limits the kinds searched.
    """
    lang = lang or "en"
    designation = catalogue_number(query)
    if designation:
        catalog = get_catalog()
        i = catalog.find(designation)
        if i is None or (kinds and "catalog" not in kinds):
            return []
        key = catalog.names[i]
        return [{"kind": "catalog", "key": key, "name": object_label("catalog", key, lang),
                 "matched": designation, "match": "number", "score": SCORES["exact"]}]

    text = search_text(query)
    if not text:
        return []
    index = get_catalog().search_index
    query_words = _content_words(text)
    close = {q: index.close_words(q) for q in query_words} if fuzzy else {}

    best = {}
    for kind, key, name, entry_lang, rank, order, entry_text, words in index.entries:
        if kinds and kind not in kinds:
            continue
        scored = _score(text, query_words, close, entry_text, words, fuzzy)
        if scored is None:
            continue
        score, match = scored
        sort_key = (-score, 0 if entry_lang == lang else 1, rank, order)
        current = best.get((kind, key))
        if current is None or sort_key < current[0]:
            best[(kind, key)] = (sort_key, name, match, score)

    ranked = sorted(best.items(), key=lambda item: item[1][0])[:limit]
    return [{"kind": kind, "key": key, "name": object_label(kind, key, lang),
             "matched": name, "match": match, "score": score}
            for (kind, key), (_sort, name, match, score) in ranked]


def find_by_proper_name(query):
    """Catalogue designation for a name, matched ignoring case and accents, or
    None. Strict: no close spellings, because this resolves telescope targets."""
    found = search(query, fuzzy=False, kinds=("catalog",), limit=1)
    return found[0]["key"] if found else None


def find_body(query):
    """The ephemeris key ("moon") for a body's exact name in any language, or None."""
    found = search(query, fuzzy=False, kinds=("body",), limit=1)
    return found[0]["key"] if found and found[0]["match"] == "exact" else None


def constellation_position(key):
    """(ra, dec) in degrees where a constellation's name is drawn, or None."""
    for figure in get_catalog().constellations:
        if figure["id"] != key:
            continue
        if figure.get("anchor"):
            return tuple(figure["anchor"])
        # No label position: the middle of its stars, averaged as vectors so
        # figures crossing RA 0 don't land on the far side of the sky
        x = y = z = 0.0
        for line in figure["lines"]:
            for ra, dec in line:
                r, d = math.radians(ra), math.radians(dec)
                x += math.cos(d) * math.cos(r)
                y += math.cos(d) * math.sin(r)
                z += math.sin(d)
        if not (x or y or z):
            return None
        return (math.degrees(math.atan2(y, x)) % 360.0,
                math.degrees(math.atan2(z, math.hypot(x, y))))
    return None


def _search_entries(names, mag, index, name_records, translations, constellations):
    """(kind, key, name, lang, rank) for every name SearchIndex should hold."""
    def catalog_rank(designation):
        i = index.get(normalize_designation(designation))
        value = mag[i] if i is not None else float("nan")
        return 99.0 if value != value else value

    entries = []
    for designation, record in name_records.items():
        rank = catalog_rank(designation)
        for alias in record.get("aliases", ()):
            if not is_designation(alias):
                entries.append(("catalog", designation, alias, "en", rank))
    for key, name in SOLAR_SYSTEM.items():
        entries.append(("body", key, name, "en", _BODY_MAGS.get(key, 30.0)))
    for figure in constellations:
        entries.append(("constellation", figure["id"], figure["name"], "en", _CONSTELLATION_MAG))
    shown = {figure["id"] for figure in constellations}
    for lang, by_object in translations.items():
        for (table, key), entry in by_object.items():
            if table == "SolarSystem":
                kind, rank = "body", _BODY_MAGS.get(key, 30.0)
            elif table == "ConstellationsTable":
                if key not in shown:
                    continue
                kind, rank = "constellation", _CONSTELLATION_MAG
            else:
                kind, rank = "catalog", catalog_rank(key)
            for name in [entry["name"], *entry["aliases"]]:
                if not is_designation(name):
                    entries.append((kind, key, name, lang, rank))
    return entries


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
        self.search_index = SearchIndex(_search_entries(
            names, mag, self.index, self.name_records, self.translations, self.constellations))

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
