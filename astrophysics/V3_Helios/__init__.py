from datetime import datetime, timedelta

from .convert import getRaDecAtTime


def _phase_name_from_angle(phase_angle_deg):
    # Generic phase naming using Sun-Body-Earth phase angle.
    # 0 deg = full, 180 deg = new.
    angle = abs(float(phase_angle_deg))
    if angle < 22.5:
        return "Full"
    if angle < 67.5:
        return "Gibbous"
    if angle < 112.5:
        return "Quarter"
    if angle < 157.5:
        return "Crescent"
    return "New"


def _degrees_to_hms(ra_deg):
    total_hours = (ra_deg / 15.0) % 24.0
    hours = int(total_hours)
    minutes_float = (total_hours - hours) * 60.0
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60.0, 2)

    if seconds >= 60.0:
        seconds = 0.0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        hours = (hours + 1) % 24

    return [hours, minutes, seconds]


def _degrees_to_dms(dec_deg):
    sign = -1 if dec_deg < 0 else 1
    abs_degrees = abs(dec_deg)
    degrees = int(abs_degrees)
    minutes_float = (abs_degrees - degrees) * 60.0
    minutes = int(minutes_float)
    seconds = round((minutes_float - minutes) * 60.0, 2)

    if seconds >= 60.0:
        seconds = 0.0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        degrees += 1

    degrees *= sign
    return [degrees, minutes, seconds]


def getAllCelestialData(year, month, day, hour: int = 0, minute: int = 0, second: float = 0.0):
    # Adding the seconds as a timedelta handles fractions (e.g. 12.9999996 s
    # would otherwise round to 1,000,000 microseconds and raise).
    celestial_time = datetime(year, month, day, hour, minute) + timedelta(seconds=float(second))
    celestial_positions = getRaDecAtTime(celestial_time)
    formatted = {}

    for body_name, values in celestial_positions.items():
        ra_deg = values.get("ra_deg")
        dec_deg = values.get("dec_deg")
        if ra_deg is None or dec_deg is None:
            continue

        body_data = {
            "ra": _degrees_to_hms(ra_deg),
            "dec": _degrees_to_dms(dec_deg),
        }

        visual_mag = values.get("visual_mag")
        if visual_mag is not None:
            body_data["vmag"] = float(visual_mag)

        phase_angle_deg = values.get("phase_angle_deg")
        if phase_angle_deg is not None:
            body_data["phase_angle_deg"] = float(phase_angle_deg)

        moon_phase_name = values.get("moon_phase_name")
        if moon_phase_name:
            body_data["phase_name"] = moon_phase_name
        elif phase_angle_deg is not None and body_name.lower() != "sun":
            body_data["phase_name"] = _phase_name_from_angle(phase_angle_deg)

        moon_illumination_fraction = values.get("moon_illumination_fraction")
        if moon_illumination_fraction is not None:
            body_data["moon_illumination_fraction"] = float(moon_illumination_fraction)

        moon_elongation_deg = values.get("moon_elongation_deg")
        if moon_elongation_deg is not None:
            body_data["moon_elongation_deg"] = float(moon_elongation_deg)

        formatted[body_name.lower()] = body_data

    return formatted
