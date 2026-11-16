"""Dead zones: parts of the sky a telescope must never point at (it would
hit its own mount, or look into somebody's window).

The telescope owns them and enforces them (the client's utils/dead_zones.py).
The server only edits them through setDeadZones and shows them: the status
push carries {"dead_zones": {"zones": [...]}}, and the star map tints them.

A zone is a box in altitude and azimuth at the telescope's site:
    {"name": "Next door", "az": [80, 120], "alt": [-90, 35],
     "reason": "privacy" | "physical" | "other", "locked": false}
az runs clockwise from north; from > to wraps through north, [0, 360] is all
round. Locked zones were set on the Pi and can't be changed from here.
"""
import math

REASONS = ("privacy", "physical", "other")
MAX_ZONES = 32


def _number(value, what):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number")
    if math.isnan(number) or math.isinf(number):
        raise ValueError(f"{what} must be a number")
    return number


def clean_zone(raw, index=0):
    # One zone, checked and tidied; ValueError says what's wrong
    where = f"Zone {index + 1}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object with az and alt")
    az, alt = raw.get("az"), raw.get("alt")
    if not isinstance(az, (list, tuple)) or len(az) != 2:
        raise ValueError(f"{where}: az must be [from, to]")
    if not isinstance(alt, (list, tuple)) or len(alt) != 2:
        raise ValueError(f"{where}: alt must be [lowest, highest]")
    az_from, az_to = _number(az[0], f"{where} azimuth"), _number(az[1], f"{where} azimuth")
    lo, hi = sorted((_number(alt[0], f"{where} altitude"), _number(alt[1], f"{where} altitude")))
    if lo < -90 or hi > 90:
        raise ValueError(f"{where}: altitude must be from -90 to 90")
    if hi - lo < 0.1:
        raise ValueError(f"{where}: the altitude range is empty")
    if az_to - az_from >= 360 or (az_from == 0 and az_to == 360):
        az_from, az_to = 0.0, 360.0
    else:
        az_from, az_to = az_from % 360, az_to % 360
        if az_from == az_to:
            raise ValueError(f"{where}: the azimuth range is empty (0 to 360 is all round)")
    reason = str(raw.get("reason") or "other").strip().lower()
    return {
        "name": " ".join(str(raw.get("name") or "").split())[:40],
        "az": [round(az_from, 2), round(az_to, 2)],
        "alt": [round(lo, 2), round(hi, 2)],
        "reason": reason if reason in REASONS else "other",
        "locked": bool(raw.get("locked", False)),
    }


def clean_zones(raw):
    # A list of zones for setDeadZones; ValueError on the first bad one
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("zones must be a list")
    if len(raw) > MAX_ZONES:
        raise ValueError(f"At most {MAX_ZONES} exclusion zones")
    return [clean_zone(z, i) for i, z in enumerate(raw)]


def zones_from_status(status):
    # The zones a telescope reported in its status push, or None if it didn't
    # say (an older client). Bad entries are dropped: this is the client's data.
    part = (status or {}).get("dead_zones")
    if not isinstance(part, dict) or not isinstance(part.get("zones"), list):
        return None
    out = []
    for i, z in enumerate(part["zones"][:MAX_ZONES]):
        try:
            out.append(clean_zone(z, i))
        except ValueError:
            continue
    return out


def site_from_status(status):
    # {"lat", "lon"} the telescope reported, or None
    site = (status or {}).get("site")
    if not isinstance(site, dict):
        return None
    try:
        lat, lon = float(site["lat"]), float(site["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 360):
        return None
    return {"lat": lat, "lon": lon}
