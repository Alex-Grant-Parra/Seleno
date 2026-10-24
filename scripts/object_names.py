"""List and check the sky-object names in other languages (ObjectNamesTable).

    python scripts/object_names.py list es    # every Spanish name, with its English one
    python scripts/object_names.py check      # rows that name no object; coverage per language

English names stay in the catalogue tables' own columns; a row in
ObjectNamesTable gives an object its name in one language (see the comment on
ObjectNamesTable in models/tables.py). Edit rows directly in instance/Data.db,
then restart the server: the star map reads them with the rest of the catalogue.
"""
import argparse
import os
import sqlite3
import sys

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DB_PATH = os.path.join(BASE_DIR, "instance", "Data.db")

# The ephemeris' names for the bodies the SolarSystem rows can name
SOLAR_SYSTEM = {"sun": "Sun", "moon": "Moon", "mercury": "Mercury", "venus": "Venus", "mars": "Mars",
                "jupiter": "Jupiter", "saturn": "Saturn", "uranus": "Uranus", "neptune": "Neptune",
                "pluto": "Pluto"}


def is_designation(alias):
    upper = alias.upper().replace(" ", "")
    return any(upper.startswith(p) and upper[len(p):].isdigit() for p in ("HD", "NGC", "IC", "M"))


def english_names(conn):
    """(ObjectTable, ObjectKey) -> English displayed name, for everything that has one."""
    names = {}
    for table, column in (("HDSTARTable", "commonNames"), ("NGCtable", "Common names"),
                          ("IndexTable", "Common names")):
        for key, cell in conn.execute(f'SELECT Name, "{column}" FROM "{table}" '
                                      f'WHERE "{column}" IS NOT NULL AND "{column}" != ""'):
            proper = [a.strip() for a in cell.split(",") if a.strip() and not is_designation(a.strip())]
            if proper:
                names[(table, key)] = proper[0]
    for abbr, name in conn.execute('SELECT Abbr, Name FROM "ConstellationsTable"'):
        names[("ConstellationsTable", abbr)] = name
    for key, name in SOLAR_SYSTEM.items():
        names[("SolarSystem", key)] = name
    return names


def all_keys(conn):
    """Every key each linked table has, named or not."""
    keys = {"SolarSystem": set(SOLAR_SYSTEM)}
    for table, column in (("HDSTARTable", "Name"), ("NGCtable", "Name"), ("IndexTable", "Name"),
                          ("ConstellationsTable", "Abbr")):
        keys[table] = {row[0] for row in conn.execute(f'SELECT "{column}" FROM "{table}"')}
    return keys


def cmd_list(conn, lang):
    english = english_names(conn)
    rows = conn.execute('SELECT id, ObjectTable, ObjectKey, Name, Aliases FROM "ObjectNamesTable" '
                        'WHERE Lang = ? ORDER BY ObjectTable, ObjectKey', (lang,)).fetchall()
    for id_, table, key, name, aliases in rows:
        en = english.get((table, key), "-")
        extra = f"  [also: {aliases}]" if aliases else ""
        print(f"{id_:>5} {table:<19} {key:<13} {en:<34} -> {name}{extra}")
    print(f"{len(rows)} rows")


def cmd_check(conn):
    keys = all_keys(conn)
    english = english_names(conn)
    rows = conn.execute('SELECT id, ObjectTable, ObjectKey, Lang, Name FROM "ObjectNamesTable"').fetchall()
    orphans = [r for r in rows if r[2] not in keys.get(r[1], ())]
    for id_, table, key, lang, name in orphans:
        print(f"row {id_}: {table} has no {key!r} ({lang} {name!r})")
    langs = sorted({r[3] for r in rows})
    for lang in langs:
        mine = [r for r in rows if r[3] == lang]
        named = {(r[1], r[2]) for r in mine}
        same = len(english) - len(named & set(english))
        print(f"{lang}: {len(mine)} names; {same} named objects keep their English name")
    return not orphans


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DB_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list")
    p_list.add_argument("lang")
    sub.add_parser("check")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'ObjectNamesTable'").fetchone():
        sys.exit("No ObjectNamesTable yet: start the server once to create it.")
    if args.cmd == "list":
        cmd_list(conn, args.lang)
    elif not cmd_check(conn):
        sys.exit(1)


if __name__ == "__main__":
    main()
