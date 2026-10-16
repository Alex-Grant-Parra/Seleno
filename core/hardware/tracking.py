import threading
import time
from datetime import datetime, timezone
from typing import Optional
from core.hardware import mount as mount_model
from utils.location import get_current_location
from utils.sky_view import lst_degrees, sun_radec
from utils.telescope_state import (set_telescope_coords, get_telescope_coords, get_slew_config, set_target_coords,
    get_target_coords, update_hour_angle, get_mount_axes, set_mount_axes)
from utils.LEDmanager import get_led_manager
from utils.esp32_state import esp32_state
from esp32.pins import get_motor_boards

# Motor control
from esp32.interfaceESP32 import ESP32Connection, ESP32SerialConfig, ESP32Motor


POLARIS_RA_DEG = 37.95456067
POLARIS_DEC_DEG = 89.26410897

# One sidereal day in seconds (Earth's rotation period relative to the stars)
SIDEREAL_DAY_S = 86164.0905
SIDEREAL_DEG_PER_S = 360.0 / SIDEREAL_DAY_S

# Global tracking state
_tracking_thread = None
_tracking_active = False
_target_object = None  # {"name": str, "ra": float, "dec": float, "mag": float}

# Hour angle updater thread (keeps hour angle in sync with Earth's rotation)
_hour_angle_thread = None
_hour_angle_updater_active = False

# Motor initialization flag
_motors_initialized = False

# What the front panel shows: idle, slewing, centering or tracking.
_panel_state = "idle"


def _publish_state(state: str, **fields) -> None:
    # Tell the front panel (utils/panel_ui.py); never let it break tracking.
    global _panel_state
    _panel_state = state
    try:
        from utils.panel_ui import tracking_update

        tracking_update(state=state, **fields)
    except Exception as exc:
        print(f"[tracking] Front panel update failed: {exc}")


def is_sidereal_tracking() -> bool:
    return _tracking_active and _panel_state == "tracking"

# Last ESP32 step counter value read per motor. The axis angles advance by
# the difference between reads; the counter is never reset mid-read, since
# steps taken between a read and a reset would be lost.
_last_motor_steps = {"motor1": 0, "motor2": 0}
_motor_steps_per_rev = 1600.0


def _reset_motor_positions(motor1, motor2) -> None:
    # Zero both ESP32 step counters and the matching read baselines.
    motor1.reset_position()
    motor2.reset_position()
    _last_motor_steps["motor1"] = 0
    _last_motor_steps["motor2"] = 0


def _get_esp32_connection():
    # Get the ESP32 connection from the centralized singleton.
    # This ensures tracking uses the same connection as the rest of the system,
    # with proper reconnection handling if the port changes.
    return esp32_state.ensure_connection()


def _initialize_motors():
    # Initialize motors on the ESP32 if not already done.
    global _motors_initialized
    
    if _motors_initialized:
        return True
    
    conn = _get_esp32_connection()
    if not conn:
        print("[tracking] Cannot initialize motors: ESP32 connection not available")
        return False
    
    try:
        print("[tracking] Initializing motors on ESP32...")
        
        # Check if motors already exist
        try:
            motors = conn.list_motors()
            print(f"[tracking] Existing motors: {motors}")
        except Exception as e:
            print(f"[tracking] Could not list motors: {e}")
            motors = {}

        motor_ids = set(motors.get("motors", [])) if isinstance(motors, dict) else set()

        for board_key, board in get_motor_boards().items():
            motor_id = str(board.get("motor_id", "")).strip()
            label = str(board.get("label", board_key)).strip() or board_key
            pins = board.get("pins", {}) if isinstance(board.get("pins", {}), dict) else {}
            required = bool(board.get("required", False))

            if not motor_id:
                print(f"[tracking] Skipping motor board '{board_key}': missing motor_id")
                continue

            if motor_id in motor_ids:
                # The v0.8 firmware boots with motor1/motor2 registered but
                # disengaged, so an existing motor may still need its driver on.
                print(f"[tracking] {motor_id} ({label}) already exists")
                try:
                    motor = ESP32Motor(conn, motor_id)
                    if bool(board.get("engage", True)) and not motor.status().get("enabled", False):
                        motor.engage()
                        print(f"[tracking] {motor_id} ({label}) engaged")
                except Exception as e:
                    if required:
                        print(f"[tracking] Error engaging required motor {motor_id} ({label}): {e}")
                        return False
                    print(f"[tracking] Warning: could not engage optional motor {motor_id} ({label}): {e}")
                continue

            print(f"[tracking] Creating {motor_id} ({label})...")
            try:
                ESP32Motor.create(
                    conn=conn,
                    motor_id=motor_id,
                    step_pin=int(pins.get("step")),
                    dir_pin=int(pins.get("dir")),
                    en_pin=int(pins.get("en")),
                    steps_per_rev=int(board.get("steps_per_rev", 1600)),
                    engage=bool(board.get("engage", True)),
                    replace=bool(board.get("replace", False)),
                    microsteps=board.get("microsteps"),
                    chopper=board.get("chopper"),
                )
                motor_ids.add(motor_id)
                print(f"[tracking] {motor_id} ({label}) created successfully")
            except Exception as e:
                if required:
                    print(f"[tracking] Error creating required motor {motor_id} ({label}): {e}")
                    import traceback
                    traceback.print_exc()
                    return False

                print(f"[tracking] Warning: Could not create optional motor {motor_id} ({label}): {e}")
                import traceback
                traceback.print_exc()
        
        _motors_initialized = True
        # Start counting from the counters as they are: steps taken before
        # this (another session's slews) are already in the stored axes.
        _resync_motor_baseline(conn)
        print("[tracking] Motor initialization complete")
        return True
        
    except Exception as e:
        print(f"[tracking] Error initializing motors: {e}")
        return False


def _hour_angle_updater_loop() -> None:
    # Background thread that continuously updates hour angle to reflect Earth's rotation.
    # 
    # This runs independently and keeps the current_hour_angle in sync with Earth's rotation,
    # even when the telescope is not actively tracking a target.
    # Runs every second to ensure accurate hour angle at all times.
    global _hour_angle_updater_active
    
    print("[tracking] Hour angle updater thread started")
    
    while _hour_angle_updater_active:
        try:
            # Update the hour angle in state to reflect Earth's rotation
            # RA stays constant, but HA changes as Earth rotates
            updated_ha = update_hour_angle()
            
            # Update every second to keep HA current
            time.sleep(1.0)
        
        except Exception as e:
            print(f"[tracking] Error in hour angle updater loop: {e}")
            time.sleep(1.0)
    
    print("[tracking] Hour angle updater thread stopped")


def _start_hour_angle_updater() -> None:
    # Start the background hour angle updater thread.
    global _hour_angle_thread, _hour_angle_updater_active
    
    if _hour_angle_updater_active:
        return  # Already running
    
    if _hour_angle_thread and _hour_angle_thread.is_alive():
        return  # Thread still alive
    
    _hour_angle_updater_active = True
    _hour_angle_thread = threading.Thread(target=_hour_angle_updater_loop, daemon=True)
    _hour_angle_thread.start()
    print("[tracking] Hour angle updater started")


def _stop_hour_angle_updater() -> None:
    # Stop the background hour angle updater thread.
    global _hour_angle_thread, _hour_angle_updater_active
    
    if not _hour_angle_updater_active:
        return
    
    _hour_angle_updater_active = False
    
    if _hour_angle_thread and _hour_angle_thread.is_alive():
        _hour_angle_thread.join(timeout=5)
    
    _hour_angle_thread = None
    print("[tracking] Hour angle updater stopped")


def _resync_motor_baseline(conn) -> None:
    for motor_id in ("motor1", "motor2"):
        try:
            _last_motor_steps[motor_id] = ESP32Motor(conn, motor_id).get_position()
        except Exception as e:
            print(f"[tracking] Could not read {motor_id}'s position: {e}")


def mount_config(speed_sps: Optional[float] = None) -> mount_model.MountConfig:
    # The mount as configured: gear ratios, speed and how far each axis may
    # turn (config/client_config.json slew_config and mount).
    from utils.config_state import get_mount_config

    slew = get_slew_config()
    limits = get_mount_config()
    return mount_model.MountConfig(
        ra_gear=slew["ra_gear_ratio"],
        dec_gear=slew["dec_gear_ratio"],
        steps_per_rev=_motor_steps_per_rev,
        slew_sps=speed_sps or slew["slew_speed_sps"],
        ra_limits=tuple(limits["ra_axis_limits"]),
        dec_limits=tuple(limits["dec_axis_limits"]),
        pole_crossing=limits["pole_crossing"],
    )


def _sun_hour_angle(longitude: float, when: Optional[datetime] = None):
    when = when or datetime.now(timezone.utc)
    sun_ra, sun_dec = sun_radec(when)
    return mount_model.wrap180(lst_degrees(when, longitude) - sun_ra), sun_dec


def _longitude() -> Optional[float]:
    location = get_current_location() or {}
    lon = location.get("longitude")
    return float(lon) if lon is not None else None


def plan_goto(ra: float, dec: float, start_axes=None, speed_sps: Optional[float] = None):
    # The route from the axes (default: where the telescope is) to point at
    # (ra, dec), aimed where the target will be when it gets there: the
    # quickest way, over a pole if that's quicker, and curving round the
    # Sun's keep-out (utils/sun_guard.keep_out_deg). None without a site
    # or a known position.
    from utils import sun_guard

    lon = _longitude()
    start = start_axes if start_axes is not None else get_mount_axes()
    if lon is None or start is None:
        return None
    config = mount_config(speed_sps)
    now = datetime.now(timezone.utc)
    lst = lst_degrees(now, lon)
    sun = _sun_hour_angle(lon, now)
    start_sun = mount_model.separation(mount_model.sky_from_axes(*start), sun)
    route = None
    lead = 0.0
    for _ in range(2):
        target = (mount_model.wrap180(lst + lead - ra), dec)
        keep_out = sun_guard.keep_out_deg(start_sun, mount_model.separation(target, sun))
        route = mount_model.plan_route(start, target, config, sun=sun, keep_out=keep_out)
        lead = route.seconds * SIDEREAL_DEG_PER_S
    return route


def _update_current_coords_from_motors() -> None:
    # Add the steps each motor has taken since the last read to the axis
    # angles (core/hardware/mount.py); the RA/Dec follow from them.
    global _motor_steps_per_rev
    try:
        conn = _get_esp32_connection()
        if not conn:
            return
        deltas = {"motor1": 0, "motor2": 0}
        for motor_id in ("motor1", "motor2"):
            motor = ESP32Motor(conn, motor_id)
            try:
                position = motor.get_position()
            except Exception as e:
                print(f"[tracking] Error reading {motor_id} position: {e}")
                continue
            deltas[motor_id] = position - _last_motor_steps[motor_id]
            _last_motor_steps[motor_id] = position
            if motor_id == "motor1":
                _motor_steps_per_rev = float(motor.steps_per_rev or _motor_steps_per_rev)
            time.sleep(0.05)  # Small delay between commands to avoid overwhelming ESP32
        axes = get_mount_axes()
        if axes is None:
            return
        d_ra, d_dec = mount_config().axis_degrees(deltas["motor1"], deltas["motor2"])
        if d_ra or d_dec:
            set_mount_axes(axes[0] + d_ra, axes[1] + d_dec, source="tracking")
            print(f"[tracking] Axes moved RA {d_ra:+.5f}°, Dec {d_dec:+.5f}° "
                  f"(steps {deltas['motor1']}, {deltas['motor2']}); now {axes[0] + d_ra:.4f}°, {axes[1] + d_dec:.4f}°")
    except Exception as e:
        print(f"[tracking] Error updating current coords from motors: {e}")


def _wait_until_stopped(conn, timeout_s: float = 5.0) -> None:
    # A stop from slew speed ramps down (up to about a second).
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if not any(ESP32Motor(conn, m).status().get("moving") for m in ("motor1", "motor2")):
                return
        except Exception as e:
            print(f"[tracking] Warning: Error reading motor status: {e}")
            return
        time.sleep(0.1)


def _run_route(route, config: mount_model.MountConfig) -> None:
    # Drive both motors along the route as one smooth path ({"cmd":"path"}).
    # Doesn't wait; the tracking loop watches for the end. Raises if the
    # ESP32 refuses it.
    global _motors_initialized
    conn = _get_esp32_connection()
    if not conn:
        raise RuntimeError("ESP32 connection not available")
    try:
        motors = conn.list_motors()
        if "motor1" not in str(motors) or "motor2" not in str(motors):
            _motors_initialized = False
    except Exception as e:
        print(f"[tracking] Could not verify motors: {e}")
        _motors_initialized = False
    if not _initialize_motors():
        raise RuntimeError("Motors not ready")
    for motor_id in ("motor1", "motor2"):
        motor = ESP32Motor(conn, motor_id)
        try:
            if not motor.status().get("enabled", False):
                motor.engage()
        except Exception as e:
            print(f"[tracking] Error engaging {motor_id}: {e}")
    steps = mount_model.route_steps(route, config)
    if not any(a or b for a, b in steps):
        print("[tracking] Already there: nothing to move")
        return
    shape = "round the Sun" if route.detour else "straight"
    side = ", over the pole" if route.flips else ""
    print(f"[tracking] Slewing {shape}{side}: {len(steps)} points, about {route.seconds:.0f} s at "
          f"{config.slew_sps:.0f} sps, closest to the Sun {route.sun_deg:.1f}°")
    conn.path(steps, config.slew_sps)


def _sun_and_limits_problem() -> Optional[str]:
    # Whether tracking has to stop: pointing (or aimed) closer to the Sun
    # than this session was allowed, or an axis past its limit.
    target = _target_object or {}
    axes = get_mount_axes()
    lon = _longitude()
    if axes is None or lon is None or "ra" not in target:
        return None
    sun = _sun_hour_angle(lon)
    pointing = mount_model.sky_from_axes(*axes)
    lst = lst_degrees(datetime.now(timezone.utc), lon)
    aimed = (mount_model.wrap180(lst - float(target["ra"])), float(target["dec"]))
    floor = float(target.get("sun_floor", 19.5))
    closest = min(mount_model.separation(pointing, sun), mount_model.separation(aimed, sun))
    if closest < floor:
        return f"{closest:.0f} deg from the Sun: tracking stopped"
    if not mount_config().within(axes, slack=2.0):
        return "An axis reached its limit: tracking stopped"
    return None


def _halt_from_loop(reason: str) -> None:
    # Stop tracking from inside the tracking thread (stop_tracking would wait
    # for this thread) and say why on the front panel.
    global _tracking_active, _target_object
    print(f"[tracking] {reason}")
    _tracking_active = False
    _target_object = None
    conn = _get_esp32_connection()
    if conn:
        for motor_id in ("motor1", "motor2"):
            try:
                conn.send({"cmd": "stop", "motor": motor_id})
            except Exception as e:
                print(f"[tracking] Error stopping {motor_id}: {e}")
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


def _start_ra_tracking() -> None:
    # Start RA motor in continuous tracking mode at the configured sidereal rate.
    # 
    # This is called once when the telescope has slewed to the target.
    # The motor will run indefinitely until stopped, automatically compensating
    # for Earth's rotation at the sidereal rate.
    try:
        conn = _get_esp32_connection()
        if not conn:
            print("[tracking] Error: ESP32 connection not available; cannot start tracking")
            return
        
        config = get_slew_config()
        ra_gear_ratio = config.get("ra_gear_ratio", 360.0)
        motor1 = ESP32Motor(conn, "motor1")
        motor2 = ESP32Motor(conn, "motor2")
        # Steps per revolution follow the driver's microstep setting, which
        # the firmware reports (it can be changed at runtime over the TMC UART).
        ra_steps_per_rev = motor1.steps_per_rev
        # One output revolution per sidereal day: ra_gear_ratio motor revolutions
        tracking_speed_sps = ra_gear_ratio * ra_steps_per_rev / SIDEREAL_DAY_S
        
        # Stop any existing movement on both motors first
        print(f"[tracking] Stopping all motors before starting continuous tracking")
        try:
            motor1.stop()
            motor2.stop()
        except Exception as e:
            print(f"[tracking] Warning: Error stopping motors: {e}")
        
        # A stop from slew speed ramps down (up to about a second), so wait
        # until both have actually stopped rather than a fixed pause.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if not motor1.status().get("moving") and not motor2.status().get("moving"):
                    break
            except Exception as e:
                print(f"[tracking] Warning: Error reading motor status: {e}")
                break
            time.sleep(0.1)
        
        # Set speed to configured tracking rate
        print(f"[tracking] Setting RA motor speed to {tracking_speed_sps:.4f} sps (sidereal, {ra_gear_ratio}:1)")
        motor1.set_speed_sps(tracking_speed_sps)
        
        # Start continuous motion at sidereal rate (forward = tracking east)
        print(f"[tracking] ⭐ Starting continuous RA tracking EASTWARD (forward=True) at sidereal rate")
        motor1.start_continuous(forward=True)
        
        print(f"[tracking] ✓ RA motor now tracking continuously at {tracking_speed_sps:.4f} sps")
    
    except Exception as e:
        print(f"[tracking] Error starting RA tracking: {e}")


def _continuous_tracking_loop() -> None:
    # Background thread that monitors tracking and updates coordinates.
    # 
    # Once slew is complete, this thread:
    # 1. Monitors motor status to detect when slew is complete
    # 2. Verifies we're at the target position
    # 3. Starts continuous tracking at the configured speed
    global _tracking_active, _target_object
    leds = get_led_manager()
    
    print("[tracking] Continuous tracking thread started")
    
    # Track whether we've started sidereal tracking
    sidereal_tracking_active = False
    # Track if we've already logged waiting state (to avoid spam)
    waiting_logged = False
    coord_update_interval = 0
    
    while _tracking_active:
        try:
            if _target_object is None:
                time.sleep(1)
                continue
            
            # Update current position from actual motor movements
            _update_current_coords_from_motors()
            now_coords = get_telescope_coords() or {}
            if now_coords:
                _publish_state(_panel_state, ra_now=now_coords.get('right_ascension'),
                               dec_now=now_coords.get('declination'))
            
            # Log current coordinates periodically for debugging
            coord_update_interval += 1
            if coord_update_interval >= 5:  # Log every 5 cycles
                coords = get_telescope_coords() or {}
                print(f"[tracking] Periodic status: Current RA={coords.get('right_ascension', 0):.4f}°, Dec={coords.get('declination', 0):.4f}°")
                coord_update_interval = 0
            
            # Update hour angle to reflect Earth's rotation
            update_hour_angle()
            
            # Never closer to the Sun than this session was allowed, nor past
            # an axis limit (utils/sun_guard.py, core/hardware/mount.py).
            problem = _sun_and_limits_problem()
            if problem:
                _halt_from_loop(problem)
                break
            
            # Check if we should start sidereal tracking
            if not sidereal_tracking_active:
                conn = _get_esp32_connection()
                if conn:
                    try:
                        # Check both motors' status with delays between commands
                        motor1 = ESP32Motor(conn, "motor1")
                        motor2 = ESP32Motor(conn, "motor2")
                        
                        try:
                            motor1_status = motor1.status()
                            motor1_moving = motor1_status.get("moving", False)
                        except Exception as e:
                            # If we can't get status, assume still moving and skip this cycle
                            if "ESP32 error" not in str(e):
                                print(f"[tracking] Warning: Could not get motor1 status: {e}")
                            motor1_moving = True  # Assume moving to be safe
                        
                        time.sleep(0.1)  # Delay between status commands
                        
                        try:
                            motor2_status = motor2.status()
                            motor2_moving = motor2_status.get("moving", False)
                        except Exception as e:
                            # If we can't get status, assume still moving and skip this cycle
                            if "ESP32 error" not in str(e):
                                print(f"[tracking] Warning: Could not get motor2 status: {e}")
                            motor2_moving = True  # Assume moving to be safe
                        
                        # Debug: Log if only one motor is moving (only once to avoid spam)
                        if motor1_moving or motor2_moving:
                            if not waiting_logged:
                                if motor1_moving and not motor2_moving:
                                    print(f"[tracking] RA motor still moving, Dec stopped - waiting for RA to finish")
                                elif motor2_moving and not motor1_moving:
                                    print(f"[tracking] Dec motor still moving, RA stopped - waiting for Dec to finish")
                                elif motor1_moving and motor2_moving:
                                    print(f"[tracking] Both motors still moving - waiting...")
                                waiting_logged = True
                        
                        # Only proceed if BOTH motors have stopped
                        if not motor1_moving and not motor2_moving:
                            waiting_logged = False  # Reset for next slew
                            print(f"[tracking] Both motors stopped - checking position")
                            
                            # Verify we're actually at the target: the axes that
                            # point at it now, on the side the mount is on.
                            axes = get_mount_axes()
                            target = _target_object or {}
                            lon = _longitude()
                            if axes is not None and lon is not None and "ra" in target:
                                config = mount_config()
                                lst = lst_degrees(datetime.now(timezone.utc), lon)
                                goal = mount_model.nearest_solution(
                                    mount_model.wrap180(lst - float(target["ra"])), float(target["dec"]), axes, config)
                                delta_ra_axis = goal[0] - axes[0]
                                delta_dec_axis = goal[1] - axes[1]
                                
                                slew = get_slew_config()
                                centered_threshold = slew.get("centered_threshold_degrees", 0.01)
                                
                                print(f"[tracking] Position check: RA axis error={delta_ra_axis:.6f}°, "
                                      f"Dec axis error={delta_dec_axis:.6f}° (centred within {centered_threshold}°)")
                                
                                # If we're centered on target, start sidereal tracking
                                if abs(delta_ra_axis) < centered_threshold and abs(delta_dec_axis) < centered_threshold:
                                    print(f"[tracking] ✓ Within centered threshold - starting sidereal tracking")
                                    leds.flash_tracking_locked()
                                    leds.set_tracking(True, pulse=False)
                                    leds.set_command_busy(False)
                                    _start_ra_tracking()
                                    sidereal_tracking_active = True
                                    # From now on it holds on the target: as close to the
                                    # Sun as the target was allowed, no closer.
                                    _target_object["sun_floor"] = _target_object.get("sun_floor_tracking", 19.5)
                                    coords = get_telescope_coords() or {}
                                    _publish_state("tracking", ra_now=coords.get("right_ascension"),
                                                   dec_now=coords.get("declination"))
                                # If we're off target but motors stopped, issue correction slew
                                else:
                                    print(f"[tracking] ⚠ Off target - issuing correction")
                                    small = max(abs(delta_ra_axis), abs(delta_dec_axis)) < slew.get("slew_threshold_degrees", 1.0)
                                    speed = slew["refine_speed_sps"] if small else slew["slew_speed_sps"]
                                    route = plan_goto(float(target["ra"]), float(target["dec"]), speed_sps=speed)
                                    _publish_state("centering")
                                    if route is not None:
                                        try:
                                            _run_route(route, mount_config(speed))
                                        except Exception as e:
                                            print(f"[tracking] Correction refused: {e}")
                                    waiting_logged = False  # Reset for next wait cycle
                    except Exception as e:
                        print(f"[tracking] Error checking motor status: {e}")
            
            # Sleep to reduce ESP32 load while still getting reasonable update frequency
            time.sleep(1.5)  # Check every 1.5 seconds - balance between responsiveness and ESP32 load
        
        except Exception as e:
            print(f"[tracking] Error in tracking loop: {e}")
            time.sleep(2.0)  # Wait longer after errors
    
    print("[tracking] Continuous tracking thread stopped")


def stop_tracking(set_leds_idle: bool = True) -> None:
    # Stop continuous tracking and motors.
    global _tracking_active, _tracking_thread, _target_object
    leds = get_led_manager()
    
    if _tracking_active:
        print("[tracking] Stopping continuous tracking")
        _tracking_active = False
        _publish_state("idle")
        
        # Wait for thread to finish
        if _tracking_thread and _tracking_thread.is_alive():
            _tracking_thread.join(timeout=5)
        
        _target_object = None
        _tracking_thread = None
        
        print("[tracking] Tracking stopped")
    
    # Stop all motors
    conn = _get_esp32_connection()
    if conn:
        print("[tracking] Stopping motors...")
        try:
            # Stop motor1 (RA)
            try:
                conn.send({"cmd": "stop", "motor": "motor1"})
                print("[tracking] Motor1 (RA) stopped")
            except Exception as e:
                print(f"[tracking] Error stopping motor1: {e}")
            
            # Stop motor2 (DEC)
            try:
                conn.send({"cmd": "stop", "motor": "motor2"})
                print("[tracking] Motor2 (DEC) stopped")
            except Exception as e:
                print(f"[tracking] Error stopping motor2: {e}")
        except Exception as e:
            print(f"[tracking] Error stopping motors: {e}")
    else:
        print("[tracking] Warning: ESP32 connection not available to stop motors")

    if set_leds_idle:
        leds.set_idle()


def stop_tracking_for_estop() -> None:
    # The ESP32 has latched its emergency stop and halted the motors itself.
    # Stop the Pi's tracking loop so it doesn't keep issuing moves; the stop
    # commands stop_tracking() sends are still accepted while latched.
    print("[tracking] Emergency stop: stopping tracking")
    stop_tracking(set_leds_idle=False)


def on_esp32_reboot() -> None:
    # A restarted ESP32 has its motors disengaged and step counters at zero.
    # Re-create/engage on the next move and restart position deltas from 0.
    global _motors_initialized
    _motors_initialized = False
    _last_motor_steps["motor1"] = 0
    _last_motor_steps["motor2"] = 0
    print("[tracking] ESP32 restarted: motors will be re-initialised before the next move")


def sync_position(ra: float, dec: float) -> None:
    # The telescope has been centred on a known object by hand (front panel
    # "Align on star"): stop, and take that as the current position. The
    # step counters restart too, so moves made outside tracking (the panel's
    # jog) aren't added to it.
    stop_tracking()
    conn = _get_esp32_connection()
    if conn:
        _wait_until_stopped(conn)
        try:
            _reset_motor_positions(ESP32Motor(conn, "motor1"), ESP32Motor(conn, "motor2"))
        except Exception as e:
            print(f"[tracking] Warning: could not reset the step counters: {e}")
    # The axes follow, on the side of the mount it's on now.
    set_telescope_coords(float(ra), float(dec), source="manual_reset")
    print(f"[tracking] Position set by hand: RA={float(ra):.4f}°, Dec={float(dec):.4f}°")


def _set_polaris_alignment(location: dict) -> None:
    # Set telescope to Polaris coordinates for polar alignment.
    longitude = location.get('longitude')
    if longitude is None:
        print("[tracking] Warning: Longitude missing; cannot set Polaris alignment.")
        return
    # Store Polaris RA (not HA) in state
    set_telescope_coords(POLARIS_RA_DEG, POLARIS_DEC_DEG, source="polaris")


def trackCoordinates(name, ra, dec, mag, *, sun_approval=None):
    # Slew to a celestial object and track it as the sky turns.
    # 
    # The telescope will:
    # 1. Check the Sun (utils/sun_guard.py): near it, someone at the front
    #    panel has to confirm first; nothing moves or stops if they don't
    # 2. Stop any existing tracking
    # 3. Slew the quickest way (core/hardware/mount.py): over a pole if that's
    #    quicker, curving round the Sun if the straight way passes it, as one
    #    smooth move of both axes
    # 4. Correct any small error, then track at the sidereal rate
    # 
    # Args:
    #     name: Object name
    #     ra: Right ascension in degrees (can be string or float)
    #     dec: Declination in degrees (can be string or float)
    #     mag: Magnitude
    #     sun_approval: from sun_guard.confirm_goto when a panel flow already
    #         asked (keyword only; a server request can't make one)
    global _tracking_active, _tracking_thread, _target_object
    leds = get_led_manager()
    
    # Convert string inputs to floats if needed
    try:
        ra = float(ra) % 360.0
        dec = float(dec)
    except (ValueError, TypeError) as e:
        print(f"[tracking] Error: Invalid RA or Dec format: {e}")
        leds.set_error(True, critical=False)
        return {"error": "Invalid RA or Dec"}
    if not -90.0 <= dec <= 90.0:
        return {"error": "Dec must be -90 to 90"}
    
    location = get_current_location()
    if location is None or location.get('longitude') is None or location.get('latitude') is None:
        print("[tracking] Warning: Location data not available. Cannot calculate hour angle.")
        leds.set_error(True, critical=False)
        return {"error": "No site set: tracking needs latitude and longitude"}
    
    from utils import sun_guard
    allowed, reason, approval = sun_guard.confirm_goto(str(name), ra, dec, approval=sun_approval)
    if not allowed:
        print(f"[tracking] Not going to {name}: {reason}")
        return {"error": reason}
    
    # If already tracking, stop the previous tracking session first
    if _tracking_active:
        print(f"[tracking] ⚠ Already tracking - stopping previous session before acquiring new target")
        stop_tracking()
        time.sleep(0.5)  # Brief pause to ensure tracking fully stops
    
    print(f"[tracking] Acquiring target: {name}, RA: {ra}, Dec: {dec}, Mag: {mag}")
    print(f"[tracking] Observer location: Longitude={location.get('longitude')}, Latitude={location.get('latitude')}")
    leds.clear_error()
    leds.set_command_busy(True)

    # Poll ifAligned() until telescope is polar aligned
    print(f"[tracking] Waiting for polar alignment before tracking...")
    while not ifAligned():
        print(f"[tracking] Telescope not aligned. Checking again in 1 second...")
        time.sleep(1)
    print(f"[tracking] Telescope is aligned. Proceeding with tracking.")

    try:
        mag_value = float(mag)
    except (TypeError, ValueError):
        mag_value = None
    _publish_state("slewing", target=str(name), ra=ra, dec=dec, mag=mag_value)

    # Bring the position up to date (any motion has stopped first).
    conn = _get_esp32_connection()
    if conn and _initialize_motors():
        _wait_until_stopped(conn)
        _update_current_coords_from_motors()
    if get_mount_axes() is None:
        # Nothing known about where it points: start from the target.
        print("[tracking] WARNING: No known position; taking the target as the starting point")
        set_telescope_coords(ra, dec, source="assumed")

    route = plan_goto(ra, dec)
    if route is None:
        _publish_state("idle")
        leds.set_command_busy(False)
        return {"error": "Couldn't plan the slew"}
    if route.notes:
        print(f"[tracking] Note: {'; '.join(route.notes)}")

    # How close to the Sun this session may get: on the way, and then on
    # the target (the watchdog in the tracking loop holds it to that).
    target_sun = mount_model.separation(
        (mount_model.wrap180(lst_degrees(datetime.now(timezone.utc), float(location['longitude'])) - ra), dec),
        _sun_hour_angle(float(location['longitude'])))
    danger = sun_guard.DANGER_DEG - 0.5
    _target_object = {
        "name": name, "ra": ra, "dec": dec, "mag": mag,
        "sun_floor": min(danger, route.sun_deg - 1.0),
        "sun_floor_tracking": min(danger, target_sun - 1.0) if approval is not None else danger,
    }
    set_target_coords(ra, dec, source="tracking")

    config = mount_config()
    try:
        leds.set_movement("slewing")
        _run_route(route, config)
    except Exception as e:
        print(f"[tracking] Slew refused: {e}")
        _target_object = None
        _publish_state("idle")
        leds.set_error(True, critical=False)
        leds.set_command_busy(False)
        return {"error": f"Slew refused: {e}"}
    
    # Start continuous tracking thread if not already running
    if not _tracking_active:
        _tracking_active = True
        _tracking_thread = threading.Thread(target=_continuous_tracking_loop, daemon=True)
        _tracking_thread.start()
        _start_hour_angle_updater()  # Also start the background hour angle updater
        print(f"[tracking] Continuous tracking started for {name}")

    return {
        "status": "slewing",
        "seconds": round(route.seconds, 1),
        "round_the_sun": route.detour,
        "over_the_pole": route.flips,
        "closest_to_sun_deg": round(route.sun_deg, 1),
    }


def ifAligned():
    # placeholder, will link to a momentary button in the future
    # will return true if the telescope is polar aligned, otherwise false
    return True


# Automatically start the hour angle updater when the module is loaded
_start_hour_angle_updater()

