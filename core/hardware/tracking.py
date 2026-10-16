"""Going to a target and tracking it.

trackCoordinates (from the server or the front panel) and track_object
(the Moon, planets, named stars):

1. Check the Sun (utils/sun_guard.py): near it, someone at the front panel
   has to confirm first; nothing moves or stops if they don't. Then the
   horizon (utils/horizon.py) and the park.
2. Plan the slew (core/hardware/mount.py): the quickest way, over a pole
   if that's quicker, curving round the Sun's keep-out, aimed where the
   target will be when it gets there; run it as one smooth path.
3. When the motors stop (the firmware's motion_done event, with a status
   poll as a fallback), check the position and correct any error.
4. Track: both axes run at the target's rates (sidereal for the stars,
   the ephemeris's for the Moon and planets, or a chosen rate), with a
   gentle correction for any drift between where the axes are and where
   the target is.

Where things point:
* Coordinates people see are J2000 (catalogues, the server, plate
  solves); the mount aims at the target's apparent place of date,
  precessed and lifted by refraction (utils/astrometry.py).
* The axes are dead reckoning from the motor steps
  (utils/telescope_state.py); the session's pointing model
  (core/hardware/pointing.py) maps them to where the telescope really
  points. Each alignment star or plate solve refines it.
* South of the equator the mount model is mirrored and sidereal tracking
  turns the RA motor backwards.

The tracking loop also watches: the Sun (never closer than this session
was allowed), the axis limits, the horizon (stop when the target sets).
It stops and says why on the panel.
"""
import math
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from core.hardware import mount as mount_model
from esp32.interfaceESP32 import ESP32Motor
from esp32.pins import get_motor_boards
from utils import astrometry, clock, ephemeris, horizon
from utils.esp32_state import esp32_state
from utils.LEDmanager import get_led_manager
from utils.location import get_current_location
from utils.log import get_logger
from utils.sky_view import lst_degrees, sun_radec
from utils.telescope_state import (
    add_alignment_point, get_alignment, get_mount_axes, get_parked, get_slew_config, get_telescope_coords,
    mount_config as _state_mount_config, pointing_model, set_mount_axes, set_parked, set_target_coords,
    set_telescope_coords, update_hour_angle,
)

log = get_logger("tracking")

POLARIS_RA_DEG = 37.95456067
POLARIS_DEC_DEG = 89.26410897
SIGMA_OCTANTIS_RA_DEG = 317.19543
SIGMA_OCTANTIS_DEC_DEG = -88.95650

# One sidereal day in seconds (Earth's rotation period relative to the stars)
SIDEREAL_DAY_S = 86164.0905
SIDEREAL_DEG_PER_S = 360.0 / SIDEREAL_DAY_S
SIDEREAL_ARCSEC_PER_S = SIDEREAL_DEG_PER_S * 3600.0
# Tracking rates, arcseconds of hour angle per second.
RATE_PRESETS = {"sidereal": SIDEREAL_ARCSEC_PER_S, "lunar": 14.685, "solar": 15.0, "king": 15.0369}

TRACK_POLL_S = 5.0          # position and watchdog while tracking
SLEW_POLL_S = 2.0           # status poll while slewing, if no motion_done comes
RATE_UPDATE_S = 30.0        # recompute the rates while tracking
DRIFT_GAIN_S = 60.0         # correct a drift over about this long
DRIFT_MAX_FRACTION = 0.1    # ... at most 10% of the sidereal rate
DITHER_TIMEOUT_S = 60.0

# Global tracking state
_tracking_thread = None
_tracking_active = False
_target_object = None  # {"name", "ra", "dec", "mag", "body", "offset", "sun_floor", ...}
_motors_initialized = False
_panel_state = "idle"  # what the front panel shows: idle, slewing, centering, tracking
_wake = threading.Event()
_motion_event = threading.Event()
_path_started_at = 0.0
_rates_sent: Dict[str, Optional[Tuple[float, bool]]] = {"motor1": None, "motor2": None}
_locked_at = 0.0
_last_drift_check = 0.0

# Last ESP32 step counter value read per motor. The axis angles advance by
# the difference between reads; the counter is never reset mid-read, since
# steps taken between a read and a reset would be lost.
_last_motor_steps = {"motor1": 0, "motor2": 0}
_motor_steps_per_rev = 1600.0
# Backlash: steps added when an axis reversed, to be taken off the axis
# angles (the motor turned them, the axis didn't), and each axis's last
# direction of travel.
_takeup = {"motor1": 0, "motor2": 0}
_last_dir = {"motor1": 0, "motor2": 0}


def _publish_state(state: str, **fields) -> None:
    # Tell the front panel (utils/panel_ui.py); never let it break tracking.
    global _panel_state
    _panel_state = state
    try:
        from utils.panel_ui import tracking_update

        tracking_update(state=state, **fields)
    except Exception as exc:
        log.warning("Front panel update failed: %s", exc)


def is_sidereal_tracking() -> bool:
    return _tracking_active and _panel_state == "tracking"


def _reset_motor_positions(motor1, motor2) -> None:
    # Zero both ESP32 step counters and the matching read baselines.
    motor1.reset_position()
    motor2.reset_position()
    _last_motor_steps["motor1"] = 0
    _last_motor_steps["motor2"] = 0


def _get_esp32_connection():
    # The shared ESP32 connection (utils/esp32_state.py), reconnecting if needed.
    return esp32_state.ensure_connection()


def _initialize_motors():
    # Create or engage the motors on the ESP32 if not already done.
    global _motors_initialized

    if _motors_initialized:
        return True
    conn = _get_esp32_connection()
    if not conn:
        log.warning("Cannot initialise motors: ESP32 connection not available")
        return False
    try:
        try:
            motors = conn.list_motors()
        except Exception as e:
            log.warning("Could not list motors: %s", e)
            motors = {}
        motor_ids = set(motors.get("motors", [])) if isinstance(motors, dict) else set()

        for board_key, board in get_motor_boards().items():
            motor_id = str(board.get("motor_id", "")).strip()
            label = str(board.get("label", board_key)).strip() or board_key
            pins = board.get("pins", {}) if isinstance(board.get("pins", {}), dict) else {}
            required = bool(board.get("required", False))
            if not motor_id:
                continue
            if motor_id in motor_ids:
                # The v0.8 firmware boots with motor1/motor2 registered but
                # disengaged, so an existing motor may still need its driver on.
                try:
                    motor = ESP32Motor(conn, motor_id)
                    if bool(board.get("engage", True)) and not motor.status().get("enabled", False):
                        motor.engage()
                        log.info("%s (%s) engaged", motor_id, label)
                except Exception as e:
                    if required:
                        log.error("Error engaging required motor %s (%s): %s", motor_id, label, e)
                        return False
                    log.warning("Could not engage optional motor %s (%s): %s", motor_id, label, e)
                continue
            try:
                ESP32Motor.create(
                    conn=conn, motor_id=motor_id, step_pin=int(pins.get("step")), dir_pin=int(pins.get("dir")),
                    en_pin=int(pins.get("en")), steps_per_rev=int(board.get("steps_per_rev", 1600)),
                    engage=bool(board.get("engage", True)), replace=bool(board.get("replace", False)),
                    microsteps=board.get("microsteps"), chopper=board.get("chopper"),
                )
                motor_ids.add(motor_id)
                log.info("%s (%s) created", motor_id, label)
            except Exception as e:
                if required:
                    log.exception("Error creating required motor %s (%s): %s", motor_id, label, e)
                    return False
                log.warning("Could not create optional motor %s (%s): %s", motor_id, label, e)
        _motors_initialized = True
        # Start counting from the counters as they are: steps taken before
        # this (another session's slews) are already in the stored axes.
        _resync_motor_baseline(conn)
        log.info("Motors ready")
        return True
    except Exception as e:
        log.exception("Error initialising motors: %s", e)
        return False


def _read_position(motor: ESP32Motor) -> float:
    """A motor's position in logical steps, with the fraction: the firmware's
    position_fine counts 1/256 microsteps (51200 a turn), 32 times finer than
    the logical step. At the Moon's Dec rate a whole step takes minutes."""
    result = motor.conn.send({"cmd": "get_position", "motor": motor.motor_id})
    try:
        motor._remember(result)
    except Exception:
        pass
    if "position_fine" in result:
        steps_per_rev = float(result.get("steps_per_rev") or motor.steps_per_rev or 1600)
        return float(result["position_fine"]) * steps_per_rev / 51200.0
    return float(result.get("position", 0))


def _resync_motor_baseline(conn) -> None:
    for motor_id in ("motor1", "motor2"):
        try:
            _last_motor_steps[motor_id] = _read_position(ESP32Motor(conn, motor_id))
        except Exception as e:
            log.warning("Could not read %s's position: %s", motor_id, e)


def mount_config(speed_sps: Optional[float] = None) -> mount_model.MountConfig:
    # The mount as configured (utils/telescope_state.mount_config), with the
    # motors' steps per turn as the firmware reports them.
    return _state_mount_config(speed_sps, _motor_steps_per_rev)


def _site() -> Optional[Tuple[float, float]]:
    location = get_current_location() or {}
    if location.get("latitude") is None or location.get("longitude") is None:
        return None
    return float(location["latitude"]), float(location["longitude"])


def _longitude() -> Optional[float]:
    location = get_current_location() or {}
    lon = location.get("longitude")
    return float(lon) if lon is not None else None


def _sun_hour_angle(longitude: float, when: Optional[datetime] = None):
    when = when or clock.now()
    sun_ra, sun_dec = sun_radec(when)
    return mount_model.wrap180(lst_degrees(when, longitude) - sun_ra), sun_dec


def target_sky(target: Dict[str, Any], when: Optional[datetime] = None) -> Optional[Tuple[float, float]]:
    """Where to aim for a target now (or at when): its apparent hour angle
    and declination of date, real sky. None without a site."""
    site = _site()
    if site is None:
        return None
    when = when or clock.now()
    lat, lon = site
    body = target.get("body")
    off_ra, off_dec = target.get("offset", (0.0, 0.0))
    lst = lst_degrees(when, lon)
    if body:
        p = ephemeris.position_of_date(body, when, site if body == "Moon" else None)
        ha = mount_model.wrap180(lst - (p["ra"] + off_ra))
        dec = p["dec"] + off_dec
        return astrometry.apparent_hadec(ha, dec, lat) if astrometry.refraction_enabled() else (ha, dec)
    return astrometry.j2000_to_apparent_hadec(float(target["ra"]) + off_ra, float(target["dec"]) + off_dec,
        lst, lat, when)


def plan_goto(ra: float, dec: float, start_axes=None, speed_sps: Optional[float] = None,
        target: Optional[Dict[str, Any]] = None):
    # The route from the axes (default: where the telescope is) to point at
    # (ra, dec) J2000 (or target, for a moving one), aimed where the target
    # will be when it gets there: the quickest way, over a pole if that's
    # quicker, and curving round the Sun's keep-out
    # (utils/sun_guard.keep_out_deg). None without a site or a position.
    from utils import sun_guard

    lon = _longitude()
    start = start_axes if start_axes is not None else get_mount_axes()
    if lon is None or start is None:
        return None
    target = target or {"ra": float(ra), "dec": float(dec)}
    config = mount_config(speed_sps)
    model = pointing_model()
    now = clock.now()
    sun = _sun_hour_angle(lon, now)
    start_sun = mount_model.separation(mount_model.sky_from_axes(*start, config), sun)
    route = None
    lead_s = 0.0
    for _ in range(2):
        aim = target_sky(target, now + timedelta(seconds=lead_s))
        if aim is None:
            return None
        keep_out = sun_guard.keep_out_deg(start_sun, mount_model.separation(aim, sun))
        route = mount_model.plan_route(start, aim, config, sun=sun, keep_out=keep_out, model=model)
        lead_s = route.seconds
    return route


def _update_current_coords_from_motors() -> None:
    # Add the steps each motor has taken since the last read to the axis
    # angles (core/hardware/mount.py), less any backlash take-up.
    global _motor_steps_per_rev
    try:
        conn = _get_esp32_connection()
        if not conn:
            return
        deltas = {"motor1": 0, "motor2": 0}
        for motor_id in ("motor1", "motor2"):
            motor = ESP32Motor(conn, motor_id)
            try:
                position = _read_position(motor)
            except Exception as e:
                log.warning("Error reading %s position: %s", motor_id, e)
                continue
            deltas[motor_id] = position - _last_motor_steps[motor_id] - _takeup[motor_id]
            _takeup[motor_id] = 0
            _last_motor_steps[motor_id] = position
            if motor_id == "motor1":
                _motor_steps_per_rev = float(motor.steps_per_rev or _motor_steps_per_rev)
        axes = get_mount_axes()
        if axes is None:
            return
        d_ra, d_dec = mount_config().axis_degrees(deltas["motor1"], deltas["motor2"])
        if d_ra or d_dec:
            set_mount_axes(axes[0] + d_ra, axes[1] + d_dec, source="tracking")
            log.debug("Axes moved RA %+.5f, Dec %+.5f; now %.4f, %.4f", d_ra, d_dec, axes[0] + d_ra, axes[1] + d_dec)
    except Exception as e:
        log.warning("Error updating the position from the motors: %s", e)


def _wait_until_stopped(conn, timeout_s: float = 5.0) -> None:
    # A stop from slew speed ramps down (up to about a second).
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if not any(ESP32Motor(conn, m).status().get("moving") for m in ("motor1", "motor2")):
                return
        except Exception as e:
            log.warning("Error reading motor status: %s", e)
            return
        time.sleep(0.1)


def _backlash_steps():
    try:
        from utils.config_state import get_mount_config

        b = get_mount_config().get("backlash_steps", [0, 0])
        return int(b[0]), int(b[1])
    except Exception:
        return 0, 0


def with_backlash(steps, backlash=None, last_dir=None):
    """A path's points with backlash take-up: where an axis sets off the
    opposite way to its last move, every point from there on goes that many
    steps further. Returns (points, take-up per motor, last direction)."""
    backlash = backlash if backlash is not None else _backlash_steps()
    last_dir = dict(last_dir if last_dir is not None else _last_dir)
    takeup = {"motor1": 0, "motor2": 0}
    out = [list(p) for p in steps]
    prev = (0, 0)
    for i, p in enumerate(steps):
        for axis, motor in ((0, "motor1"), (1, "motor2")):
            move = p[axis] - prev[axis]
            if move == 0:
                continue
            direction = 1 if move > 0 else -1
            if backlash[axis] and last_dir[motor] and direction != last_dir[motor]:
                extra = direction * backlash[axis]
                takeup[motor] += extra
                for q in out[i:]:
                    q[axis] += extra
            last_dir[motor] = direction
        prev = p
    return [tuple(p) for p in out], takeup, last_dir


def _run_route(route, config: mount_model.MountConfig) -> None:
    # Drive both motors along the route as one smooth path ({"cmd":"path"}).
    # Doesn't wait; the tracking loop watches for the end. Raises if the
    # ESP32 refuses it.
    global _motors_initialized, _path_started_at, _last_dir
    conn = _get_esp32_connection()
    if not conn:
        raise RuntimeError("ESP32 connection not available")
    try:
        motors = conn.list_motors()
        if "motor1" not in str(motors) or "motor2" not in str(motors):
            _motors_initialized = False
    except Exception as e:
        log.warning("Could not verify motors: %s", e)
        _motors_initialized = False
    if not _initialize_motors():
        raise RuntimeError("Motors not ready")
    for motor_id in ("motor1", "motor2"):
        motor = ESP32Motor(conn, motor_id)
        try:
            if not motor.status().get("enabled", False):
                motor.engage()
        except Exception as e:
            log.warning("Error engaging %s: %s", motor_id, e)
    steps = mount_model.route_steps(route, config)
    if not any(a or b for a, b in steps):
        log.info("Already there: nothing to move")
        return
    steps, takeup, last_dir = with_backlash(steps)
    shape = "round the Sun" if route.detour else "straight"
    side = ", over the pole" if route.flips else ""
    log.info("Slewing %s%s: %d points, about %.0f s at %.0f sps, closest to the Sun %.1f deg",
        shape, side, len(steps), route.seconds, config.slew_sps, route.sun_deg)
    _motion_event.clear()
    _rates_sent["motor1"] = _rates_sent["motor2"] = None
    conn.path(steps, config.slew_sps)
    for motor_id in ("motor1", "motor2"):
        _takeup[motor_id] += takeup[motor_id]
    _last_dir = last_dir
    _path_started_at = time.monotonic()


def on_motion_done(event: Dict[str, Any]) -> None:
    """From the ESP32's motion_done events (utils/esp32_events.py): a move
    ended, so the tracking loop looks now rather than at its next poll."""
    _motion_event.set()
    _wake.set()


def _motors_moving(conn) -> bool:
    try:
        return any(ESP32Motor(conn, m).status().get("moving") for m in ("motor1", "motor2"))
    except Exception as e:
        log.warning("Could not read motor status: %s", e)
        return True


def _sun_and_limits_problem() -> Optional[str]:
    # Whether tracking has to stop: pointing (or aimed) closer to the Sun
    # than this session was allowed, or an axis past its limit.
    target = _target_object or {}
    axes = get_mount_axes()
    lon = _longitude()
    if axes is None or lon is None or ("ra" not in target and not target.get("body")):
        return None
    config = mount_config()
    sun = _sun_hour_angle(lon)
    pointing = mount_model.sky_from_axes(*axes, config)
    aimed = target_sky(target) or pointing
    floor = float(target.get("sun_floor", 19.5))
    closest = min(mount_model.separation(pointing, sun), mount_model.separation(aimed, sun))
    if closest < floor:
        return f"{closest:.0f} deg from the Sun: tracking stopped"
    if not config.within(axes, slack=2.0):
        return "An axis reached its limit: tracking stopped"
    return None


def _horizon_problem() -> Optional[str]:
    target = _target_object or {}
    site = _site()
    if site is None or not horizon.stop_when_set() or target.get("below_ok"):
        return None
    aim = target_sky(target)
    if aim is None:
        return None
    alt, az = astrometry.hadec_to_altaz(aim[0], aim[1], site[0])
    limit = horizon.limit_at(az)
    if alt < limit - 0.5:
        return f"{target.get('name', 'The target')} has set: tracking stopped"
    return None


def _halt_from_loop(reason: str) -> None:
    # Stop tracking from inside the tracking thread (stop_tracking would wait
    # for this thread) and say why on the front panel.
    global _tracking_active, _target_object
    log.warning(reason)
    _tracking_active = False
    _target_object = None
    conn = _get_esp32_connection()
    if conn:
        for motor_id in ("motor1", "motor2"):
            try:
                conn.send({"cmd": "stop", "motor": motor_id})
            except Exception as e:
                log.warning("Error stopping %s: %s", motor_id, e)
    _rates_sent["motor1"] = _rates_sent["motor2"] = None
    _publish_state("idle")
    try:
        get_led_manager().set_error(True, critical=False)
    except Exception:
        pass
    try:
        from utils.panel_ui import get_panel_ui

        get_panel_ui().toast(reason, 6000)
    except Exception:
        pass


# --- rates ------------------------------------------------------------------------------

def _rate_setting() -> Tuple[str, float]:
    from utils.config_state import get_section

    s = get_section("tracking")
    name = str(s.get("rate", "sidereal")).lower()
    if name == "custom":
        return name, float(s.get("custom_arcsec_per_s", SIDEREAL_ARCSEC_PER_S))
    return (name, RATE_PRESETS[name]) if name in RATE_PRESETS else ("sidereal", SIDEREAL_ARCSEC_PER_S)


def sky_rates(target: Dict[str, Any], when: Optional[datetime] = None) -> Tuple[float, float, str]:
    """How fast the target's hour angle and declination change, degrees
    per second, and which rate that is."""
    when = when or clock.now()
    body = target.get("body")
    try:
        from utils.config_state import get_section

        auto = bool(get_section("tracking").get("auto_rate", True))
    except Exception:
        auto = True
    if body and auto:
        site = _site()
        dra, ddec = ephemeris.rates(body, when, site if body == "Moon" else None)
        return SIDEREAL_DEG_PER_S - dra, ddec, body.lower()
    name, arcsec = _rate_setting()
    return arcsec / 3600.0, 0.0, name


def current_rate() -> Dict[str, Any]:
    target = _target_object
    if not target or not is_sidereal_tracking():
        return {"rate": None}
    ha_rate, dec_rate, name = sky_rates(target)
    return {"rate": name, "ha_arcsec_per_s": round(ha_rate * 3600.0, 4), "dec_arcsec_per_s": round(dec_rate * 3600.0, 4)}


def set_rate(name: str, custom_arcsec_per_s: Optional[float] = None) -> Dict[str, Any]:
    """Choose the tracking rate: sidereal, lunar, solar, king or custom."""
    from utils.config_state import save_section

    name = str(name).lower()
    if name not in RATE_PRESETS and name != "custom":
        return {"error": f"rate is one of {', '.join(list(RATE_PRESETS) + ['custom'])}"}
    changes: Dict[str, Any] = {"rate": name}
    if custom_arcsec_per_s is not None:
        changes["custom_arcsec_per_s"] = float(custom_arcsec_per_s)
    save_section("tracking", changes)
    _wake.set()
    return {"rate": name}


def _drive_axis(conn, motor_id: str, axis_rate: float, steps_per_axis_deg: float) -> None:
    """Run a motor continuously at axis_rate (deg/s, signed); stop it when
    the rate is next to nothing. Only sends what changed."""
    sps = abs(axis_rate) * steps_per_axis_deg
    forward = axis_rate >= 0
    if sps < 1e-4:
        if _rates_sent.get(motor_id) not in (None, (0.0, True)):
            conn.send({"cmd": "stop", "motor": motor_id})
        _rates_sent[motor_id] = (0.0, True)
        return
    last = _rates_sent.get(motor_id)
    if last is not None and last[1] == forward and last[0] > 0 and abs(sps - last[0]) / last[0] < 0.002:
        return
    motor = ESP32Motor(conn, motor_id)
    motor.set_speed_sps(sps)
    if last is None or last[1] != forward or last[0] == 0.0:
        motor.start_continuous(forward=forward)
    _rates_sent[motor_id] = (sps, forward)


def _drive_rates(conn) -> None:
    """Set both motors to follow the target, with a correction for any
    drift between the axes and where the target is."""
    target = _target_object
    if not target:
        return
    _update_current_coords_from_motors()  # the correction needs the axes as they are now
    axes = get_mount_axes()
    if axes is None:
        return
    config = mount_config()
    ha_rate, dec_rate, _name = sky_rates(target)
    rate_a, rate_d = mount_model.axis_rates(ha_rate, dec_rate, axes[1], config)
    goal = _goal_axes(target, axes, config)
    if goal is not None:
        limit = DRIFT_MAX_FRACTION * SIDEREAL_DEG_PER_S
        rate_a += max(-limit, min(limit, (goal[0] - axes[0]) / DRIFT_GAIN_S))
        rate_d += max(-limit, min(limit, (goal[1] - axes[1]) / DRIFT_GAIN_S))
    _drive_axis(conn, "motor1", rate_a, config.ra_gear * config.steps_per_rev / 360.0)
    _drive_axis(conn, "motor2", rate_d, config.dec_gear * config.steps_per_rev / 360.0)


def _goal_axes(target: Dict[str, Any], axes, config) -> Optional[Tuple[float, float]]:
    aim = target_sky(target)
    if aim is None:
        return None
    return mount_model.nearest_solution(aim[0], aim[1], axes, config, model=pointing_model())


def _start_ra_tracking() -> None:
    # Start tracking the target: both motors at its rates.
    conn = _get_esp32_connection()
    if not conn:
        log.error("ESP32 connection not available; cannot start tracking")
        return
    _rates_sent["motor1"] = _rates_sent["motor2"] = None
    try:
        _wait_until_stopped(conn)
        _drive_rates(conn)
        if _target_object:
            ha_rate, dec_rate, name = sky_rates(_target_object)
            log.info("Tracking at the %s rate (%.4f, %.4f arcsec/s)", name, ha_rate * 3600.0, dec_rate * 3600.0)
    except Exception as e:
        log.exception("Error starting tracking: %s", e)


# --- the loop -------------------------------------------------------------------------

def _check_position(conn) -> None:
    """The motors have stopped after a slew or a correction: lock on, or
    correct what's left."""
    global _locked_at
    leds = get_led_manager()
    target = _target_object or {}
    axes = get_mount_axes()
    if axes is None or _site() is None:
        return
    config = mount_config()
    goal = _goal_axes(target, axes, config)
    if goal is None:
        return
    delta_ra_axis = goal[0] - axes[0]
    delta_dec_axis = goal[1] - axes[1]
    slew = get_slew_config()
    centered_threshold = slew.get("centered_threshold_degrees", 0.01)
    log.info("Position check: RA axis error %.6f, Dec axis error %.6f (centred within %s)",
        delta_ra_axis, delta_dec_axis, centered_threshold)
    if abs(delta_ra_axis) < centered_threshold and abs(delta_dec_axis) < centered_threshold:
        leds.flash_tracking_locked()
        leds.set_tracking(True, pulse=False)
        leds.set_command_busy(False)
        _start_ra_tracking()
        _locked_at = time.monotonic()
        # From now on it holds on the target: as close to the Sun as the
        # target was allowed, no closer.
        if _target_object is not None:
            _target_object["sun_floor"] = _target_object.get("sun_floor_tracking", 19.5)
        coords = get_telescope_coords() or {}
        _publish_state("tracking", ra_now=coords.get("right_ascension"), dec_now=coords.get("declination"))
        return
    log.info("Off target: correcting")
    small = max(abs(delta_ra_axis), abs(delta_dec_axis)) < slew.get("slew_threshold_degrees", 1.0)
    speed = slew["refine_speed_sps"] if small else slew["slew_speed_sps"]
    route = plan_goto(0.0, 0.0, speed_sps=speed, target=target)
    _publish_state("centering")
    if route is not None:
        try:
            _run_route(route, mount_config(speed))
        except Exception as e:
            log.warning("Correction refused: %s", e)


def _continuous_tracking_loop() -> None:
    global _last_drift_check
    log.info("Tracking loop started")
    last_coords = 0.0
    last_rates = 0.0
    last_poll = 0.0
    while _tracking_active:
        try:
            if _target_object is None:
                _wake.wait(1.0)
                _wake.clear()
                continue
            now = time.monotonic()
            moving = _panel_state in ("slewing", "centering")
            if moving or now - last_coords >= TRACK_POLL_S:
                _update_current_coords_from_motors()
                last_coords = now
                update_hour_angle()
                coords = get_telescope_coords() or {}
                if coords:
                    _publish_state(_panel_state, ra_now=coords.get("right_ascension"),
                        dec_now=coords.get("declination"))
            problem = _sun_and_limits_problem() or _horizon_problem()
            if problem:
                _halt_from_loop(problem)
                break
            conn = _get_esp32_connection()
            if moving:
                ended = _motion_event.is_set() or now - last_poll >= SLEW_POLL_S
                if ended and conn:
                    last_poll = now
                    _motion_event.clear()
                    if not _motors_moving(conn):
                        _update_current_coords_from_motors()
                        _check_position(conn)
            elif conn and now - last_rates >= RATE_UPDATE_S:
                last_rates = now
                _drive_rates(conn)
                _maybe_check_drift()
            _wake.wait(0.5 if moving else TRACK_POLL_S)
            _wake.clear()
        except Exception as e:
            log.exception("Error in the tracking loop: %s", e)
            time.sleep(2.0)
    log.info("Tracking loop stopped")


def _maybe_check_drift() -> None:
    """With solver.drift_check_min, re-solve the live view every so often
    while tracking and correct (utils/plate_solve.py)."""
    global _last_drift_check
    try:
        from utils.config_state import get_section

        minutes = float(get_section("solver").get("drift_check_min", 0) or 0)
    except Exception:
        return
    if minutes <= 0 or time.monotonic() - _last_drift_check < minutes * 60:
        return
    _last_drift_check = time.monotonic()

    def run() -> None:
        try:
            from utils import plate_solve

            plate_solve.drift_check()
        except Exception as exc:
            log.info("Drift check: %s", exc)
    threading.Thread(target=run, name="drift-check", daemon=True).start()


def stop_tracking(set_leds_idle: bool = True) -> None:
    # Stop tracking and the motors.
    global _tracking_active, _tracking_thread, _target_object
    leds = get_led_manager()
    if _tracking_active:
        log.info("Stopping tracking")
        _tracking_active = False
        _publish_state("idle")
        _wake.set()
        if _tracking_thread and _tracking_thread.is_alive() and _tracking_thread is not threading.current_thread():
            _tracking_thread.join(timeout=5)
        _target_object = None
        _tracking_thread = None
    conn = _get_esp32_connection()
    if conn:
        for motor_id in ("motor1", "motor2"):
            try:
                conn.send({"cmd": "stop", "motor": motor_id})
            except Exception as e:
                log.warning("Error stopping %s: %s", motor_id, e)
    else:
        log.warning("ESP32 connection not available to stop the motors")
    _rates_sent["motor1"] = _rates_sent["motor2"] = None
    try:
        from utils.telescope_state import flush

        flush()
    except Exception:
        pass
    if set_leds_idle:
        leds.set_idle()


def stop_tracking_for_estop() -> None:
    # The ESP32 has latched its emergency stop and halted the motors itself.
    # Stop the Pi's tracking loop so it doesn't keep issuing moves; the stop
    # commands stop_tracking() sends are still accepted while latched.
    log.warning("Emergency stop: stopping tracking")
    stop_tracking(set_leds_idle=False)


def on_esp32_reboot() -> None:
    # A restarted ESP32 has its motors disengaged and step counters at zero.
    # Re-create/engage on the next move and restart position deltas from 0.
    global _motors_initialized
    _motors_initialized = False
    _last_motor_steps["motor1"] = 0
    _last_motor_steps["motor2"] = 0
    _rates_sent["motor1"] = _rates_sent["motor2"] = None
    log.info("ESP32 restarted: motors will be re-initialised before the next move")


def _catch_up_position() -> None:
    conn = _get_esp32_connection()
    if conn and _initialize_motors():
        _wait_until_stopped(conn)
        _update_current_coords_from_motors()


def sync_position(ra: float, dec: float, name: str = "", source: str = "star") -> None:
    # The telescope is pointing at a known place (J2000), centred by hand on
    # the front panel ("Align on star") or found by a plate solve: stop, and
    # add it to the session's alignment (utils/telescope_state.py). Without
    # a known position or site the axes are simply set from it.
    stop_tracking()
    _catch_up_position()
    model = add_alignment_point(float(ra), float(dec), name=name, source=source)
    if model is None:
        set_telescope_coords(float(ra), float(dec), source="manual_reset")
    else:
        log.info("Aligned on %s: %d point(s), rms %.1f arcmin", name or f"{ra:.3f}, {dec:.3f}", model.points,
            model.rms_arcmin)


def alignment_summary() -> Dict[str, Any]:
    from core.hardware.pointing import polar_report

    alignment = get_alignment()
    model = alignment.model
    return {"points": len(alignment.points), "rms_arcmin": round(model.rms_arcmin, 2),
        "model": {k: round(getattr(model, k) * 60.0, 2) for k in ("ih", "id", "alt", "az", "ch", "np")},
        "polar": polar_report(model)}


def clear_alignment() -> Dict[str, Any]:
    from utils.telescope_state import clear_alignment as clear

    clear()
    return {"status": "Alignment cleared"}


def _start_position() -> Tuple[float, float]:
    """Where the telescope is taken to be when nothing is known: the park
    (home) position, pointing at the pole with the counterweight down."""
    from utils.config_state import get_mount_config

    park = get_mount_config().get("park_axes", [0.0, 90.0])
    return float(park[0]), float(park[1])


def _set_polaris_alignment(location: dict) -> None:
    # Pointing at the pole star: Polaris, or sigma Octantis in the south.
    if location.get("latitude") is not None and float(location["latitude"]) < 0:
        set_telescope_coords(SIGMA_OCTANTIS_RA_DEG, SIGMA_OCTANTIS_DEC_DEG, source="polaris")
    else:
        set_telescope_coords(POLARIS_RA_DEG, POLARIS_DEC_DEG, source="polaris")


def _start_goto(target: Dict[str, Any], sun_approval=None) -> Dict[str, Any]:
    """Steps 2-4 for a target that passed the checks."""
    global _tracking_active, _tracking_thread, _target_object
    leds = get_led_manager()
    name = target.get("name", "")
    location = get_current_location() or {}

    if _tracking_active:
        log.info("Already tracking: stopping before the new target")
        stop_tracking()

    log.info("Acquiring %s (RA %.4f, Dec %.4f%s)", name, target.get("ra", 0.0), target.get("dec", 0.0),
        f", {target['body']}" if target.get("body") else "")
    leds.clear_error()
    leds.set_command_busy(True)
    _publish_state("slewing", target=str(name), ra=target.get("ra"), dec=target.get("dec"), mag=target.get("mag"))

    # Bring the position up to date (any motion has stopped first).
    _catch_up_position()
    if get_mount_axes() is None:
        start = _start_position()
        log.warning("No known position: taking the park position %s as the start", start)
        set_mount_axes(start[0], start[1], source="assumed", when="now")

    route = plan_goto(0.0, 0.0, target=target)
    if route is None:
        _publish_state("idle")
        leds.set_command_busy(False)
        return {"error": "Couldn't plan the slew"}
    if route.notes:
        log.info("Note: %s", "; ".join(route.notes))

    # How close to the Sun this session may get: on the way, and then on
    # the target (the watchdog in the tracking loop holds it to that).
    from utils import sun_guard

    aim = target_sky(target)
    lon = float(location["longitude"])
    target_sun = mount_model.separation(aim, _sun_hour_angle(lon)) if aim else 180.0
    danger = sun_guard.DANGER_DEG - 0.5
    target["sun_floor"] = min(danger, route.sun_deg - 1.0)
    target["sun_floor_tracking"] = min(danger, target_sun - 1.0) if sun_approval is not None else danger
    _target_object = target
    set_target_coords(float(target.get("ra", 0.0)), float(target.get("dec", 0.0)), source="tracking")

    config = mount_config()
    try:
        leds.set_movement("slewing")
        _run_route(route, config)
    except Exception as e:
        log.warning("Slew refused: %s", e)
        _target_object = None
        _publish_state("idle")
        leds.set_error(True, critical=False)
        leds.set_command_busy(False)
        return {"error": f"Slew refused: {e}"}

    if not _tracking_active:
        _tracking_active = True
        _tracking_thread = threading.Thread(target=_continuous_tracking_loop, daemon=True, name="tracking")
        _tracking_thread.start()
    _wake.set()
    return {
        "status": "slewing",
        "seconds": round(route.seconds, 1),
        "round_the_sun": route.detour,
        "over_the_pole": route.flips,
        "closest_to_sun_deg": round(route.sun_deg, 1),
    }


def _checks(name: str, ra: float, dec: float, sun_approval, allow_below_horizon: bool):
    """The Sun, the site, the park and the horizon, in that order (the Sun
    before anything else changes). Returns (error or None, approval)."""
    from utils import sun_guard

    site = _site()
    if site is None:
        get_led_manager().set_error(True, critical=False)
        return {"error": "No site set: tracking needs latitude and longitude"}, None
    allowed, reason, approval = sun_guard.confirm_goto(str(name), ra, dec, approval=sun_approval)
    if not allowed:
        log.info("Not going to %s: %s", name, reason)
        return {"error": reason}, None
    if get_parked():
        return {"error": "The telescope is parked: unpark it first"}, None
    if not allow_below_horizon:
        alt, _az, limit = horizon.check(ra, dec, site[0], lst_degrees(clock.now(), site[1]))
        if alt < limit:
            return {"error": f"{name} is below the horizon limit ({alt:.0f} deg up, limit {limit:.0f})"}, None
    return None, approval


def trackCoordinates(name, ra, dec, mag, *, sun_approval=None, allow_below_horizon=False):
    # Slew to J2000 (ra, dec) and track it as the sky turns (see the module
    # docstring). sun_approval: from sun_guard.confirm_goto when a panel
    # flow already asked (keyword only; a server request can't make one).
    # allow_below_horizon: the panel asked "go anyway?".
    try:
        ra = float(ra) % 360.0
        dec = float(dec)
    except (ValueError, TypeError) as e:
        log.warning("Invalid RA or Dec: %s", e)
        get_led_manager().set_error(True, critical=False)
        return {"error": "Invalid RA or Dec"}
    if not -90.0 <= dec <= 90.0:
        return {"error": "Dec must be -90 to 90"}
    if allow_below_horizon is not True:
        allow_below_horizon = False  # only a real True from the panel
    body = ephemeris.find(str(name)) if name else None
    error, approval = _checks(str(name), ra, dec, sun_approval, allow_below_horizon)
    if error:
        return error
    try:
        mag_value = float(mag)
    except (TypeError, ValueError):
        mag_value = None
    target: Dict[str, Any] = {"name": name, "ra": ra, "dec": dec, "mag": mag_value, "below_ok": allow_below_horizon}
    if body and body != "Sun":
        target["body"] = body  # followed with its own rates
    return _start_goto(target, approval)


def track_object(name: str, *, sun_approval=None, allow_below_horizon=False) -> Dict[str, Any]:
    """Go to the Moon, a planet or a named star (from the cached catalogue)
    and track it; works offline."""
    if allow_below_horizon is not True:
        allow_below_horizon = False
    body = ephemeris.find(name)
    if body:
        site = _site()
        ra, dec = ephemeris.position(body, clock.now(), site if body == "Moon" else None)
        p = ephemeris.position_of_date(body, clock.now())
        return trackCoordinates(body, ra, dec, p.get("mag"), sun_approval=sun_approval,
            allow_below_horizon=allow_below_horizon)
    star = find_star(name)
    if star is None:
        return {"error": f"Don't know {name}"}
    return trackCoordinates(star[0], star[1], star[2], star[3], sun_approval=sun_approval,
        allow_below_horizon=allow_below_horizon)


def find_star(name: str):
    """(name, ra, dec, mag) of a named star in the sky data, or None."""
    from utils.manual_control import display_name
    from utils.sky_data import get_sky_data

    data = get_sky_data()
    data.refresh()
    stars, names, _figures, _planets = data.snapshot()
    key = display_name(str(name)).lower()
    for ra, dec, mag, star_id in stars:
        star_name = display_name(names.get(star_id, ""))
        if star_name and star_name.lower() == key:
            return star_name, float(ra), float(dec), float(mag)
    return None


def recentre(timeout_s: float = DITHER_TIMEOUT_S) -> bool:
    """Check the position now and correct it (after the alignment changed,
    or a dither), then wait until it's tracking again."""
    if _target_object is None or not _tracking_active:
        return False
    _publish_state("centering")
    _motion_event.set()
    _wake.set()
    deadline = time.monotonic() + timeout_s
    time.sleep(0.5)
    while time.monotonic() < deadline:
        if _panel_state == "tracking":
            return True
        if not _tracking_active:
            return False
        time.sleep(0.25)
    return False


def dither(arcmin: float = 2.0) -> bool:
    """Shift the pointing a little, at random (up to arcmin), and wait until
    it's tracking again. Between exposures: stacked frames then average out
    fixed noise. Returns whether it settled."""
    target = _target_object
    if target is None or not is_sidereal_tracking():
        return False
    radius = random.uniform(0.3, 1.0) * float(arcmin) / 60.0
    angle = random.uniform(0.0, 2.0 * math.pi)
    dec = float(target.get("dec", 0.0))
    target["offset"] = (radius * math.cos(angle) / max(0.05, math.cos(math.radians(dec))), radius * math.sin(angle))
    log.info("Dither: %.1f arcmin", radius * 60.0)
    return recentre()  # off by the offset now, so it corrects


# --- park --------------------------------------------------------------------------------

def move_to_axes(goal: Tuple[float, float], wait: bool = True, movement: str = "slewing") -> Dict[str, Any]:
    """Slew to axis angles (not a sky target): round the Sun's keep-out
    when the straight way passes it. Tracking must be stopped first."""
    from utils import sun_guard

    _catch_up_position()
    start = get_mount_axes()
    if start is None:
        return {"error": "The position isn't known"}
    lon = _longitude()
    config = mount_config()
    sun = _sun_hour_angle(lon) if lon is not None else None
    keep_out = 0.0
    if sun is not None:
        start_sun = mount_model.separation(mount_model.sky_from_axes(*start, config), sun)
        goal_sun = mount_model.separation(mount_model.sky_from_axes(*goal, config), sun)
        keep_out = sun_guard.keep_out_deg(start_sun, goal_sun)
    route = mount_model.plan_axes_route(start, goal, config, sun=sun, keep_out=keep_out)
    leds = get_led_manager()
    try:
        leds.set_movement(movement)
        _run_route(route, config)
    except Exception as exc:
        leds.set_movement(None)
        return {"error": f"Move refused: {exc}"}
    if wait:
        conn = _get_esp32_connection()
        deadline = time.monotonic() + route.seconds * 2 + 30
        time.sleep(0.3)
        while conn and time.monotonic() < deadline:
            _motion_event.wait(1.0)
            _motion_event.clear()
            if not _motors_moving(conn):
                break
        _update_current_coords_from_motors()
        leds.set_movement(None)
    return {"status": "moved", "seconds": round(route.seconds, 1), "closest_to_sun_deg": round(route.sun_deg, 1)}


def park() -> Dict[str, Any]:
    """Slew to the park position (config mount.park_axes; the pole with the
    counterweight down by default), round the Sun, and stay there: go-tos
    are refused until unpark()."""
    from utils.config_state import get_mount_config

    if get_parked():
        return {"status": "Already parked"}
    stop_tracking()
    _catch_up_position()
    goal = tuple(float(v) for v in get_mount_config()["park_axes"])
    if get_mount_axes() is None:
        set_mount_axes(goal[0], goal[1], source="park", when="now")
        set_parked(True)
        return {"status": "Parked (the position wasn't known: taken as parked)"}
    _publish_state("slewing", target="Park")
    result = move_to_axes(goal, movement="homing")
    _publish_state("idle")
    if "error" in result:
        return result
    set_parked(True)
    get_led_manager().flash_parking_complete()
    from utils.telescope_state import flush

    flush()
    log.info("Parked")
    return {"status": "Parked", "seconds": result.get("seconds")}


def unpark() -> Dict[str, Any]:
    set_parked(False)
    return {"status": "Unparked"}


def set_park_here() -> Dict[str, Any]:
    """Make where the telescope points now the park position."""
    from utils.config_state import get_mount_config, save_section

    _catch_up_position()
    axes = get_mount_axes()
    if axes is None:
        return {"error": "The position isn't known"}
    save_section("mount", {"park_axes": [round(axes[0], 4), round(axes[1], 4)]})
    return {"status": "Park position saved", "park_axes": get_mount_config()["park_axes"]}
