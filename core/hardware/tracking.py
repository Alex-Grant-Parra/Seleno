import threading
import time
from utils.location import get_current_location
from utils.Tools import hour_angle
from utils.telescope_state import set_telescope_coords, get_telescope_coords, get_slew_config, set_target_coords, get_target_coords, update_hour_angle
from utils.LEDmanager import get_led_manager
from utils.esp32_state import esp32_state
from esp32.pins import get_motor_boards

# Motor control
from esp32.interfaceESP32 import ESP32Connection, ESP32SerialConfig, ESP32Motor


POLARIS_RA_DEG = 37.95456067
POLARIS_DEC_DEG = 89.26410897

# One sidereal day in seconds (Earth's rotation period relative to the stars)
SIDEREAL_DAY_S = 86164.0905

# Global tracking state
_tracking_thread = None
_tracking_active = False
_target_object = None  # {"name": str, "ra": float, "dec": float, "mag": float}

# Hour angle updater thread (keeps hour angle in sync with Earth's rotation)
_hour_angle_thread = None
_hour_angle_updater_active = False

# Motor initialization flag
_motors_initialized = False

# (ra, ha) written by the last motor-position update. The mount holds a fixed
# HA while its motor is stopped, so the next update must start from this HA
# rather than re-deriving it from RA at the current time.
_last_ha_baseline = None

# Last ESP32 step counter value read per motor. Coordinates advance by the
# difference between reads; the counter is never reset mid-read, since steps
# taken between a read and a reset would be lost.
_last_motor_steps = {"motor1": 0, "motor2": 0}


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
                print(f"[tracking] {motor_id} ({label}) already exists")
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
                    replace=bool(board.get("replace", True)),
                )
                motor_ids.add(motor_id)
                _last_motor_steps[motor_id] = 0
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


def _update_current_coords_from_motors() -> None:
    # Update the current telescope coordinates based on actual motor positions.
    # 
    # Reads the ESP32 step counters and applies the change since the previous read
    # to the current coordinates.
    # 
    # This ensures that the current RA/DEC always reflects the actual position based on
    # how much the motors have turned, accounting for real-world movement inaccuracies.
    global _last_ha_baseline
    try:
        conn = _get_esp32_connection()
        if not conn:
            return
        
        # Get motor positions in raw steps
        config = get_slew_config()
        motor1 = ESP32Motor(conn, "motor1")
        motor2 = ESP32Motor(conn, "motor2")
        
        ra_gear_ratio = config.get("ra_gear_ratio", 360.0)
        dec_gear_ratio = config.get("dec_gear_ratio", 144.0)
        
        # Step deltas since the previous read (0 if a read fails, so no movement is lost)
        motor1_steps = 0
        motor2_steps = 0
        
        try:
            position = motor1.get_position()
            motor1_steps = position - _last_motor_steps["motor1"]
            _last_motor_steps["motor1"] = position
        except Exception as e:
            # Log ALL position read failures for debugging
            print(f"[tracking] Error reading motor1 position: {e}")
            
        time.sleep(0.05)  # Small delay between commands to avoid overwhelming ESP32
        
        try:
            position = motor2.get_position()
            motor2_steps = position - _last_motor_steps["motor2"]
            _last_motor_steps["motor2"] = position
        except Exception as e:
            # Log ALL position read failures for debugging
            print(f"[tracking] Error reading motor2 position: {e}")
        
        # Convert motor steps to sky degrees using gear ratios
        # A X:1 gearbox means X motor revolutions = 1 output revolution = 360° sky
        # So: sky_degrees = (motor_steps / steps_per_rev) * (360 / gear_ratio)
        # Example RA (360:1): 1 motor rev = 360/360 = 1° sky
        # Example DEC (144:1): 1 motor rev = 360/144 = 2.5° sky
        motor1_delta_deg = (motor1_steps / motor1._steps_per_rev) * (360.0 / ra_gear_ratio)
        motor2_delta_deg = (motor2_steps / motor2._steps_per_rev) * (360.0 / dec_gear_ratio)
        
        # Get last known current coordinates
        coords = get_telescope_coords() or {}
        last_known_ra = coords.get('right_ascension', 0.0)
        last_known_dec = coords.get('declination', 0.0)
        
        # Motors move in Hour Angle space, not RA space
        # We need to convert HA position to RA using current time/location
        # Formula: RA = LST - HA
        
        # Get current location for LST calculation
        from utils.location import get_current_location
        from utils.Tools import hour_angle
        location = get_current_location()
        if location:
            longitude = location.get('longitude', 0)
            
            # Baseline HA is where the mount pointed at the last update. Reuse it
            # when the stored RA is still the one we wrote; recomputing it from RA
            # at the current time would count Earth's rotation twice while tracking.
            if _last_ha_baseline is not None and _last_ha_baseline[0] == last_known_ra:
                last_known_ha = _last_ha_baseline[1]
            else:
                last_known_ha = hour_angle(last_known_ra, longitude) if last_known_ra != 0.0 else 0.0
            
            # Add motor movement to HA (motors track in HA space)
            updated_ha = last_known_ha + motor1_delta_deg
            
            # Convert updated HA back to RA using current LST
            # We need to reverse the hour_angle formula: RA = LST - HA
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            D = now.day + now.hour/24 + now.minute/1440 + now.second/86400
            Y, M = now.year, now.month
            if M <= 2:
                Y -= 1
                M += 12
            A = Y // 100
            B = 2 - A + A // 4
            JD = int(365.25*(Y + 4716)) + int(30.6001*(M + 1)) + D + B - 1524.5
            GMST = 280.46061837 + 360.98564736629 * (JD - 2451545)
            LST = (GMST + longitude) % 360
            
            # RA = LST - HA (with normalization to 0-360)
            updated_ra = (LST - updated_ha) % 360
            _last_ha_baseline = (float(updated_ra), updated_ha)
        else:
            # Fallback if location not available (shouldn't happen but just in case)
            updated_ra = last_known_ra + motor1_delta_deg
        
        # DEC moves directly (not affected by Earth's rotation)
        updated_dec = last_known_dec + motor2_delta_deg
        
        # Calculate new current position based on motor movements
        # updated_ra and updated_dec are already calculated above
        
        # Update state with actual current position
        set_telescope_coords(updated_ra, updated_dec, source="tracking")
        
        if motor1_delta_deg != 0 or motor2_delta_deg != 0:
            print(f"[tracking] Updated current from motors: HA delta={motor1_delta_deg:.6f}°, Dec delta={motor2_delta_deg:.6f}°")
            print(f"[tracking] New current position: RA={updated_ra:.4f}°, Dec={updated_dec:.4f}°")
        else:
            # Log periodically even when stationary to help debug stuck coordinates
            print(f"[tracking] Motor deltas are zero. Current position: RA={updated_ra:.4f}°, Dec={updated_dec:.4f}° (motor1_steps={motor1_steps}, motor2_steps={motor2_steps})")
    
    except Exception as e:
        print(f"[tracking] Error updating current coords from motors: {e}")


def _move_motors(delta_ha: float, delta_dec: float) -> None:
    # Move motors to compensate for HA and Dec deltas using multi-phase slewing.
    # 
    # Applies gear ratios to scale motor movements correctly.
    # DOES NOT BLOCK - returns immediately after sending commands.
    # Motor movements happen asynchronously; use _update_current_coords_from_motors() to track progress.
    # 
    # Args:
    #     delta_ha: Hour angle difference in degrees (positive = east/forward)
    #     delta_dec: Declination difference in degrees (positive = north)
    conn = _get_esp32_connection()
    if not conn:
        print("[tracking] Warning: ESP32 connection not available; cannot move motors")
        return
    
    # Verify motors exist on ESP32 and re-initialize if needed
    try:
        motors = conn.list_motors()
        if "motor1" not in str(motors) or "motor2" not in str(motors):
            print(f"[tracking] Motors not found on ESP32 (existing: {motors}), reinitializing...")
            global _motors_initialized
            _motors_initialized = False  # Force re-initialization
    except Exception as e:
        print(f"[tracking] Could not verify motors: {e}, attempting initialization...")
        _motors_initialized = False
    
    # Ensure motors are initialized before trying to move them
    if not _initialize_motors():
        print("[tracking] Warning: Motors not initialized; cannot move")
        return
    
    config = get_slew_config()
    
    # Apply gear ratios to convert sky movement to motor movement
    # For a 360:1 gearbox, a 1° sky movement requires 360° motor rotation
    ra_gear_ratio = config.get("ra_gear_ratio", 360.0)
    dec_gear_ratio = config.get("dec_gear_ratio", 144.0)
    
    motor_delta_ha = delta_ha * ra_gear_ratio
    motor_delta_dec = delta_dec * dec_gear_ratio
    
    # Absolute distances to target (in motor degrees)
    abs_motor_delta_ha = abs(motor_delta_ha)
    abs_motor_delta_dec = abs(motor_delta_dec)
    
    # Thresholds are in sky degrees, so we need to scale them by EACH motor's gear ratio
    slew_threshold_motor_ra = config["slew_threshold_degrees"] * ra_gear_ratio
    slew_threshold_motor_dec = config["slew_threshold_degrees"] * dec_gear_ratio
    
    print(f"[tracking] Sky deltas: HA={delta_ha:.4f}°, Dec={delta_dec:.4f}°")
    print(f"[tracking] Motor deltas (with gear ratios {ra_gear_ratio}:1, {dec_gear_ratio}:1): HA={motor_delta_ha:.4f}°, Dec={motor_delta_dec:.4f}°")
    print(f"[tracking] Slew thresholds (motor °): RA={slew_threshold_motor_ra:.4f}°, Dec={slew_threshold_motor_dec:.4f}°")
    
    try:
        # Create motor instances
        motor1 = ESP32Motor(conn, "motor1")
        motor2 = ESP32Motor(conn, "motor2")
        
        # Engage motors before movement (they may be disengaged for safety)
        print("[tracking] Engaging motors before movement...")
        try:
            motor1.engage()
            print("[tracking] Motor1 (RA) engaged")
        except Exception as e:
            print(f"[tracking] Error engaging motor1: {e}")
        
        try:
            motor2.engage()
            print("[tracking] Motor2 (DEC) engaged")
        except Exception as e:
            print(f"[tracking] Error engaging motor2: {e}")
        
        # RA Motor: Choose speed based on distance (non-blocking)
        if abs_motor_delta_ha > slew_threshold_motor_ra:
            # Phase 1: Slew at high speed
            try:
                direction = "EAST/forward" if delta_ha > 0 else "WEST/backward"
                print(f"[tracking] RA motor slewing: {motor_delta_ha:.4f}° (motor) for {delta_ha:.4f}° (sky) {direction} at {config['slew_speed_sps']:.1f} sps")
                motor1.set_speed_sps(config["slew_speed_sps"])
                motor1.turn_degrees(abs_motor_delta_ha, forward=delta_ha > 0)  # Non-blocking
                print(f"[tracking] RA motor slew command sent (forward={delta_ha > 0})")
            except Exception as e:
                print(f"[tracking] Error moving RA motor: {e}")
        elif abs_motor_delta_ha > 0.01:  # Minimum 0.01° motor movement (prevents tiny movements)
            # Phase 2: Refine at medium speed (also used for fine corrections)
            try:
                direction = "EAST/forward" if delta_ha > 0 else "WEST/backward"
                print(f"[tracking] RA motor refining: {motor_delta_ha:.4f}° (motor) {direction} at {config['refine_speed_sps']:.1f} sps")
                motor1.set_speed_sps(config["refine_speed_sps"])
                motor1.turn_degrees(abs_motor_delta_ha, forward=delta_ha > 0)  # Non-blocking
                print(f"[tracking] RA motor refine command sent (forward={delta_ha > 0})")
            except Exception as e:
                print(f"[tracking] Error refining RA motor: {e}")
        else:
            print(f"[tracking] RA already at target (delta={motor_delta_ha:.6f}° motor)")
        
        # DEC Motor: Choose speed based on distance (non-blocking)
        if abs_motor_delta_dec > slew_threshold_motor_dec:
            # Phase 1: Slew at high speed
            try:
                direction = "NORTH/forward" if delta_dec > 0 else "SOUTH/backward"
                print(f"[tracking] DEC motor slewing: {motor_delta_dec:.4f}° (motor) for {delta_dec:.4f}° (sky) {direction} at {config['slew_speed_sps']:.1f} sps")
                motor2.set_speed_sps(config["slew_speed_sps"])
                motor2.turn_degrees(abs_motor_delta_dec, forward=delta_dec > 0)  # Non-blocking
                print(f"[tracking] DEC motor slew command sent (forward={delta_dec > 0})")
            except Exception as e:
                print(f"[tracking] Error moving DEC motor: {e}")
        elif abs_motor_delta_dec > 0.01:  # Minimum 0.01° motor movement (prevents tiny movements)
            # Phase 2: Refine at medium speed (also used for fine corrections)
            try:
                direction = "NORTH/forward" if delta_dec > 0 else "SOUTH/backward"
                print(f"[tracking] DEC motor refining: {motor_delta_dec:.4f}° (motor) {direction} at {config['refine_speed_sps']:.1f} sps")
                motor2.set_speed_sps(config["refine_speed_sps"])
                motor2.turn_degrees(abs_motor_delta_dec, forward=delta_dec > 0)  # Non-blocking
                print(f"[tracking] DEC motor refine command sent (forward={delta_dec > 0})")
            except Exception as e:
                print(f"[tracking] Error refining DEC motor: {e}")
        else:
            print(f"[tracking] DEC already at target (delta={motor_delta_dec:.6f}° motor)")
        
        print("[tracking] Motor movement commands sent (non-blocking)")
    
    except Exception as e:
        print(f"[tracking] Error in motor movement: {e}")


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
        ra_steps_per_rev = int(get_motor_boards()["ra"].get("steps_per_rev", 1600))
        # One output revolution per sidereal day: ra_gear_ratio motor revolutions
        tracking_speed_sps = ra_gear_ratio * ra_steps_per_rev / SIDEREAL_DAY_S
        
        motor1 = ESP32Motor(conn, "motor1")
        motor2 = ESP32Motor(conn, "motor2")
        
        # Stop any existing movement on both motors first
        print(f"[tracking] Stopping all motors before starting continuous tracking")
        try:
            motor1.stop()
            motor2.stop()
        except Exception as e:
            print(f"[tracking] Warning: Error stopping motors: {e}")
        
        time.sleep(0.3)  # Brief pause to ensure motors fully stop
        
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
            
            # Log current coordinates periodically for debugging
            coord_update_interval += 1
            if coord_update_interval >= 5:  # Log every 5 cycles
                coords = get_telescope_coords() or {}
                print(f"[tracking] Periodic status: Current RA={coords.get('right_ascension', 0):.4f}°, Dec={coords.get('declination', 0):.4f}°")
                coord_update_interval = 0
            
            # Update hour angle to reflect Earth's rotation
            update_hour_angle()
            
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
                            
                            # Verify we're actually at the target
                            coords = get_telescope_coords() or {}
                            target_coords = get_target_coords() or {}
                            current_ra = coords.get('right_ascension', 0.0)
                            current_dec = coords.get('declination', 0.0)
                            target_ra = target_coords.get('right_ascension', 0.0)
                            target_dec = target_coords.get('declination', 0.0)
                            
                            # Get location for HA calculation
                            from utils.location import get_current_location
                            from utils.Tools import hour_angle
                            location = get_current_location()
                            
                            if location:
                                longitude = location.get('longitude', 0)
                                current_ha = hour_angle(current_ra, longitude)
                                target_ha = hour_angle(target_ra, longitude)
                                
                                # Calculate shortest path around 360° circle
                                delta_ha = target_ha - current_ha
                                if delta_ha > 180:
                                    delta_ha -= 360
                                elif delta_ha < -180:
                                    delta_ha += 360
                                delta_dec = target_dec - current_dec
                                abs_delta_ha = abs(delta_ha)
                                abs_delta_dec = abs(delta_dec)
                                
                                config = get_slew_config()
                                centered_threshold = config.get("centered_threshold_degrees", 0.01)
                                refine_threshold = config.get("center_threshold_degrees", 0.1)
                                
                                print(f"[tracking] Position check: HA error={delta_ha:.6f}°, Dec error={delta_dec:.6f}°")
                                print(f"[tracking] Thresholds: centered={centered_threshold}°, refine={refine_threshold}°")
                                
                                # If we're centered on target, start sidereal tracking
                                if abs_delta_ha < centered_threshold and abs_delta_dec < centered_threshold:
                                    print(f"[tracking] ✓ Within centered threshold - starting sidereal tracking")
                                    leds.flash_tracking_locked()
                                    leds.set_tracking(True, pulse=False)
                                    leds.set_command_busy(False)
                                    _start_ra_tracking()
                                    sidereal_tracking_active = True
                                # If we're off target but motors stopped, issue correction slew
                                else:
                                    print(f"[tracking] ⚠ Off target - issuing fine correction slew")
                                    print(f"[tracking]   HA correction: {delta_ha:.6f}° ({'EAST/forward' if delta_ha > 0 else 'WEST/backward'})")
                                    print(f"[tracking]   Dec correction: {delta_dec:.6f}° ({'NORTH/forward' if delta_dec > 0 else 'SOUTH/backward'})")
                                    # Reset motor positions to establish new baseline
                                    _reset_motor_positions(motor1, motor2)
                                    # Move by the current error
                                    _move_motors(delta_ha, delta_dec)
                                    waiting_logged = False  # Reset for next wait cycle
                    except Exception as e:
                        print(f"[tracking] Error checking motor status: {e}")
            
            # Sleep to reduce ESP32 load while still getting reasonable update frequency
            time.sleep(1.5)  # Check every 1.5 seconds - balance between responsiveness and ESP32 load
        
        except Exception as e:
            print(f"[tracking] Error in tracking loop: {e}")
            time.sleep(2.0)  # Wait longer after errors
    
    print("[tracking] Continuous tracking thread stopped")


def stop_tracking() -> None:
    # Stop continuous tracking and motors.
    global _tracking_active, _tracking_thread, _target_object
    leds = get_led_manager()
    
    if _tracking_active:
        print("[tracking] Stopping continuous tracking")
        _tracking_active = False
        
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

    leds.set_idle()


def _set_polaris_alignment(location: dict) -> None:
    # Set telescope to Polaris coordinates for polar alignment.
    longitude = location.get('longitude')
    if longitude is None:
        print("[tracking] Warning: Longitude missing; cannot set Polaris alignment.")
        return
    # Store Polaris RA (not HA) in state
    set_telescope_coords(POLARIS_RA_DEG, POLARIS_DEC_DEG, source="polaris")


def trackCoordinates(name, ra, dec, mag):
    # Start tracking a celestial object with continuous sky rotation compensation.
    # 
    # The telescope will:
    # 1. Stop any existing tracking
    # 2. Slew to the target at high speed
    # 3. Refine approach at medium speed
    # 4. Center on target at slow speed
    # 5. Continuously track the object as the sky rotates
    # 
    # Args:
    #     name: Object name
    #     ra: Right ascension in degrees (can be string or float)
    #     dec: Declination in degrees (can be string or float)
    #     mag: Magnitude
    global _tracking_active, _tracking_thread, _target_object
    leds = get_led_manager()
    
    # If already tracking, stop the previous tracking session first
    if _tracking_active:
        print(f"[tracking] ⚠ Already tracking - stopping previous session before acquiring new target")
        stop_tracking()
        time.sleep(0.5)  # Brief pause to ensure tracking fully stops
    
    # Convert string inputs to floats if needed
    try:
        ra = float(ra)
        dec = float(dec)
    except (ValueError, TypeError) as e:
        print(f"[tracking] Error: Invalid RA or Dec format: {e}")
        leds.set_error(True, critical=False)
        return
    
    print(f"[tracking] Acquiring target: {name}, RA: {ra}, Dec: {dec}, Mag: {mag}")
    
    # Get current location from config
    location = get_current_location()
    if location is None:
        print("[tracking] Warning: Location data not available. Cannot calculate hour angle.")
        leds.set_error(True, critical=False)
        return
    
    # Extract longitude and latitude from location JSON
    longitude = location.get('longitude')
    latitude = location.get('latitude')
    print(f"[tracking] Observer location: Longitude={longitude}, Latitude={latitude}")
    leds.clear_error()

    # Convert target RA to hour angle using current location and time
    TargetHA = hour_angle(ra, longitude)
    TargetDec = dec
    print(f"[tracking] Target: {name} - RA={ra:.4f}°, Dec={dec:.4f}°")
    print(f"[tracking] Target Hour Angle (current): HA={TargetHA:.4f}°")
    leds.set_command_busy(True)

    # Read current telescope coordinates from state (stored as RA, not HA)
    coords = get_telescope_coords() or {}
    CurrentRA = coords.get('right_ascension', 0.0)
    CurrentDec = coords.get('declination', 0.0)
    
    # Validate coordinates - if corrupted, assume we're starting fresh at target
    # Valid ranges: RA 0-360°, Dec -90° to +90°
    if CurrentRA < 0 or CurrentRA > 360 or CurrentDec < -90 or CurrentDec > 90:
        print(f"[tracking] WARNING: Corrupted coordinates detected (RA={CurrentRA:.2f}°, Dec={CurrentDec:.2f}°)")
        print(f"[tracking] Resetting current position to target as starting point")
        CurrentRA = ra
        CurrentDec = dec
    
    # Convert current telescope RA to current hour angle
    # This accounts for Earth's rotation since last slew
    CurrentHA = hour_angle(CurrentRA, longitude) if CurrentRA != 0.0 else 0.0
    
    print(f"[tracking] Current telescope: RA={CurrentRA:.4f}°, Dec={CurrentDec:.4f}°")
    print(f"[tracking] Current Hour Angle (recalculated): HA={CurrentHA:.4f}°")

    # Calculate the difference between target and current coordinates
    DeltaHA = TargetHA - CurrentHA
    # Normalize to shortest path around 360° circle (especially important crossing 0°/360°)
    if DeltaHA > 180:
        DeltaHA -= 360
    elif DeltaHA < -180:
        DeltaHA += 360
    DeltaDec = TargetDec - CurrentDec
    print(f"[tracking] Delta to move: HA={DeltaHA:.4f}°, Dec={DeltaDec:.4f}°")
    print(f"[tracking] Absolute delta: HA={abs(DeltaHA):.4f}°, Dec={abs(DeltaDec):.4f}°")

    # Poll ifAligned() until telescope is polar aligned
    print(f"[tracking] Waiting for polar alignment before tracking...")
    while not ifAligned():
        print(f"[tracking] Telescope not aligned. Checking again in 1 second...")
        time.sleep(1)
    print(f"[tracking] Telescope is aligned. Proceeding with tracking.")

    # Slew, refine, and center on target
    if abs(DeltaHA) > 0.001 or abs(DeltaDec) > 0.001:
        print(f"[tracking] Movement required, calling _move_motors...")
        leds.set_movement("slewing")
        
        # Reset motor positions AND set current coordinates to establish clean baseline
        try:
            conn = _get_esp32_connection()
            if conn:
                motor1 = ESP32Motor(conn, "motor1")
                motor2 = ESP32Motor(conn, "motor2")
                _reset_motor_positions(motor1, motor2)
                print("[tracking] Motor positions reset to zero")
                
                # Set current coordinates to CURRENT position (where we are NOW, before slew)
                # NOT to target - motor position tracking will update coords as we move toward target
                # This prevents double-counting the movement
                set_telescope_coords(CurrentRA, CurrentDec, source="tracking")
                print(f"[tracking] Current position baseline: RA={CurrentRA:.4f}°, Dec={CurrentDec:.4f}°")
        except Exception as e:
            print(f"[tracking] Warning: Could not reset motor positions: {e}")
        
        # Move the motors to target (non-blocking)
        _move_motors(DeltaHA, DeltaDec)
        # Motor positions will be updated by the continuous tracking loop as motors move
    else:
        print(f"[tracking] Telescope already at target")

    # Store target for continuous tracking
    _target_object = {"name": name, "ra": ra, "dec": dec, "mag": mag}
    
    # Update target coordinates in state
    set_target_coords(ra, dec, source="tracking")
    
    # Start continuous tracking thread if not already running
    if not _tracking_active:
        _tracking_active = True
        _tracking_thread = threading.Thread(target=_continuous_tracking_loop, daemon=True)
        _tracking_thread.start()
        _start_hour_angle_updater()  # Also start the background hour angle updater
        print(f"[tracking] Continuous tracking started for {name}")

    # Note: ifAligned() check removed here - it was overwriting target coordinates with Polaris
    # Polaris alignment should be done separately via a dedicated alignment command


def ifAligned():
    # placeholder, will link to a momentary button in the future
    # will return true if the telescope is polar aligned, otherwise false
    return True


# Automatically start the hour angle updater when the module is loaded
_start_hour_angle_updater()

