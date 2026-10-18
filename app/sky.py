"""Low-precision positions of the Sun and the Moon, and what they mean for a night's
imaging at a site: altitudes, twilight, the Moon's phase and distance from a target,
a target's transit and how long it stays high.

The formulas are the Astronomical Almanac's low-precision ones: the Sun to about
0.01 degrees and the Moon to a few tenths of a degree, which is plenty for
"is it dark, where is the Moon, how high is M42 at midnight". Times are UTC.
"""
import math
from datetime import datetime, timedelta, timezone

J2000 = 2451545.0
DEG = math.pi / 180.0

# Sun altitudes that bound the twilights
CIVIL, NAUTICAL, ASTRONOMICAL = -6.0, -12.0, -18.0


def julian_day(when):
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return (when - datetime(2000, 1, 1, 12)).total_seconds() / 86400.0 + J2000


def from_julian_day(jd):
    return datetime(2000, 1, 1, 12) + timedelta(days=jd - J2000)


def _wrap360(x):
    return x % 360.0


def gmst_deg(jd):
    """Greenwich mean sidereal time in degrees."""
    d = jd - J2000
    t = d / 36525.0
    return _wrap360(280.46061837 + 360.98564736629 * d + 0.000387933 * t * t)


def _obliquity(jd):
    return 23.439291 - 0.0130042 * (jd - J2000) / 36525.0


def _ecliptic_to_equatorial(lam, beta, eps):
    lam, beta, eps = lam * DEG, beta * DEG, eps * DEG
    ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps), math.cos(lam))
    dec = math.asin(math.sin(beta) * math.cos(eps) + math.cos(beta) * math.sin(eps) * math.sin(lam))
    return _wrap360(ra / DEG), dec / DEG


def sun_position(jd):
    """(ra, dec, ecliptic longitude) of the Sun, degrees."""
    n = jd - J2000
    mean_long = 280.460 + 0.9856474 * n
    anomaly = (357.528 + 0.9856003 * n) * DEG
    lam = mean_long + 1.915 * math.sin(anomaly) + 0.020 * math.sin(2 * anomaly)
    ra, dec = _ecliptic_to_equatorial(lam, 0.0, _obliquity(jd))
    return ra, dec, _wrap360(lam)


def moon_position(jd):
    """(ra, dec, ecliptic longitude, horizontal parallax) of the Moon, geocentric, degrees."""
    t = (jd - J2000) / 36525.0

    def s(a, b):
        return math.sin((a + b * t) * DEG)

    def c(a, b):
        return math.cos((a + b * t) * DEG)

    lam = (218.32 + 481267.881 * t + 6.29 * s(135.0, 477198.87) - 1.27 * s(259.3, -413335.36)
           + 0.66 * s(235.7, 890534.22) + 0.21 * s(269.9, 954397.74) - 0.19 * s(357.5, 35999.05)
           - 0.11 * s(186.5, 966404.03))
    beta = (5.13 * s(93.3, 483202.02) + 0.28 * s(228.2, 960400.89) - 0.28 * s(318.3, 6003.15)
            - 0.17 * s(217.6, -407332.21))
    parallax = (0.9508 + 0.0518 * c(135.0, 477198.87) + 0.0095 * c(259.3, -413335.36)
                + 0.0078 * c(235.7, 890534.22) + 0.0028 * c(269.9, 954397.74))
    ra, dec = _ecliptic_to_equatorial(lam, beta, _obliquity(jd))
    return ra, dec, _wrap360(lam), parallax


def separation_deg(ra1, dec1, ra2, dec2):
    """Angle between two directions, degrees."""
    a = math.sin(dec1 * DEG) * math.sin(dec2 * DEG) + math.cos(dec1 * DEG) * math.cos(dec2 * DEG) * math.cos((ra1 - ra2) * DEG)
    return math.acos(max(-1.0, min(1.0, a))) / DEG


def alt_az(ra, dec, lat, lon, jd):
    """Altitude and azimuth (from north, through east) in degrees."""
    ha = (gmst_deg(jd) + lon - ra) * DEG
    lat_r, dec_r = lat * DEG, dec * DEG
    sin_alt = math.sin(lat_r) * math.sin(dec_r) + math.cos(lat_r) * math.cos(dec_r) * math.cos(ha)
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(-math.cos(dec_r) * math.sin(ha),
                    math.sin(dec_r) * math.cos(lat_r) - math.cos(dec_r) * math.sin(lat_r) * math.cos(ha))
    return alt / DEG, _wrap360(az / DEG)


def sun_altitude(lat, lon, jd):
    ra, dec, _ = sun_position(jd)
    return alt_az(ra, dec, lat, lon, jd)[0]


def moon_altitude(lat, lon, jd):
    """Topocentric: the Moon's parallax lowers it by up to a degree."""
    ra, dec, _, parallax = moon_position(jd)
    alt, _ = alt_az(ra, dec, lat, lon, jd)
    return alt - parallax * math.cos(alt * DEG)


def moon_phase(jd):
    """(illuminated fraction 0..1, waxing?, phase name)."""
    _, _, sun_lon = sun_position(jd)
    ra_m, dec_m, moon_lon, _ = moon_position(jd)
    ra_s, dec_s, _ = sun_position(jd)
    elongation = separation_deg(ra_m, dec_m, ra_s, dec_s)
    fraction = (1.0 - math.cos(elongation * DEG)) / 2.0
    age = _wrap360(moon_lon - sun_lon)          # 0 new, 180 full
    waxing = age < 180.0
    if fraction < 0.03:
        name = "New Moon"
    elif fraction > 0.97:
        name = "Full Moon"
    elif abs(fraction - 0.5) < 0.06:
        name = "First quarter" if waxing else "Last quarter"
    elif fraction < 0.5:
        name = "Waxing crescent" if waxing else "Waning crescent"
    else:
        name = "Waxing gibbous" if waxing else "Waning gibbous"
    return fraction, waxing, name


def twilight_name(sun_alt):
    if sun_alt > -0.833:
        return "day"
    if sun_alt > CIVIL:
        return "civil twilight"
    if sun_alt > NAUTICAL:
        return "nautical twilight"
    if sun_alt > ASTRONOMICAL:
        return "astronomical twilight"
    return "astronomical night"


def _crossings(func, jd0, hours, level, step_min=10.0):
    """Times in [jd0, jd0 + hours] where func(jd) crosses `level`, refined by bisection.
    Each as (jd, rising?)."""
    out = []
    step = step_min / 1440.0
    n = int(hours * 60 / step_min) + 1
    prev_jd, prev = jd0, func(jd0) - level
    for i in range(1, n + 1):
        jd = jd0 + i * step
        value = func(jd) - level
        if (prev < 0) != (value < 0):
            lo, hi, f_lo = prev_jd, jd, prev
            for _ in range(20):
                mid = 0.5 * (lo + hi)
                f_mid = func(mid) - level
                if (f_mid < 0) == (f_lo < 0):
                    lo, f_lo = mid, f_mid
                else:
                    hi = mid
            out.append((0.5 * (lo + hi), value > prev))
        prev_jd, prev = jd, value
    return out


def night_window(lat, lon, when, level=ASTRONOMICAL):
    """The night (Sun below `level`) that `when` falls in, or the next one.
    Returns (start, end) datetimes, either None when it doesn't happen within a day
    (midsummer at high latitude, or polar night when both are None and it's dark)."""
    jd = julian_day(when)
    sun = lambda t: sun_altitude(lat, lon, t)
    now_dark = sun(jd) < level
    if now_dark:
        before = _crossings(sun, jd - 1.0, 24, level)
        starts = [t for t, rising in before if not rising]
        start = starts[-1] if starts else None
    else:
        start = None
    after = _crossings(sun, jd, 30, level)
    if not now_dark:
        starts = [t for t, rising in after if not rising]
        start = starts[0] if starts else None
    ends = [t for t, rising in after if rising and (start is None or t > start)]
    end = ends[0] if ends else None
    to_dt = lambda t: from_julian_day(t).replace(microsecond=0) if t is not None else None
    return to_dt(start), to_dt(end)


def transit(ra, lon, when):
    """The next meridian crossing of a direction with right ascension `ra`, within a day."""
    jd = julian_day(when)
    lst = _wrap360(gmst_deg(jd) + lon)
    hours = _wrap360(ra - lst) / 15.0410686    # sidereal hours to solar hours
    return when + timedelta(hours=hours)


def target_tonight(ra, dec, lat, lon, when, night=None, min_alt=30.0, step_min=5.0):
    """How a target fares from `when` to the end of the night: altitude now, the
    highest it gets in the dark, its transit, and the minutes it spends above min_alt
    while it's dark."""
    jd_now = julian_day(when)
    alt_now, az_now = alt_az(ra, dec, lat, lon, jd_now)
    jd_next = julian_day(when + timedelta(minutes=10))
    rising = alt_az(ra, dec, lat, lon, jd_next)[0] > alt_now
    start, end = night if night is not None else night_window(lat, lon, when)
    lo = max(when, start) if start else when
    hi = end if end else when + timedelta(hours=12)
    best_alt, best_time, minutes_above = -90.0, None, 0.0
    above_from = above_to = None
    if hi > lo:
        jd = julian_day(lo)
        jd_end = julian_day(hi)
        step = step_min / 1440.0
        while jd <= jd_end:
            alt, _ = alt_az(ra, dec, lat, lon, jd)
            if alt > best_alt:
                best_alt, best_time = alt, jd
            if alt >= min_alt:
                minutes_above += step_min
                above_from = above_from or jd
                above_to = jd
            jd += step
    tr = transit(ra, lon, when - timedelta(hours=12))
    if tr < when - timedelta(hours=1):
        tr = transit(ra, lon, when)
    to_dt = lambda t: from_julian_day(t).replace(microsecond=0) if t is not None else None
    return {
        "alt": round(alt_now, 2),
        "az": round(az_now, 2),
        "rising": rising,
        "best_alt": round(best_alt, 2) if best_time is not None else None,
        "best_time": to_dt(best_time),
        "transit": tr.replace(microsecond=0),
        "transit_alt": round(90.0 - abs(lat - dec), 2),
        "minutes_above": minutes_above,
        "above_from": to_dt(above_from),
        "above_to": to_dt(above_to),
        "circumpolar": dec > 90.0 - lat if lat >= 0 else dec < -90.0 - lat,
        "never_rises": dec < lat - 90.0 if lat >= 0 else dec > lat + 90.0,
    }


def sky_summary(lat, lon, when, target=None, min_alt=30.0):
    """Everything the interface's sky panel needs, as plain JSON-ready values."""
    jd = julian_day(when)
    sun_alt = sun_altitude(lat, lon, jd)
    moon_ra, moon_dec, _, _ = moon_position(jd)
    moon_alt = moon_altitude(lat, lon, jd)
    fraction, waxing, phase_name = moon_phase(jd)
    night = night_window(lat, lon, when, ASTRONOMICAL)
    nautical = night_window(lat, lon, when, NAUTICAL)
    iso = lambda d: d.isoformat() + "Z" if d else None
    out = {
        "time": iso(when.replace(microsecond=0)),
        "site": {"lat": lat, "lon": lon},
        "sun": {"alt": round(sun_alt, 2), "state": twilight_name(sun_alt)},
        "dark": sun_alt < ASTRONOMICAL,
        "night": {"start": iso(night[0]), "end": iso(night[1]),
                  "nautical_start": iso(nautical[0]), "nautical_end": iso(nautical[1])},
        "moon": {"alt": round(moon_alt, 2), "ra": round(moon_ra, 3), "dec": round(moon_dec, 3),
                 "illumination": round(fraction, 3), "waxing": waxing, "phase": phase_name},
    }
    if target is not None:
        ra, dec = float(target["ra"]), float(target["dec"])
        info = target_tonight(ra, dec, lat, lon, when, night=night, min_alt=min_alt)
        info.update(
            name=target.get("name"),
            ra=ra,
            dec=dec,
            moon_separation=round(separation_deg(ra, dec, moon_ra, moon_dec), 1),
        )
        for key in ("best_time", "transit", "above_from", "above_to"):
            info[key] = iso(info[key])
        info["verdict"] = verdict(info, out)
        out["target"] = info
    return out


def verdict(target, sky):
    """('ok'|'warn'|'bad', plain words) for imaging the target now."""
    alt = target["alt"]
    if target.get("never_rises"):
        return "bad", "Never rises from this telescope's site"
    if alt < 0:
        return "bad", "Below the horizon now"
    if alt < 20:
        return "warn", "Low in the sky: the air blurs and dims it"
    moon = sky["moon"]
    if moon["alt"] > 0 and moon["illumination"] > 0.5 and target["moon_separation"] < 40:
        return "warn", "Bright Moon close by: shorter exposures, or a narrowband filter"
    if sky["sun"]["alt"] > ASTRONOMICAL:
        return "warn", "The sky isn't fully dark yet"
    if alt < 30:
        return "warn", "Fairly low; it gets better nearer its transit" if target["rising"] else "Fairly low and sinking"
    return "ok", "Good conditions for this target"
