"""Where the telescope points, and the session's alignment.

Where the telescope points is kept as its two axis angles (axis_ra_deg,
axis_dec_deg; see core/hardware/mount.py): that says which side of the
mount it's on and how far each axis has turned. They're dead reckoning:
the motor steps added up since the position was last known.

The sky position follows from them, the time and the pointing model
(core/hardware/pointing.py), which is fitted to the alignment points taken
this session (Align on star, plate solves). A stopped telescope's RA drifts
with the sky.

The state lives in memory (config_state.runtime_store) and is written to
config/runtime_state.json when it changes: within a few seconds while the
axes move, at once for a sync, a target or a park. Values worked out from
others (RA/Dec now, hour angle) ride along for anything that reads the
file, but never cause a write.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from utils import clock
from utils.config_state import get_slew_config as get_static_slew_config, runtime_store
from utils.log import get_logger

log = get_logger("telescope_state")

# Kept for the tests, which reset it between config swaps.
_STATE_CACHE: Optional[Dict[str, Any]] = None

_alignment_cache: Dict[str, Any] = {"key": None, "alignment": None}


def _store():
    global _STATE_CACHE
    if _STATE_CACHE is None:
        # Someone (a test) cleared the cache: read the file again.
        runtime_store().reload()
        _STATE_CACHE = {}
    return runtime_store()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _site() -> Optional[Tuple[float, float]]:
    from utils.location import get_current_location

    loc = get_current_location() or {}
    if loc.get("latitude") is None or loc.get("longitude") is None:
        return None
    return float(loc["latitude"]), float(loc["longitude"])


def _longitude() -> Optional[float]:
    site = _site()
    return site[1] if site else None


def _lst(longitude: float, when: Optional[datetime] = None) -> float:
    from utils.sky_view import lst_degrees

    return lst_degrees(when or clock.now(), longitude)


def to_mount_sky(ra: float, dec: float, when: Optional[datetime] = None) -> Optional[Tuple[float, float]]:
    """J2000 (ra, dec) to where the mount points for it now: apparent hour
    angle and declination of date (utils/astrometry.py). None without a site."""
    from utils.astrometry import j2000_to_apparent_hadec

    site = _site()
    if site is None:
        return None
    when = when or clock.now()
    return j2000_to_apparent_hadec(float(ra), float(dec), _lst(site[1], when), site[0], when)


def from_mount_sky(ha: float, dec: float, when: Optional[datetime] = None) -> Optional[Tuple[float, float]]:
    """The inverse: J2000 (ra, dec) of an apparent (ha, dec) now."""
    from utils.astrometry import apparent_hadec_to_j2000

    site = _site()
    if site is None:
        return None
    when = when or clock.now()
    return apparent_hadec_to_j2000(float(ha), float(dec), _lst(site[1], when), site[0], when)


def hemisphere() -> str:
    """"north" or "south": the mount's setting, or from the site's
    latitude when it's "auto" (north if there's no site)."""
    from utils.config_state import get_mount_config

    setting = get_mount_config().get("hemisphere", "auto")
    if setting in ("north", "south"):
        return setting
    site = _site()
    return "south" if site is not None and site[0] < 0 else "north"


def mount_config(speed_sps: Optional[float] = None, steps_per_rev: float = 1600.0):
    """The mount as configured: gears, speed, axis limits, hemisphere
    (config/client_config.json slew_config and mount)."""
    from core.hardware.mount import MountConfig
    from utils.config_state import get_mount_config

    slew = get_static_slew_config()
    m = get_mount_config()
    return MountConfig(
        ra_gear=slew["ra_gear_ratio"],
        dec_gear=slew["dec_gear_ratio"],
        steps_per_rev=steps_per_rev,
        slew_sps=speed_sps or slew["slew_speed_sps"],
        ra_limits=tuple(m["ra_axis_limits"]),
        dec_limits=tuple(m["dec_axis_limits"]),
        pole_crossing=m["pole_crossing"],
        south=hemisphere() == "south",
    )


# --- alignment ----------------------------------------------------------------------

def get_alignment():
    from core.hardware.pointing import Alignment

    data = _store().get("alignment") or {}
    key = repr(data)
    if _alignment_cache["key"] != key:
        _alignment_cache["alignment"] = Alignment.from_dict(data)
        _alignment_cache["key"] = key
    return _alignment_cache["alignment"]


def pointing_model():
    """The fitted model's sky() for the mount functions, or None when
    there's nothing to correct."""
    model = get_alignment().model
    return None if model.is_identity() else model.sky


def _save_alignment(alignment) -> None:
    _store().update({"alignment": alignment.to_dict()}, when="now")


def clear_alignment() -> None:
    from core.hardware.pointing import Alignment

    _save_alignment(Alignment())


def add_alignment_point(ra: float, dec: float, name: str = "", source: str = "star"):
    """The telescope is really pointing at (ra, dec) now: add that to the
    session's alignment and refit. Returns the model, or None when the
    position or site isn't known (then the axes are just set)."""
    from core.hardware.mount import mount_frame
    from core.hardware.pointing import AlignmentPoint

    axes = get_mount_axes()
    aim = to_mount_sky(ra, dec)
    if axes is None or aim is None:
        return None
    config = mount_config()
    sky = mount_frame(aim, config)
    alignment = get_alignment()
    model = alignment.add(AlignmentPoint(axes, sky, source, name))
    _save_alignment(alignment)
    return model


# --- the axes ---------------------------------------------------------------------

def get_mount_axes() -> Optional[Tuple[float, float]]:
    """The telescope's axis angles (RA axis, Dec axis), degrees. Older
    state files only have RA/Dec: the axes are then worked out from them as
    they were when saved (the mount held still since), on the normal side."""
    store = _store()
    a, d = store.get("axis_ra_deg"), store.get("axis_dec_deg")
    if a is not None and d is not None:
        return float(a), float(d)
    lon = _longitude()
    ra, dec = store.get("current_right_ascension"), store.get("current_declination")
    if lon is None or ra is None or dec is None or (not ra and not dec):
        return None
    when = None
    try:
        stamp = datetime.fromisoformat(str(store.get("updated_at")))
        when = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    from core.hardware.mount import mount_frame

    aim = to_mount_sky(float(ra), max(-90.0, min(90.0, float(dec))), when)
    axes = mount_frame(aim, mount_config())
    store.update({"axis_ra_deg": axes[0], "axis_dec_deg": axes[1]}, when="now")
    return axes


def _sky_now(axes: Tuple[float, float], lon: float) -> Tuple[float, float, float]:
    """(J2000 RA, Dec, apparent hour angle) the axes point at now."""
    from core.hardware.mount import mount_frame, sky_from_axes

    config = mount_config()
    model = pointing_model()
    if model is None:
        ha, dec = sky_from_axes(axes[0], axes[1], config)
    else:
        ha, dec = mount_frame(model(axes), config)
    j2000 = from_mount_sky(ha, dec)
    if j2000 is None:
        return (_lst(lon) - ha) % 360.0, dec, ha
    return j2000[0], j2000[1], ha


def set_mount_axes(ra_axis: float, dec_axis: float, source: str = "tracking", when: str = "soon") -> None:
    """Record the axis angles (and the RA/Dec they point at now).
    when: "soon" (written within a few seconds) or "now"."""
    update: Dict[str, Any] = {
        "axis_ra_deg": float(ra_axis),
        "axis_dec_deg": float(dec_axis),
        "source": source,
        "updated_at": _now_iso(),
    }
    lon = _longitude()
    if lon is not None:
        ra, dec, ha = _sky_now((float(ra_axis), float(dec_axis)), lon)
        update["current_right_ascension"] = ra
        update["current_declination"] = dec
        update["current_hour_angle"] = ha
    _store().update(update, when=when)


def get_telescope_coords() -> Optional[Dict[str, float]]:
    """The current RA/Dec, from the axes, the time and the alignment."""
    store = _store()
    a, d = store.get("axis_ra_deg"), store.get("axis_dec_deg")
    if a is not None and d is not None:
        lon = _longitude()
        if lon is not None:
            ra, dec, _ha = _sky_now((float(a), float(d)), lon)
            return {"right_ascension": ra, "declination": dec}
    current_ra = store.get("current_right_ascension")
    current_dec = store.get("current_declination")
    if current_ra is None or current_dec is None or not store.get("updated_at"):
        return None  # never set (the file's defaults)
    return {"right_ascension": float(current_ra), "declination": float(current_dec)}


def get_target_coords() -> Optional[Dict[str, float]]:
    """The target the telescope is aiming for (the current position if
    there's none)."""
    store = _store()
    target_ra = store.get("target_right_ascension")
    target_dec = store.get("target_declination")
    if target_ra is None or target_dec is None:
        return get_telescope_coords()
    return {"right_ascension": float(target_ra), "declination": float(target_dec)}


def set_telescope_coords(right_ascension: float, declination: float, source: str = "manual",
        hour_angle: float = None) -> None:
    """Take (RA, Dec) as where the telescope points now, without an
    alignment point: the axes are set to point there, on the side of the
    mount it's on (a start position, or no site to fit with)."""
    update: Dict[str, Any] = {
        "current_right_ascension": float(right_ascension),
        "current_declination": float(declination),
        "source": source,
        "updated_at": _now_iso(),
    }
    aim = to_mount_sky(right_ascension, declination)
    if aim is not None:
        from core.hardware.mount import mount_frame, nearest_solution

        config = mount_config()
        near = get_mount_axes() or mount_frame(aim, config)
        axes = nearest_solution(aim[0], aim[1], near, config, model=pointing_model())
        update["axis_ra_deg"], update["axis_dec_deg"] = axes
        update["current_hour_angle"] = aim[0]
    else:
        update["axis_ra_deg"] = update["axis_dec_deg"] = None
    _store().update(update, when="now")
    log.info(f"Current coords set: RA={right_ascension:.4f}°, Dec={declination:.4f}°")


def set_target_coords(right_ascension: float, declination: float, source: str = "manual") -> None:
    """The target the telescope is aiming for."""
    _store().update({
        "target_right_ascension": float(right_ascension),
        "target_declination": float(declination),
        "updated_at": _now_iso(),
    }, when="now")


def update_hour_angle() -> Optional[float]:
    """The hour angle the telescope points at now. Nothing is written: it
    follows from the axes and the time."""
    store = _store()
    a, d = store.get("axis_ra_deg"), store.get("axis_dec_deg")
    lon = _longitude()
    if a is None or d is None or lon is None:
        return None
    ra, dec, ha = _sky_now((float(a), float(d)), lon)
    store.update({"current_hour_angle": ha, "current_right_ascension": ra, "current_declination": dec},
        when="never")
    return ha


def get_parked() -> bool:
    return bool(_store().get("parked", False))


def set_parked(parked: bool) -> None:
    _store().update({"parked": bool(parked)}, when="now")


def flush() -> None:
    """Write any pending change now (before a shutdown)."""
    runtime_store().flush()


def get_slew_config() -> Dict[str, float]:
    # Return slewing configuration (speeds and thresholds) from static state.
    return get_static_slew_config()
