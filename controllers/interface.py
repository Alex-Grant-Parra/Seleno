import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Blueprint, render_template, request, jsonify, session
from astrophysics.V1_Keplarian.convert import convert
from astrophysics.planetary_model import getAllCelestialData
from datetime import datetime
import time

from app.telescopeLink import Telescope, current_telescope  # Updated import

interface_bp = Blueprint("interface", __name__, url_prefix="/interface")


def _require_telescope_control_access():
    from flask_login import current_user
    if getattr(current_user, 'is_limited', False):
        return jsonify({"status": "error", "message": "Limited accounts cannot control telescopes."}), 403
    return None

_CELESTIAL_DEFAULT_VMAGS = {
    "sun": -26.74,
    "moon": -12.70,
    "pluto": 14.0,
}

@interface_bp.route("/")
def interface():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return render_template("errors/401.html"), 401
    return render_template("interface.html")

@interface_bp.route("/update_camera", methods=["POST"])
def update_camera():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to control telescope"}), 401

    guard = _require_telescope_control_access()
    if guard:
        return guard
    
    data = request.json or {}
    response = {"status": "success", "message": "Settings updated"}

    # Determine target telescope
    telescope_id = data.get("telescopeId") or data.get("telescope_id") or (session.get('selected_telescope') or {}).get('telescope_id')
    if not telescope_id:
        return jsonify({"status": "error", "message": "No telescope selected"}), 400

    t = Telescope(telescope_id)

    # Set shutter speed if provided
    shutter_speed = data.get("shutterSpeed")
    iso = data.get("iso")

    print(shutter_speed, iso)
    if shutter_speed:
        try:
            # Set the shutter speed using Camera class
            print("Changing shutterspeed")
            t.camera.set_settings(["/main/capturesettings/shutterspeed", shutter_speed])
        except Exception as e:
            response = {"status": "error", "message": f"Failed to set shutter speed: {e}"}
            print(response)
            return jsonify(response)

    # Handling ISO
    if iso:
        try:
            print("Changing iso")
            t.camera.set_settings(["/main/imgsettings/iso", iso])
        except Exception as e:
            response = {"status": "error", "message": f"Failed to set ISO: {e}"}
            print(response)
            return jsonify(response)

    return jsonify(response)

def extract_friendly_common_name(common_names_field: str) -> str:
    # Extract the first friendly name from commonNames field; skip catalog designations
    if not common_names_field:
        return ''
    parts = [p.strip() for p in common_names_field.split(',')]
    for name in parts:
        name_upper = name.upper()
        # Skip catalog designations (HD, NGC, IC, M followed by number)
        if (name_upper.startswith('HD') or 
            name_upper.startswith('NGC') or 
            name_upper.startswith('IC') or 
            (name_upper.startswith('M') and len(name) > 1 and name[1:].strip().replace(' ', '').isdigit())):
            continue
        # Found a friendly name
        return name
    return ''

@interface_bp.route("/search_object", methods=["POST"])
def search_object():
    from models.tables import HDSTARtable, IndexTable, NGCtable
    data = request.json
    search_value = data.get("searchValue", "").strip()

    print(f"Received search query: {search_value}")

    result = None

    searchableCelestials = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"]
    try:
        # Normalize search for robust matching
        raw = search_value
        norm = raw.strip()
        norm_upper = norm.upper()
        # Determine the table based on prefix or content
        if norm_upper.startswith("HD"):
            # Accept variations like hd48915 or hd 48915
            print(f"Querying HDSTARtable (flex) for {norm}")
            result = HDSTARtable.query_by_name_flexible(norm)
            if not result:
                # As a fallback, try exact
                result = HDSTARtable.query_by_name(norm)

        elif norm_upper.startswith("NGC"):
            print(f"Querying NGCtable for {search_value}")
            result = NGCtable.query_by_name(search_value)

        elif norm_upper.startswith("IC"):
            print(f"Querying IndexTable for {search_value}")
            result = IndexTable.query_by_name(search_value)

        elif norm_upper.startswith("M") and len(norm_upper) > 1 and norm_upper[1:].isdigit():
            # Handle Messier objects (e.g., "M1", "m42", "M104")
            messier_designation = norm_upper
            print(f"Querying NGCtable for Messier object {messier_designation}")
            result = NGCtable.query_by_messier(messier_designation)

        elif norm.lower() in searchableCelestials:
            search_value = norm.lower()
            now = datetime.utcnow()
            CelestialData = getAllCelestialData(now.year, now.month, now.day, now.hour, now.minute, now.second)

            if search_value in CelestialData:
                formattedData = format_celestial_data(search_value, CelestialData[search_value])
                print(formattedData)
                result = formattedData
            else:
                print("Celestial object not found.")

        else:
            print(f"Searching by common name across stars and NGC: {norm}")
            # Exact alias match first (so "Deneb" doesn't land on Denebola),
            # then the substring search below
            from app import star_catalog
            designation = star_catalog.find_by_proper_name(norm)
            if designation:
                result = HDSTARtable.query_by_name(designation)

            if not result:
                result = HDSTARtable.query_by_common_name(norm)
            if not result:
                # Then try NGC common names (exact ilike on full cell)
                result = NGCtable.query_by_common_name(norm)
            
            if not result:
                print("Object not found by common name")
                return jsonify({"status": "error", "message": "Object not found"})

    except ValueError as e:
        print(f"Error during search: {e}")
        return jsonify({"status": "error", "message": "Invalid search format"})

    if result:
        # Check if result is a dictionary
        if isinstance(result, dict):
            result_data = result
        else:
            result_data = {column: getattr(result, column) for column in result.__table__.columns.keys()}

        name = result_data.get('Name', "Null")
        ra = float(result_data.get('RA', 0))  # Default to 0 if RA is missing or None
        dec = float(result_data.get('DEC', 0))  # Default to 0 if DEC is missing or None
        mag = result_data.get('V-Mag', 0)  # Default to 0 if V-Mag is missing or None

        # Bayer letter, variable-star ID and displayed name from the catalogue
        try:
            from app import star_catalog
            record = star_catalog.star_name_record(name) or {}
        except Exception:
            record = {}

        if record.get('bayer'):
            result_data['bayer'] = record['bayer']
        if record.get('var'):
            result_data['variableId'] = record['var']

        # Extract friendly common name (non-HD variant) if available
        friendly_name = record.get('name')
        if not friendly_name:
            common_names_raw = result_data.get('commonNames', '') or result_data.get('Common names', '')
            friendly_name = extract_friendly_common_name(common_names_raw)
        if friendly_name:
            result_data['friendlyName'] = friendly_name


        # session["selectedObject"] = { # Adds to flask's session
        #     "name": name,
        #     "ra": ra,
        #     "dec": dec,
        #     "mag": mag
        # }
        return jsonify({"status": "success", "data": result_data})
    else:
        print("Object not found")
        return jsonify({"status": "error", "message": "Object not found"})
    


def format_celestial_data(name, data):
    ra_hours = convert.HrMinSecToDegrees(data['ra'][0], data['ra'][1], data['ra'][2])
    ra_degrees = ra_hours * 15  # Convert hours to degrees (360/24h = 15/h)
    
    dec_degrees = convert.HrMinSecToDegrees(data['dec'][0], data['dec'][1], data['dec'][2])
    vmag = data.get("vmag")
    if vmag is None:
        vmag = _CELESTIAL_DEFAULT_VMAGS.get(name.lower(), 30)
    
    formatted = {
        "Name": name.capitalize(),
        "RA": ra_degrees,
        "DEC": dec_degrees,
        "V-Mag": vmag
    }

    if data.get("phase_name"):
        formatted["phase_name"] = data.get("phase_name")
    phase_angle = data.get("phase_angle_deg")
    if phase_angle is not None:
        try:
            formatted["phase_angle_deg"] = float(phase_angle)
        except Exception:
            pass

    moon_illum = data.get("moon_illumination_fraction")
    if moon_illum is not None:
        try:
            formatted["moon_illumination_fraction"] = float(moon_illum)
        except Exception:
            pass

    moon_elong = data.get("moon_elongation_deg")
    if moon_elong is not None:
        try:
            formatted["moon_elongation_deg"] = float(moon_elong)
        except Exception:
            pass

    return formatted

@interface_bp.route("/get_camera_choices")
def get_camera_choices():
    try:
        selected = session.get('selected_telescope') or {}
        cid = selected.get('telescope_id')
        if not cid:
            return jsonify({"status": "error", "message": "No telescope selected"}), 400
        t = Telescope(cid)
        choices = t.camera.get_settings()
        return jsonify(choices)
    except Exception as e:
        print(e)
        return jsonify({"status": "error", "message": str(e)})
    

@interface_bp.route("/take_photo", methods=["POST"])
def take_photo():
    try:
        from flask_login import current_user
        if not current_user.is_authenticated:
            return jsonify({"status": "error", "message": "Must be logged in to take photos"}), 401

        guard = _require_telescope_control_access()
        if guard:
            return guard

        current_id = current_user.get_id()
        telescope_id = (request.json or {}).get("telescopeId") or (request.json or {}).get("telescope_id") or (session.get('selected_telescope') or {}).get('telescope_id')
        if not telescope_id:
            return jsonify({"status": "error", "message": "No telescope selected"}), 400
        t = Telescope(telescope_id)
        print(t.camera.capture_photo(current_id))
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})
    

@interface_bp.route("/get_telescopes", methods=["GET"])
def get_telescopes():
    # Get all available telescopes from the database
    try:
        from models.tables import Telescope
        telescopes = Telescope.get_all_telescopes()
        
        telescope_list = []
        for telescope in telescopes:
            telescope_data = {
                'id': telescope.id,
                'telescope_id': telescope.telescope_id,
                'ip_address': telescope.ip_address,
                'type': telescope.type,
                'last_seen': telescope.last_seen,
                'online': Telescope.is_telescope_online(telescope.telescope_id)
            }
            telescope_list.append(telescope_data)
        
        return jsonify({"status": "success", "telescopes": telescope_list})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to get telescopes: {str(e)}"})

@interface_bp.route("/select_telescope", methods=["POST"])
def select_telescope():
    # Set the selected telescope in the user's session
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to select telescope"}), 401
    
    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")
        
        if not telescope_id:
            return jsonify({"status": "error", "message": "Telescope ID is required"})
        
        from models.tables import Telescope
        telescope = Telescope.get_telescope_by_id(telescope_id)
        
        if not telescope:
            return jsonify({"status": "error", "message": "Telescope not found"})
        
        # Store selected telescope in session
        session['selected_telescope'] = {
            'telescope_id': telescope.get('telescope_id'),
            'ip_address': telescope.get('ip_address'),
            'type': telescope.get('type'),
            'last_seen': telescope.get('last_seen'),
            'online': Telescope.is_telescope_online(telescope.get('telescope_id', ''))
        }
        
        return jsonify({
            "status": "success", 
            "message": f"Selected telescope: {telescope_id}",
            "telescope": session['selected_telescope']
        })
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to select telescope: {str(e)}"})

@interface_bp.route("/get_selected_telescope", methods=["GET"])
def get_selected_telescope():
    # Get the currently selected telescope from session
    selected_telescope = session.get('selected_telescope')
    if selected_telescope:
        return jsonify({"status": "success", "telescope": selected_telescope})
    else:
        return jsonify({"status": "success", "telescope": None, "message": "No telescope selected"})

@interface_bp.route("/add_telescope", methods=["POST"])
def add_telescope():
    # Add a new telescope to the database
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to manage telescopes"}), 401
    
    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")
        ip_address = data.get("ipAddress") or data.get("ip_address")
        telescope_type = data.get("type") or data.get("telescope_type")
        
        # Validate required fields
        if not telescope_id:
            return jsonify({
                "status": "error", 
                "message": "Telescope ID is required"
            })
        
        from models.tables import Telescope
        result = Telescope.add_telescope(telescope_id, ip_address, telescope_type)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to add telescope: {str(e)}"})

@interface_bp.route("/remove_telescope", methods=["POST"])
def remove_telescope():
    # Remove a telescope from the database
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to manage telescopes"}), 401
    
    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")
        
        if not telescope_id:
            return jsonify({"status": "error", "message": "Telescope ID is required"})
        
        from models.tables import Telescope
        result = Telescope.remove_telescope(telescope_id)
        
        selected = session.get('selected_telescope', {})
        if result.get("status") == "success" and selected.get('telescope_id') == telescope_id:
            session.pop('selected_telescope', None)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to remove telescope: {str(e)}"})

@interface_bp.route("/update_telescope_heartbeat", methods=["POST"])
def update_telescope_heartbeat():
    # Update the last seen timestamp for a telescope (heartbeat)
    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")
        
        if not telescope_id:
            return jsonify({"status": "error", "message": "Telescope ID is required"})
        
        from models.tables import Telescope
        result = Telescope.update_last_seen(telescope_id)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to update telescope heartbeat: {str(e)}"})

@interface_bp.route("/start_live_view", methods=["POST"])
def start_live_view():
    # Start live view on the telescope
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to control telescope"}), 401
    
    print("Started live view")
    try:
        telescope_id = (request.json or {}).get("telescopeId") or (request.json or {}).get("telescope_id") or (session.get('selected_telescope') or {}).get('telescope_id')
        if not telescope_id:
            return jsonify({"status": "error", "message": "No telescope selected"}), 400
        t = Telescope(telescope_id)
        result = t.camera.start_live_view()
        return jsonify({"status": "success", "message": "Live view started", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to start live view: {str(e)}"})

@interface_bp.route("/stop_live_view", methods=["POST"])
def stop_live_view():
    # Stop live view on the telescope
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to control telescope"}), 401
    
    print("Stopped live view")
    try:
        telescope_id = (request.json or {}).get("telescopeId") or (request.json or {}).get("telescope_id") or (session.get('selected_telescope') or {}).get('telescope_id')
        if not telescope_id:
            return jsonify({"status": "error", "message": "No telescope selected"}), 400
        t = Telescope(telescope_id)
        result = t.camera.stop_live_view()
        return jsonify({"status": "success", "message": "Live view stopped", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to stop live view: {str(e)}"})

@interface_bp.route("/motor_command", methods=["POST"])
def motor_command():
    # Execute motor commands on the telescope
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Must be logged in to control telescope"}), 401
    
    try:
        data = request.json or {}
        telescope_id = data.get("telescope_id") or data.get("telescopeId") or (session.get('selected_telescope') or {}).get('telescope_id')
        command = data.get("command")
        args = data.get("args")
        motor_id = data.get("motor_id", "motor1") 
        
        if not telescope_id:
            return jsonify({"status": "error", "message": "No telescope selected"}), 400
        
        if not command:
            return jsonify({"status": "error", "message": "No command specified"}), 400
        
        # Create telescope instance
        t = Telescope(telescope_id)
        
        # Map command to motor controller method
        motor_commands = {
            "enable": t.motor.enable,
            "set_direction": t.motor.set_direction,
            "set_speed": t.motor.set_speed,
            "start": t.motor.start,
            "move_steps": t.motor.move_steps,
            "stop": t.motor.stop,
            "set_microsteps": t.motor.set_microsteps,
            "set_current": t.motor.set_current,
            "set_mode": t.motor.set_mode,
            "set_accel": t.motor.set_accel,
            "status": t.motor.status,
            "status_all": t.motor.status_all
        }
        
        if command not in motor_commands:
            return jsonify({"status": "error", "message": f"Unknown motor command: {command}"}), 400
        
        # Execute the command
        method = motor_commands[command]
        
        # Handle status_all separately (no motor_id parameter)
        if command == "status_all":
            result = method()
        elif args is not None:
            # Add motor_id to the call
            if isinstance(args, list):
                result = method(*args, motor_id=motor_id)
            else:
                result = method(args, motor_id=motor_id)
        else:
            result = method(motor_id=motor_id)
        
        print(f"Motor command '{command}' executed on motor '{motor_id}' with args {args}, result: {result}")
        return jsonify({"status": "success", "message": f"Motor command '{command}' executed successfully", "result": result})
        
    except Exception as e:
        error_msg = f"Failed to execute motor command: {str(e)}"
        print(error_msg)
        return jsonify({"status": "error", "message": error_msg}), 500

@interface_bp.route("/get_motors", methods=["POST"])
def get_motors():
    # Get list of available motors for the selected telescope
    try:
        data = request.json or {}
        telescope_id = data.get("telescope_id") or data.get("telescopeId") or (session.get('selected_telescope') or {}).get('telescope_id')
        
        if not telescope_id:
            return jsonify({"status": "error", "message": "No telescope selected"}), 400
        
        # Create telescope instance
        t = Telescope(telescope_id)
        
        # Get status of all motors
        result = t.motor.status_all()
        
        # Extract motor IDs from the result
        if isinstance(result, dict) and "error" not in result:
            motors = list(result.keys()) if result else ["motor1"]
            return jsonify({"status": "success", "motors": motors})
        else:
            # Fallback to default motor
            return jsonify({"status": "success", "motors": ["motor1"]})
        
    except Exception as e:
        print(f"Failed to get motors: {str(e)}")
        return jsonify({"status": "success", "motors": ["motor1"]})