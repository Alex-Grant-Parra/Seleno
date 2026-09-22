"""
Physical and astronomical constants for V3 Helios.

Every module imports from here so each value has exactly one definition.
Tunable model settings (timestep, table span, observer location) live in the
modules that use them, not here.
"""

import numpy as np

# ---------------------------------------------------------------------------
# Fundamental
# ---------------------------------------------------------------------------
# G is only known to ~5 significant figures.  The simulation is driven by the
# DE440 GM values below (known to ~11): masses are derived as GM / G and the
# force model multiplies back by G, so the value of G cancels out exactly.
G = 6.67430e-11                 # m^3 kg^-1 s^-2 (CODATA 2018)
C_LIGHT = 2.99792458e8          # m/s (exact)

# ---------------------------------------------------------------------------
# Units and time
# ---------------------------------------------------------------------------
METERS_PER_KM = 1000.0
AU_M = 149597870700.0           # m (IAU 2012, exact)
SECONDS_PER_DAY = 86400.0
DAYS_PER_JULIAN_YEAR = 365.25
DAYS_PER_JULIAN_CENTURY = 36525.0
JD_J2000 = 2451545.0            # Julian date of J2000.0 (2000-01-01 12:00 TT)
JD_UNIX_EPOCH = 2440587.5       # Julian date of 1970-01-01 00:00 UTC

# ---------------------------------------------------------------------------
# Gravitational parameters GM (m^3/s^2), DE440 (gm_de440.tpc).
# Mars..Pluto are planet-system values (planet + moons), matching the
# system-barycentre states written by loader.py.  loader.py also stores GM
# in initial_conditions.json; this table is the fallback.
# ---------------------------------------------------------------------------
DE440_GM = {
    "sun": 1.3271244004127939e20,
    "mercury": 2.2031868551400003e13,
    "venus": 3.24858592e14,
    "earth": 3.986004355070226e14,
    "moon": 4.902800118457549e12,
    "mars": 4.2828375815756095e13,
    "jupiter": 1.267127641e17,
    "saturn": 3.794058484179999e16,
    "uranus": 5.794556399999998e15,
    "neptune": 6.836527100580398e15,
    "pluto": 9.755e11,
}

# ---------------------------------------------------------------------------
# Earth orientation and figure
# ---------------------------------------------------------------------------
# Obliquity of the ecliptic at J2000, as used to define SPICE's ECLIPJ2000.
OBLIQUITY_J2000_RAD = np.deg2rad(84381.448 / 3600.0)

# Earth's equatorial bulge (J2) and reference radius, as used by DE440.
EARTH_J2 = 1.08262545e-3
EARTH_RADIUS_M = 6378136.3

# J2000 mean equator pole expressed in ECLIPJ2000.  It precesses only ~20"/yr,
# which is negligible for the J2 force over decades.
EARTH_POLE_ECLIPJ2000 = np.array([0.0, np.sin(OBLIQUITY_J2000_RAD), np.cos(OBLIQUITY_J2000_RAD)])

# WGS84 ellipsoid, for converting an observer's latitude/longitude/height to
# a position.  (A geodetic datum, deliberately distinct from EARTH_RADIUS_M.)
WGS84_A_M = 6378137.0
WGS84_F = 1.0 / 298.257223563

# ---------------------------------------------------------------------------
# Photometry
# ---------------------------------------------------------------------------
MOON_MEAN_DISTANCE_KM = 384400.0    # reference distance for lunar magnitude
