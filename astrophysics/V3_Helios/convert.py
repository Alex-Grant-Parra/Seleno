import calendar
from datetime import datetime, timezone
from time import struct_time
import sys

import numpy as np

try:
    from .chebyshev import evaluateAt, evaluateAtBatch, getEpochUTC
    from .constants import (AU_M, OBLIQUITY_J2000_RAD, SECONDS_PER_DAY, JD_UNIX_EPOCH,
                            JD_J2000, DAYS_PER_JULIAN_CENTURY, WGS84_A_M, WGS84_F,
                            METERS_PER_KM, MOON_MEAN_DISTANCE_KM)
except ImportError:
    from chebyshev import evaluateAt, evaluateAtBatch, getEpochUTC
    from constants import (AU_M, OBLIQUITY_J2000_RAD, SECONDS_PER_DAY, JD_UNIX_EPOCH,
                           JD_J2000, DAYS_PER_JULIAN_CENTURY, WGS84_A_M, WGS84_F,
                           METERS_PER_KM, MOON_MEAN_DISTANCE_KM)


# London, UK (city center; WGS84 geodetic)
LONDON_LAT_DEG = 51.5074
LONDON_LON_DEG = -0.1278
LONDON_ALT_M = 35.0


def _eclToEqu(vec):
    eps = OBLIQUITY_J2000_RAD
    rot = np.array(
        [[1.0, 0.0, 0.0], [0.0, np.cos(eps), -np.sin(eps)], [0.0, np.sin(eps), np.cos(eps)]]
    )
    return vec @ rot.T


def _cartToRaDec(vec):
    x, y, z = vec
    radius = np.linalg.norm(vec)
    ra_deg = np.degrees(np.arctan2(y, x) % (2.0 * np.pi))
    dec_deg = np.degrees(np.arcsin(np.clip(z / radius, -1.0, 1.0)))
    return ra_deg, dec_deg, radius


def _julianDate(dtUtc):
    return dtUtc.timestamp() / SECONDS_PER_DAY + JD_UNIX_EPOCH


def _gmstRadians(dtUtc):
    jd = _julianDate(dtUtc)
    t = (jd - JD_J2000) / DAYS_PER_JULIAN_CENTURY
    gmst_deg = (
        280.46061837
        + 360.98564736629 * (jd - JD_J2000)
        + 0.000387933 * t * t
        - (t * t * t) / 38710000.0
    )
    return np.deg2rad(gmst_deg % 360.0)


def _observerEciM(dtUtc, latDeg, lonDeg, altM=0.0):
    # WGS84 geodetic observer position converted to ECI using GMST.
    lat = np.deg2rad(float(latDeg))
    lon = np.deg2rad(float(lonDeg))
    alt = float(altM)

    a = WGS84_A_M
    f = WGS84_F
    e2 = f * (2.0 - f)

    sin_lat = np.sin(lat)
    cos_lat = np.cos(lat)
    n = a / np.sqrt(1.0 - e2 * sin_lat * sin_lat)

    x_ecef = (n + alt) * cos_lat * np.cos(lon)
    y_ecef = (n + alt) * cos_lat * np.sin(lon)
    z_ecef = ((1.0 - e2) * n + alt) * sin_lat

    theta = _gmstRadians(dtUtc)
    cos_t = np.cos(theta)
    sin_t = np.sin(theta)
    x_eci = cos_t * x_ecef - sin_t * y_ecef
    y_eci = sin_t * x_ecef + cos_t * y_ecef

    return np.array([x_eci, y_eci, z_ecef], dtype=np.float64)


def _degToHms(raDeg):
    totalHours = raDeg / 15.0
    hours = int(totalHours) % 24
    minutes = int((totalHours - hours) * 60.0)
    seconds = (totalHours - hours - minutes / 60.0) * 3600.0
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def _degToDms(decDeg):
    sign = "+" if decDeg >= 0 else "-"
    absDeg = abs(decDeg)
    degrees = int(absDeg)
    minutes = int((absDeg - degrees) * 60.0)
    seconds = (absDeg - degrees - minutes / 60.0) * 3600.0
    return f"{sign}{degrees:02d}:{minutes:02d}:{seconds:05.2f}"


def _angleDeg(vecA, vecB):
    normA = float(np.linalg.norm(vecA))
    normB = float(np.linalg.norm(vecB))
    if normA == 0.0 or normB == 0.0:
        return 0.0
    cosine = np.dot(vecA, vecB) / (normA * normB)
    return float(np.degrees(np.arccos(np.clip(cosine, -1.0, 1.0))))


def _phaseAngleDeg(bodyPos, sunPos, earthPos):
    # Angle Sun-Body-Earth as measured at the body.
    bodyToSun = sunPos - bodyPos
    bodyToEarth = earthPos - bodyPos
    return _angleDeg(bodyToSun, bodyToEarth)


def _planetVisualMagnitude(body, rAu, deltaAu, phaseDeg):
    if rAu <= 0.0 or deltaAu <= 0.0:
        return None

    commonTerm = 5.0 * np.log10(rAu * deltaAu)
    phase = float(phaseDeg)

    if body == "mercury":
        mag = -0.42 + 0.0380 * phase - 0.000273 * phase * phase + 0.000002 * phase**3
        return float(mag + commonTerm)
    if body == "venus":
        mag = -4.40 + 0.0009 * phase + 0.000239 * phase * phase - 0.00000065 * phase**3
        return float(mag + commonTerm)
    if body == "mars":
        mag = -1.52 + 0.016 * phase
        return float(mag + commonTerm)
    if body == "jupiter":
        mag = -9.395 + 0.005 * phase
        return float(mag + commonTerm)
    if body == "saturn":
        # Ring brightness is not modeled; this is a disk-only approximation.
        mag = -8.88 + 0.044 * phase
        return float(mag + commonTerm)
    if body == "uranus":
        mag = -7.19 + 0.001 * phase
        return float(mag + commonTerm)
    if body == "neptune":
        mag = -6.87
        return float(mag + commonTerm)
    if body == "pluto":
        mag = -1.0 + 0.041 * phase
        return float(mag + commonTerm)

    return None


def _moonVisualMagnitude(earthMoonDistanceM, phaseAngleDeg):
    distanceKm = float(earthMoonDistanceM) / METERS_PER_KM
    if distanceKm <= 0.0:
        return None

    alpha = float(phaseAngleDeg)
    baseMag = -12.73 + 0.026 * alpha + 4.0e-9 * (alpha**4)
    distanceTerm = 5.0 * np.log10(distanceKm / MOON_MEAN_DISTANCE_KM)
    return float(baseMag + distanceTerm)


def _moonPhaseFromGeometry(sunPos, earthPos, moonPos):
    sunGeo = sunPos - earthPos
    moonGeo = moonPos - earthPos

    elongationDeg = _angleDeg(sunGeo, moonGeo)
    illumFrac = float((1.0 - np.cos(np.deg2rad(elongationDeg))) / 2.0)
    waxing = np.cross(sunGeo, moonGeo)[2] > 0.0

    if elongationDeg < 22.5:
        phase_name = "New Moon"
    elif elongationDeg < 67.5:
        phase_name = "Waxing Crescent" if waxing else "Waning Crescent"
    elif elongationDeg < 112.5:
        phase_name = "First Quarter" if waxing else "Last Quarter"
    elif elongationDeg < 157.5:
        phase_name = "Waxing Gibbous" if waxing else "Waning Gibbous"
    else:
        phase_name = "Full Moon"

    return {
        "moon_phase_name": phase_name,
        "moon_illumination_fraction": illumFrac,
        "moon_elongation_deg": float(elongationDeg),
    }


def _visualMagnitudePayload(body, positions, observer):
    if observer != "earth" or "sun" not in positions or "earth" not in positions:
        return {}

    sunPos = positions["sun"]
    earthPos = positions["earth"]
    bodyPos = positions[body]

    if body == "moon":
        phaseDeg = _phaseAngleDeg(bodyPos, sunPos, earthPos)
        distM = float(np.linalg.norm(bodyPos - earthPos))
        payload = {
            "visual_mag": _moonVisualMagnitude(distM, phaseDeg),
            "phase_angle_deg": float(phaseDeg),
        }
        payload.update(_moonPhaseFromGeometry(sunPos, earthPos, bodyPos))
        return payload

    if body in {"mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"}:
        phaseDeg = _phaseAngleDeg(bodyPos, sunPos, earthPos)
        rAu = float(np.linalg.norm(bodyPos - sunPos) / AU_M)
        deltaAu = float(np.linalg.norm(bodyPos - earthPos) / AU_M)
        return {
            "visual_mag": _planetVisualMagnitude(body, rAu, deltaAu, phaseDeg),
            "phase_angle_deg": float(phaseDeg),
        }

    return {}


def _normalizeTimeInput(timeInput):
    if timeInput is None:
        return datetime.fromtimestamp(sys.modules["time"].time(), tz=timezone.utc)

    if isinstance(timeInput, datetime):
        return timeInput if timeInput.tzinfo is not None else timeInput.replace(tzinfo=timezone.utc)

    if isinstance(timeInput, struct_time):
        # struct_time values passed to this module are expected to be UTC
        # (for example from time.gmtime()), so use timegm rather than mktime.
        return datetime.fromtimestamp(calendar.timegm(timeInput), tz=timezone.utc)

    if isinstance(timeInput, (int, float)):
        # Numeric input is interpreted as Unix epoch seconds (time.time()).
        return datetime.fromtimestamp(float(timeInput), tz=timezone.utc)

    text = str(timeInput)
    if text.endswith("Z"):
        text = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def getRaDecAtTime(timeInput=None, observerLatDeg=None, observerLonDeg=None, observerAltM=0.0):
    """Return RA/DEC for every planet at a requested time.

    Accepts:
    - None: uses the current time from the time library
    - time.time() epoch seconds
    - datetime
    - time.struct_time
    - ISO8601 string

        Optional observer parameters:
        - observer_lat_deg / observer_lon_deg: when both are provided, return
            topocentric RA/Dec for that geodetic location.
        - observer_alt_m: observer height in meters (default 0.0).

    Requires cheb_table.npz to exist. Build it once with:
        python astrophysics/V3_Helios/chebyshev.py
    """
    reqTime = _normalizeTimeInput(timeInput)
    epoch = getEpochUTC()
    tSec = (reqTime - epoch).total_seconds()

    positions = evaluateAt(tSec)
    names = list(positions.keys())

    observer = "earth" if "earth" in names else names[0]
    observerPos = positions[observer]
    observerOffset = None
    if observerLatDeg is not None and observerLonDeg is not None:
        observerOffset = _observerEciM(reqTime, observerLatDeg, observerLonDeg, observerAltM)

    output = {}
    for body, pos in positions.items():
        if body == observer:
            continue

        vector = pos - observerPos
        vectorEqu = _eclToEqu(vector)
        if observerOffset is not None:
            # observer_offset is in equatorial J2000 (ECI), so apply it in
            # the same frame as the target vector.
            vectorEqu = vectorEqu - observerOffset
        raDeg, decDeg, distanceM = _cartToRaDec(vectorEqu)
        output[body] = {
            "ra_hms": _degToHms(raDeg),
            "dec_dms": _degToDms(decDeg),
            "ra_deg": raDeg,
            "dec_deg": decDeg,
            "dist_m": distanceM,
        }
        output[body].update(_visualMagnitudePayload(body, positions, observer))

    return output


def getRaDecAtTimes(timeInputs, observerLatDeg=None, observerLonDeg=None, observerAltM=0.0):
    """Return RA/Dec for each body across multiple requested times.

    Parameters
    ----------
    time_inputs : sequence
        Sequence of accepted time inputs for get_radec_at_time.

    Returns
    -------
    list[dict]
        A list with one output dict per requested time.
    """
    reqTimes = [_normalizeTimeInput(value) for value in timeInputs]
    epoch = getEpochUTC()
    tSeconds = np.array([(value - epoch).total_seconds() for value in reqTimes], dtype=np.float64)
    positionsByBody = evaluateAtBatch(tSeconds)
    names = list(positionsByBody.keys())
    observer = "earth" if "earth" in names else names[0]

    observerOffsets = None
    if observerLatDeg is not None and observerLonDeg is not None:
        observerOffsets = np.array(
            [_observerEciM(value, observerLatDeg, observerLonDeg, observerAltM) for value in reqTimes],
            dtype=np.float64,
        )

    outputPerTime = []
    for idx in range(len(reqTimes)):
        positionsAtT = {name: series[idx] for name, series in positionsByBody.items()}
        observerPos = positionsAtT[observer]
        row = {}
        for body, posSeries in positionsByBody.items():
            if body == observer:
                continue

            vector = posSeries[idx] - observerPos
            vectorEqu = _eclToEqu(vector)
            if observerOffsets is not None:
                vectorEqu = vectorEqu - observerOffsets[idx]

            raDeg, decDeg, distanceM = _cartToRaDec(vectorEqu)
            row[body] = {
                "ra_hms": _degToHms(raDeg),
                "dec_dms": _degToDms(decDeg),
                "ra_deg": raDeg,
                "dec_deg": decDeg,
                "dist_m": distanceM,
            }
            row[body].update(_visualMagnitudePayload(body, positionsAtT, observer))
        outputPerTime.append(row)

    return outputPerTime


# Backward-compatible aliases
get_radec_at_time = getRaDecAtTime
get_radec_at_times = getRaDecAtTimes


if __name__ == "__main__":
    from time import gmtime

    res = getRaDecAtTime(
        gmtime(),
        observerLatDeg=LONDON_LAT_DEG,
        observerLonDeg=LONDON_LON_DEG,
        observerAltM=LONDON_ALT_M,
    )
    print(f"{'Body':<10}  {'RA':>13}  {'Dec':>14}  {'Dist':>16}  {'Vmag':>7}")
    print("-" * 72)
    for body, data in res.items():
        visual_mag = data.get("visual_mag")
        vmag_text = f"{visual_mag:7.3f}" if visual_mag is not None else f"{'-':>7}"
        print(
            f"{body:<10}  {data['ra_hms']:>13}  {data['dec_dms']:>14}  "
            f"{data['dist_m']/1e9:>13.6g} Mm  {vmag_text}"
        )

    moon = res.get("moon")
    if moon is not None:
        illum_pct = 100.0 * float(moon.get("moon_illumination_fraction", 0.0))
        phase_name = moon.get("moon_phase_name", "Unknown")
        print("\nMoon Phase")
        print("-" * 20)
        print(f"Illumination: {illum_pct:.2f}%")
        print(f"Phase: {phase_name}")