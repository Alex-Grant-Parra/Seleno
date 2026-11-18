"""
Dwarf planets and bright asteroids that V3 Helios follows besides the planets.

They are integrated as test particles: they feel the Sun, the planets and the
Moon, but have no mass of their own (GM = 0), so adding them leaves every
other body's trajectory exactly as it was.  Even Ceres, the heaviest, would
shift Mars by well under a kilometre over decades, so this costs nothing in
accuracy for the planets.  The asteroids miss each other's small pulls, which
is the main error for them (arcseconds over years).

Their states at the epoch come from JPL Horizons (loader.py --small-bodies),
and their brightness from the H, G magnitude system with H and G from JPL's
small-body database; both are written to initial_conditions.json.

Plain data, no imports: the server's name search reads it too.
"""

# key -> (displayed name, Horizons record, kind, where it orbits)
#   kind     : dwarf_planet (the IAU's five, besides Pluto), asteroid or tno
#              (a trans-Neptunian object); the star map shows each separately.
#   region   : main_belt, kuiper_belt, scattered_disc or detached
SMALL_BODIES = {
    # Main asteroid belt: Ceres and the brightest asteroids (Vesta reaches mag 5)
    "ceres": ("Ceres", "1", "dwarf_planet", "main_belt"),
    "pallas": ("Pallas", "2", "asteroid", "main_belt"),
    "juno": ("Juno", "3", "asteroid", "main_belt"),
    "vesta": ("Vesta", "4", "asteroid", "main_belt"),
    "hebe": ("Hebe", "6", "asteroid", "main_belt"),
    "iris": ("Iris", "7", "asteroid", "main_belt"),
    "hygiea": ("Hygiea", "10", "asteroid", "main_belt"),
    # Kuiper belt
    "haumea": ("Haumea", "136108", "dwarf_planet", "kuiper_belt"),
    "makemake": ("Makemake", "136472", "dwarf_planet", "kuiper_belt"),
    "quaoar": ("Quaoar", "50000", "tno", "kuiper_belt"),
    "orcus": ("Orcus", "90482", "tno", "kuiper_belt"),
    "varuna": ("Varuna", "20000", "tno", "kuiper_belt"),
    # Beyond it: the scattered disc, and Sedna's detached orbit
    "eris": ("Eris", "136199", "dwarf_planet", "scattered_disc"),
    "gonggong": ("Gonggong", "225088", "tno", "scattered_disc"),
    "sedna": ("Sedna", "90377", "tno", "detached"),
}

# Slope parameter when the small-body database has none (the IAU default)
DEFAULT_G = 0.15


def kind_of(key):
    entry = SMALL_BODIES.get(str(key).lower())
    return entry[2] if entry else None
