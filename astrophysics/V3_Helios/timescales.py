"""
Civil time -> dynamical time for V3 Helios.

The simulation runs on TDB (uniform physics time); people ask for civil time.
The link is Delta-T = TT - UT, which depends on the Earth's irregular
rotation:

  * 1972 to UTC_KNOWN_UNTIL: the input is UTC and TT - UTC is exact
    (32.184 s + the leap seconds in LEAP_SECONDS).
  * Before 1972: the input is taken as UT and Delta-T comes from the
    Espenak & Meeus (2006) polynomials, which follow Morrison & Stephenson
    (2004).  Delta-T is ~2.9 h in 26 AD, uncertain by several minutes.
  * After UTC_KNOWN_UNTIL: the input is taken as UT.  Delta-T is held at its
    last known value and then curves upwards at the long-term tidal rate of
    the Earth's rotation (32 s/cy^2 as in Morrison & Stephenson's parabola).
    This is a guess: by 4026 AD it gives ~3.6 h, uncertain by hours.

TDB - TT (under 2 ms) is ignored.
"""

from datetime import datetime, timezone

try:
    from .constants import SECONDS_PER_DAY, TT_MINUS_TAI_S
except ImportError:
    from constants import SECONDS_PER_DAY, TT_MINUS_TAI_S

UTC = timezone.utc

# TAI - UTC from the given date (IERS Bulletin C).  Add a row when the IERS
# announces a new leap second, and move UTC_KNOWN_UNTIL forward.
LEAP_SECONDS = (
    (datetime(1972, 1, 1, tzinfo=UTC), 10), (datetime(1972, 7, 1, tzinfo=UTC), 11),
    (datetime(1973, 1, 1, tzinfo=UTC), 12), (datetime(1974, 1, 1, tzinfo=UTC), 13),
    (datetime(1975, 1, 1, tzinfo=UTC), 14), (datetime(1976, 1, 1, tzinfo=UTC), 15),
    (datetime(1977, 1, 1, tzinfo=UTC), 16), (datetime(1978, 1, 1, tzinfo=UTC), 17),
    (datetime(1979, 1, 1, tzinfo=UTC), 18), (datetime(1980, 1, 1, tzinfo=UTC), 19),
    (datetime(1981, 7, 1, tzinfo=UTC), 20), (datetime(1982, 7, 1, tzinfo=UTC), 21),
    (datetime(1983, 7, 1, tzinfo=UTC), 22), (datetime(1985, 7, 1, tzinfo=UTC), 23),
    (datetime(1988, 1, 1, tzinfo=UTC), 24), (datetime(1990, 1, 1, tzinfo=UTC), 25),
    (datetime(1991, 1, 1, tzinfo=UTC), 26), (datetime(1992, 7, 1, tzinfo=UTC), 27),
    (datetime(1993, 7, 1, tzinfo=UTC), 28), (datetime(1994, 7, 1, tzinfo=UTC), 29),
    (datetime(1996, 1, 1, tzinfo=UTC), 30), (datetime(1997, 7, 1, tzinfo=UTC), 31),
    (datetime(1999, 1, 1, tzinfo=UTC), 32), (datetime(2006, 1, 1, tzinfo=UTC), 33),
    (datetime(2009, 1, 1, tzinfo=UTC), 34), (datetime(2012, 7, 1, tzinfo=UTC), 35),
    (datetime(2015, 7, 1, tzinfo=UTC), 36), (datetime(2017, 1, 1, tzinfo=UTC), 37),
)
UTC_START = LEAP_SECONDS[0][0]
UTC_KNOWN_UNTIL = datetime(2026, 1, 1, tzinfo=UTC)

# Long-term curvature of Delta-T from tidal braking of the Earth's rotation.
FUTURE_DELTA_T_CURVATURE_S_PER_CY2 = 32.0

# TT is labelled like UTC (86400 s days, no leap seconds), so seconds since
# J2000 in TT are (TT label - this label).
_J2000_TT_LABEL = datetime(2000, 1, 1, 12, 0, 0, tzinfo=UTC)


def _asUtc(when):
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def decimalYear(when):
    """Calendar (proleptic Gregorian) year as a decimal, e.g. 2026.5 in early July."""
    when = _asUtc(when)
    start = datetime(when.year, 1, 1, tzinfo=UTC)
    end = datetime(when.year + 1, 1, 1, tzinfo=UTC) if when.year < 9999 else None
    length = (end - start).total_seconds() if end else 365.0 * SECONDS_PER_DAY
    return when.year + (when - start).total_seconds() / length


def taiMinusUtc(when):
    """TAI - UTC (s) for a UTC datetime on or after 1972-01-01."""
    when = _asUtc(when)
    if when < UTC_START:
        raise ValueError("TAI - UTC is only defined here from 1972")
    value = LEAP_SECONDS[0][1]
    for start, seconds in LEAP_SECONDS:
        if when >= start:
            value = seconds
        else:
            break
    return value


def _espenakMeeus(y):
    """Delta-T (s) before 1972 from Espenak & Meeus (2006), y = decimal year."""
    if y < -500:
        u = (y - 1820) / 100
        return -20 + 32 * u * u
    if y < 500:
        u = y / 100
        return (10583.6 - 1014.41 * u + 33.78311 * u**2 - 5.952053 * u**3
                - 0.1798452 * u**4 + 0.022174192 * u**5 + 0.0090316521 * u**6)
    if y < 1600:
        u = (y - 1000) / 100
        return (1574.2 - 556.01 * u + 71.23472 * u**2 + 0.319781 * u**3
                - 0.8503463 * u**4 - 0.005050998 * u**5 + 0.0083572073 * u**6)
    if y < 1700:
        t = y - 1600
        return 120 - 0.9808 * t - 0.01532 * t**2 + t**3 / 7129
    if y < 1800:
        t = y - 1700
        return 8.83 + 0.1603 * t - 0.0059285 * t**2 + 0.00013336 * t**3 - t**4 / 1174000
    if y < 1860:
        t = y - 1800
        return (13.72 - 0.332447 * t + 0.0068612 * t**2 + 0.0041116 * t**3 - 0.00037436 * t**4
                + 0.0000121272 * t**5 - 0.0000001699 * t**6 + 0.000000000875 * t**7)
    if y < 1900:
        t = y - 1860
        return (7.62 + 0.5737 * t - 0.251754 * t**2 + 0.01680668 * t**3
                - 0.0004473624 * t**4 + t**5 / 233174)
    if y < 1920:
        t = y - 1900
        return -2.79 + 1.494119 * t - 0.0598939 * t**2 + 0.0061966 * t**3 - 0.000197 * t**4
    if y < 1941:
        t = y - 1920
        return 21.20 + 0.84493 * t - 0.076100 * t**2 + 0.0020936 * t**3
    if y < 1961:
        t = y - 1950
        return 29.07 + 0.407 * t - t**2 / 233 + t**3 / 2547
    t = y - 1975
    return 45.45 + 1.067 * t - t**2 / 260 - t**3 / 718


def deltaT(when):
    """TT - UT in seconds for a civil datetime (naive datetimes are UTC)."""
    when = _asUtc(when)
    if when < UTC_START:
        return _espenakMeeus(decimalYear(when))
    if when < UTC_KNOWN_UNTIL:
        return TT_MINUS_TAI_S + taiMinusUtc(when)
    base = TT_MINUS_TAI_S + taiMinusUtc(UTC_KNOWN_UNTIL)
    centuries = (decimalYear(when) - decimalYear(UTC_KNOWN_UNTIL)) / 100.0
    return base + FUTURE_DELTA_T_CURVATURE_S_PER_CY2 * centuries * centuries


def utcToEt(when):
    """Seconds of TDB (~TT) past J2000 for a civil datetime: SPICE's "ET"."""
    when = _asUtc(when)
    return (when - _J2000_TT_LABEL).total_seconds() + deltaT(when)
