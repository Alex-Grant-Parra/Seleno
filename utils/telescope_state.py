from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from utils.config_state import load_runtime_state, save_runtime_state, runtime_state_lock, get_slew_config as get_static_slew_config

_STATE_CACHE: Optional[Dict[str, Any]] = None

# Where the telescope points is kept as its two axis angles (axis_ra_deg,
# axis_dec_deg; see core/hardware/mount.py): that says which side of the
# mount it's on and how far each axis has turned. The current RA/Dec follow
# from them and the time (a stopped telescope's RA drifts with the sky), and
# are written alongside for anything that reads the file.


def _load_state_from_disk() -> Optional[Dict[str, Any]]:
    return load_runtime_state()


def _write_state_to_disk(state_update: Dict[str, Any]) -> None:
    # Merge partial updates into latest runtime state and persist atomically.
    #
    # This avoids clobbering unrelated keys (for example liveview state) when
    # telescope tracking updates run frequently.
    global _STATE_CACHE
    with runtime_state_lock:
        latest = load_runtime_state() or {}
        merged = latest.copy()
        merged.update(state_update)

        # Preserve the last known telescope coordinates across client restarts and
        # incidental state writes. Explicit manual resets still win.
        if state_update.get("source") != "manual_reset":
            for key in (
                "current_right_ascension",
                "current_declination",
                "target_right_ascension",
                "target_declination",
            ):
                if key in state_update and float(state_update.get(key, 0.0)) == 0.0:
                    existing_value = latest.get(key)
                    if existing_value not in (None, 0, 0.0):
                        merged[key] = existing_value

        save_runtime_state(merged)
        _STATE_CACHE = merged


def _longitude() -> Optional[float]:
    from utils.location import get_current_location

    loc = get_current_location() or {}
    lon = loc.get("longitude")
    return float(lon) if lon is not None else None


def _lst(longitude: float, when: Optional[datetime] = None) -> float:
    from utils.sky_view import lst_degrees

    return lst_degrees(when or datetime.now(timezone.utc), longitude)


def _mount_config():
    from core.hardware.mount import MountConfig
    from utils.config_state import get_mount_config

    m = get_mount_config()
    return MountConfig(ra_limits=tuple(m["ra_axis_limits"]), dec_limits=tuple(m["dec_axis_limits"]),
        pole_crossing=m["pole_crossing"])


def get_mount_axes() -> Optional[Tuple[float, float]]:
    """The telescope's axis angles (RA axis, Dec axis), degrees. Older
    state files only have RA/Dec: the axes are then worked out from them as
    they were when saved (the mount held still since), on the normal side."""
    global _STATE_CACHE
    if _STATE_CACHE is None:
        _STATE_CACHE = _load_state_from_disk()
    state = _STATE_CACHE or {}
    a, d = state.get("axis_ra_deg"), state.get("axis_dec_deg")
    if a is not None and d is not None:
        return float(a), float(d)
    lon = _longitude()
    ra, dec = state.get("current_right_ascension"), state.get("current_declination")
    if lon is None or ra is None or dec is None:
        return None
    when = None
    try:
        stamp = datetime.fromisoformat(str(state.get("updated_at")))
        when = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    from core.hardware.mount import wrap180

    axes = (wrap180(_lst(lon, when) - float(ra)), max(-90.0, min(90.0, float(dec))))
    _write_state_to_disk({"axis_ra_deg": axes[0], "axis_dec_deg": axes[1]})
    return axes


def set_mount_axes(ra_axis: float, dec_axis: float, source: str = "tracking") -> None:
    """Record the axis angles (and the RA/Dec they point at now)."""
    update: Dict[str, Any] = {
        "axis_ra_deg": float(ra_axis),
        "axis_dec_deg": float(dec_axis),
        "source": source,
        "updated_at": datetime.utcnow().isoformat(),
    }
    lon = _longitude()
    if lon is not None:
        from core.hardware.mount import sky_from_axes

        ha, dec = sky_from_axes(ra_axis, dec_axis)
        update["current_right_ascension"] = (_lst(lon) - ha) % 360.0
        update["current_declination"] = dec
        update["current_hour_angle"] = ha
    _write_state_to_disk(update)


def get_telescope_coords() -> Optional[Dict[str, float]]:
    # Return the current telescope coordinates (RA/Dec) if available: from
    # the axis angles and the time when they're known.
    global _STATE_CACHE
    if _STATE_CACHE is None:
        _STATE_CACHE = _load_state_from_disk()
    if not _STATE_CACHE:
        return None
    a, d = _STATE_CACHE.get("axis_ra_deg"), _STATE_CACHE.get("axis_dec_deg")
    if a is not None and d is not None:
        lon = _longitude()
        if lon is not None:
            from core.hardware.mount import sky_from_axes

            ha, dec = sky_from_axes(float(a), float(d))
            return {"right_ascension": (_lst(lon) - ha) % 360.0, "declination": dec}
    
    # Handle backward compatibility: old state files may have 'right_ascension'/'declination'
    current_ra = _STATE_CACHE.get("current_right_ascension")
    current_dec = _STATE_CACHE.get("current_declination")
    
    if current_ra is None:
        # Legacy format - try old field names
        current_ra = _STATE_CACHE.get("right_ascension")
        if current_ra is not None:
            print(f"[telescope_state] Warning: Migrating from legacy RA/Dec format to current_RA/current_Dec.")
        else:
            # Ultra-legacy format
            current_ra = _STATE_CACHE.get("hour_angle", 0.0)
            print(f"[telescope_state] Warning: Very old state format detected, using hour_angle as RA.")
    
    if current_dec is None:
        current_dec = _STATE_CACHE.get("declination", 0.0)
    
    return {
        "right_ascension": float(current_ra),
        "declination": float(current_dec),
    }


def get_target_coords() -> Optional[Dict[str, float]]:
    # Return the target telescope coordinates (RA/Dec) if available.
    # 
    # Returns the target position the telescope is aiming for.
    global _STATE_CACHE
    if _STATE_CACHE is None:
        _STATE_CACHE = _load_state_from_disk()
    if not _STATE_CACHE:
        return None
    
    target_ra = _STATE_CACHE.get("target_right_ascension")
    target_dec = _STATE_CACHE.get("target_declination")
    
    # If no target is set, use current position as target
    if target_ra is None or target_dec is None:
        return get_telescope_coords()
    
    return {
        "right_ascension": float(target_ra),
        "declination": float(target_dec),
    }


def set_telescope_coords(right_ascension: float, declination: float, source: str = "manual", hour_angle: float = None) -> None:
    # Persist the current telescope coordinates (RA/Dec) and update in-memory cache.
    # 
    # This updates the CURRENT position of the telescope, not the target.
    # 
    # Args:
    #     right_ascension: Right Ascension in degrees (time-invariant)
    #     declination: Declination in degrees
    #     source: Source of the coordinate update
    #     hour_angle: Optional current hour angle (for live tracking display)
    update = {
        "current_right_ascension": float(right_ascension),
        "current_declination": float(declination),
        "source": source,
        "updated_at": datetime.utcnow().isoformat(),
    }
    # Add hour angle if provided (for live tracking feedback)
    if hour_angle is not None:
        update["current_hour_angle"] = float(hour_angle)

    # The axes that point there now, on the side of the mount it's on (a
    # sync doesn't flip it); without a site they're worked out later.
    lon = _longitude()
    if lon is not None:
        from core.hardware.mount import nearest_solution, wrap180

        ha = wrap180(_lst(lon) - float(right_ascension))
        near = get_mount_axes() or (ha, float(declination))
        axes = nearest_solution(ha, float(declination), near, _mount_config())
        update["axis_ra_deg"], update["axis_dec_deg"] = axes
    else:
        update["axis_ra_deg"] = update["axis_dec_deg"] = None

    _write_state_to_disk(update)
    print(f"[telescope_state] Current coords set: RA={right_ascension:.4f}°, Dec={declination:.4f}°")


def set_target_coords(right_ascension: float, declination: float, source: str = "manual") -> None:
    # Persist the target telescope coordinates (RA/Dec) and update in-memory cache.
    # 
    # This updates the TARGET position the telescope is aiming for.
    # 
    # Args:
    #     right_ascension: Target Right Ascension in degrees (time-invariant)
    #     declination: Target Declination in degrees
    #     source: Source of the target update
    update = {
        "target_right_ascension": float(right_ascension),
        "target_declination": float(declination),
        "updated_at": datetime.utcnow().isoformat(),
    }

    _write_state_to_disk(update)
    print(f"[telescope_state] Target coords set: RA={right_ascension:.4f}°, Dec={declination:.4f}°")


def update_hour_angle() -> Optional[float]:
    # Update the current hour angle to reflect Earth's rotation.
    # 
    # This keeps RA constant while recalculating HA based on current time and location.
    # Returns the updated hour angle or None if it couldn't be calculated.
    from utils.Tools import hour_angle as calculate_hour_angle
    from utils.location import get_current_location
    
    global _STATE_CACHE
    if _STATE_CACHE is None:
        _STATE_CACHE = _load_state_from_disk() or {}

    # With the axis angles known, the hour angle is the RA axis's (it only
    # changes when the motor turns) and it's the RA that drifts.
    a, d = _STATE_CACHE.get("axis_ra_deg"), _STATE_CACHE.get("axis_dec_deg")
    if a is not None and d is not None:
        from core.hardware.mount import sky_from_axes

        coords = get_telescope_coords()
        ha, _dec = sky_from_axes(float(a), float(d))
        update = {"current_hour_angle": float(ha)}
        if coords:
            update["current_right_ascension"] = coords["right_ascension"]
        _write_state_to_disk(update)
        return ha

    # Get current RA (which is time-invariant)
    current_ra = _STATE_CACHE.get("current_right_ascension", 0.0)
    
    if current_ra == 0.0:
        return None
    
    # Get observer location
    location = get_current_location()
    if location is None:
        return None
    
    longitude = location.get('longitude')
    if longitude is None:
        return None
    
    # Recalculate hour angle based on current time
    current_ha = calculate_hour_angle(current_ra, longitude)
    
    # Write only fields this function owns; preserve liveview and other flags.
    _write_state_to_disk({
        "current_hour_angle": float(current_ha),
        "updated_at": datetime.utcnow().isoformat(),
    })
    
    return current_ha


def get_slew_config() -> Dict[str, float]:
    # Return slewing configuration (speeds and thresholds) from static state.
    return get_static_slew_config()

