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
SECONDS_PER_JULIAN_CENTURY = SECONDS_PER_DAY * DAYS_PER_JULIAN_CENTURY
JD_J2000 = 2451545.0            # Julian date of J2000.0 (2000-01-01 12:00 TT)
JD_UNIX_EPOCH = 2440587.5       # Julian date of 1970-01-01 00:00 UTC
TT_MINUS_TAI_S = 32.184         # exact, by definition of TT
ARCSEC_TO_RAD = np.pi / (180.0 * 3600.0)

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

# IAU 2006 precession (Capitaine et al. 2003), polynomial coefficients in
# arcsec for T^0..T^5, T in Julian centuries of TT since J2000:
#   psi_A   luni-solar precession along the J2000 ecliptic
#   omega_A obliquity of the mean equator of date on the J2000 ecliptic,
#           as an offset from OBLIQUITY_J2000_RAD
#   chi_A   planetary precession along the equator of date
# The pole of the Earth moves ~11 degrees over 2000 years, which matters for
# the J2 and tidal forces and for mean-of-date coordinates.
PRECESSION_PSI_A_ARCSEC = (0.0, 5038.481507, -1.0790069, -0.00114045, 0.000132851, -0.0000000951)
PRECESSION_OMEGA_A_ARCSEC = (0.0, -0.025754, 0.0512623, -0.00772503, -0.000000467, 0.0000003337)
PRECESSION_CHI_A_ARCSEC = (0.0, 10.556403, -2.3814292, -0.00121197, 0.000170663, -0.0000000560)

# Earth's equatorial bulge (J2) and reference radius, as used by DE440.
EARTH_J2 = 1.08262545e-3
EARTH_RADIUS_M = 6378136.3

# The Moon's own shape (degree-2 gravity field, DE440 values; R is the field's
# reference radius).  The Moon keeps one face towards the Earth, so its long
# axis (C22) and equatorial bulge (J2) add a small extra pull along the
# Earth-Moon line: 1e-8 of the total, which is what sets the Moon's mean
# motion to ~0.4"/yr and so matters over centuries.
MOON_J2 = 2.0321568464952570e-4
MOON_C22 = 2.2382740590560020e-5
MOON_RADIUS_M = 1738.0e3

# Earth tides raised by the Moon (constant time-lag model, Mignard 1979).
# The lag makes the tidal bulge lead the Moon, which slowly pushes the Moon
# outwards and decelerates it in longitude.  k2 is the Earth's Love number;
# the lag is calibrated (calibrate_tides.py) so the model reproduces the
# measured tidal acceleration of the Moon's mean longitude below.
EARTH_K2 = 0.335
EARTH_ROTATION_RATE_RAD_S = 7.2921150e-5     # sidereal (IERS nominal)
EARTH_TIDAL_TIME_LAG_S = 562.5                # s; calibrate_tides.py (JPL: 600 s at k2 = 0.32)
LUNAR_TIDAL_ACCELERATION_ARCSEC_CY2 = -25.82  # n-dot, lunar laser ranging (Williams & Boggs 2016)

# WGS84 ellipsoid, for converting an observer's latitude/longitude/height to
# a position.  (A geodetic datum, deliberately distinct from EARTH_RADIUS_M.)
WGS84_A_M = 6378137.0
WGS84_F = 1.0 / 298.257223563

# ---------------------------------------------------------------------------
# Photometry
# ---------------------------------------------------------------------------
MOON_MEAN_DISTANCE_KM = 384400.0    # reference distance for lunar magnitude
