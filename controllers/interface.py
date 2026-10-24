import sys
import os
import logging
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from flask import Blueprint, render_template, request, jsonify, session
from astrophysics.V1_Keplarian.convert import convert
from astrophysics.planetary_model import getAllCelestialData
from datetime import datetime, timezone
import time

from app.telescopeLink import Telescope, current_telescope, result_error, JOB_NAMES  # Updated import
from app.telescope_access import (
    CONTROL,
    OWNER,
    get_telescope_row,
    requested_telescope_id,
    telescope_access_error,
    user_can_control,
)
from models import star_catalog
from flask_babel import gettext as _
from app.i18n import request_language

logger = logging.getLogger(__name__)

interface_bp = Blueprint("interface", __name__, url_prefix="/interface")


def _require_telescope_control_access(telescope_id=None, level=CONTROL):
    # Owner, approved booking covering now, or admin; None when allowed
    return telescope_access_error(telescope_id or requested_telescope_id(), level)


def _command_reply(result, success_message=None, **extra):
    # A command result as a reply: the telescope's refusal is a failure, not a success
    error = result_error(result)
    if error:
        return jsonify({"status": "error", "message": error, "result": result})
    reply = {"status": "success", "result": result, **extra}
    if success_message:
        reply["message"] = success_message
    return jsonify(reply)


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


# Camera settings the page can change, and the camera config path each one sets
CAMERA_SETTING_LABELS = {
    "shutterSpeed": "/main/capturesettings/shutterspeed",
    "iso": "/main/imgsettings/iso",
    "aperture": "/main/capturesettings/aperture",
    "whiteBalance": "/main/imgsettings/whitebalance",
    "imageFormat": "/main/imgsettings/imageformat",
    "exposureCompensation": "/main/capturesettings/exposurecompensation",
}

@interface_bp.route("/update_camera", methods=["POST"])
def update_camera():
    data = request.json or {}

    # Determine target telescope
    telescope_id = requested_telescope_id(data)
    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    t = Telescope(telescope_id)
    if "photoFormat" in data and "imageFormat" not in data:
        data["imageFormat"] = data["photoFormat"]

    changed, errors = [], []
    for field, label in CAMERA_SETTING_LABELS.items():
        value = data.get(field)
        if value in (None, ""):
            continue
        result = t.camera.set_settings([label, value])
        error = result_error(result)
        if error:
            errors.append(f"{field}: {error}")
        else:
            changed.append(field)

    if errors:
        return jsonify({"status": "error", "message": "; ".join(errors), "changed": changed})
    if not changed:
        return jsonify({"status": "error", "message": _("No camera setting given")}), 400
    return jsonify({"status": "success", "message": _("Settings updated"), "changed": changed})

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

def _catalog_search_result(entry):
    """A star_catalog.lookup() entry keyed like the catalogue tables' columns,
    which is what the search result's detail table has always shown."""
    name, record = entry["name"], entry["record"]
    result = {"Name": name, "RA": entry["ra"], "DEC": entry["dec"], "V-Mag": entry["mag"]}
    names_cell = ", ".join(record.get("aliases", ())) or None
    if name.upper().startswith("HD"):
        result["commonNames"] = names_cell
    else:
        result["Common names"] = names_cell
        if record.get("messier"):
            result["Messier"] = record["messier"]
    return result


# Alternatives listed under a search result, and how far ahead (in score) a
# close spelling must be to count as a clear winner: one word fewer left out
_SEARCH_ALTERNATIVES = 4
_CLEAR_LEAD = star_catalog.FUZZY_EXTRA_WORD


def _search_match(match):
    """A star_catalog.search() match as the client lists it."""
    return {key: match[key] for key in ("kind", "key", "name", "matched", "match")}


def _search_object_data(kind, key, lang):
    """The search result's detail data for one object, or None."""
    if kind == "body":
        now = datetime.utcnow()
        celestial = getAllCelestialData(now.year, now.month, now.day, now.hour, now.minute, now.second)
        if key not in celestial:
            return None
        data = format_celestial_data(key, celestial[key])
        data["label"] = star_catalog.object_label("body", key, lang)
        return data
    if kind == "constellation":
        position = star_catalog.constellation_position(key)
        if position is None:
            return None
        name = star_catalog.object_label("constellation", key, lang)
        return {"Name": name, "RA": position[0], "DEC": position[1], "V-Mag": None,
                "type": "constellation", "constellation": key, "label": name}
    entry = star_catalog.lookup(key)
    return _catalog_search_result(entry) if entry else None


@interface_bp.route("/search_object", methods=["POST"])
def search_object():
    """Find an object by name (in any language, forgiving small typos), by
    catalogue number (exact only), or by {"pick": {kind, key}} from a list.

    A clear best match comes back as "success" with its data, how the query
    matched it, and a few other matches. When several objects match about
    equally well, it comes back as "choose" with the list instead."""
    data = request.json or {}
    query = str(data.get("searchValue", "")).strip()[:120]
    lang = request_language()
    pick = data.get("pick") if isinstance(data.get("pick"), dict) else None

    if pick and pick.get("kind") in ("catalog", "body", "constellation") and pick.get("key"):
        kind, key = pick["kind"], str(pick["key"])
        best, alternatives = None, []
    else:
        matches = star_catalog.search(query, lang, limit=_SEARCH_ALTERNATIVES + 1)
        if not matches:
            return jsonify({"status": "error", "message": _("Object not found")})
        best, rest = matches[0], matches[1:]
        clear = (best["match"] != "fuzzy" or not rest
                 or best["score"] - rest[0]["score"] >= _CLEAR_LEAD)
        if not clear:
            return jsonify({"status": "choose", "query": query,
                            "matches": [_search_match(m) for m in matches]})
        # A close spelling only lists other close spellings; a proper match
        # doesn't drag in near-misses ("Mizar" doesn't offer "Izar")
        alternatives = [_search_match(m) for m in rest
                        if best["match"] == "fuzzy" or m["match"] != "fuzzy"]
        kind, key = best["kind"], best["key"]

    result_data = _search_object_data(kind, key, lang)
    if result_data is None:
        return jsonify({"status": "error", "message": _("Object not found")})

    name = result_data.get("Name", "Null")
    record = star_catalog.star_name_record(name) if kind == "catalog" else None
    record = record or {}
    if record.get("bayer"):
        result_data["bayer"] = record["bayer"]
    if record.get("var"):
        result_data["variableId"] = record["var"]

    # Displayed name next to the designation: in the visitor's language, else
    # the first proper name in the catalogue's names column
    if kind == "catalog":
        friendly_name = star_catalog.display_name(name, lang) or extract_friendly_common_name(
            result_data.get("commonNames", "") or result_data.get("Common names", ""))
        if friendly_name:
            result_data["friendlyName"] = friendly_name

    result_data["kind"] = kind
    if best:
        result_data["match"] = {"query": query, "matched": best["matched"], "match": best["match"]}
    result_data["alternatives"] = alternatives
    return jsonify({"status": "success", "data": result_data})


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
        cid = requested_telescope_id({})
        guard = _require_telescope_control_access(cid)
        if guard:
            return guard
        t = Telescope(cid)
        choices = t.camera.get_settings()
        error = result_error(choices)
        if error:
            return jsonify({"status": "error", "message": error})
        return jsonify(choices)
    except Exception:
        logger.exception("Failed to get camera choices")
        return jsonify({"status": "error", "message": _("Failed to get camera choices")})


@interface_bp.route("/take_photo", methods=["POST"])
def take_photo():
    try:
        from flask_login import current_user
        data = request.json or {}
        telescope_id = requested_telescope_id(data)
        guard = _require_telescope_control_access(telescope_id)
        if guard:
            return guard

        exposure_s = data.get("exposure_s")
        if exposure_s not in (None, ""):
            try:
                exposure_s = float(exposure_s)
            except (TypeError, ValueError):
                return jsonify({"status": "error", "message": "exposure_s must be a number"}), 400
            if not 0 < exposure_s <= 900:
                return jsonify({"status": "error", "message": _("Bulb exposures run from 0 to 900 s")}), 400
        else:
            exposure_s = None

        frame_type = data.get("frame_type")
        if frame_type not in (None, "", "test", "light", "snapshot"):
            return jsonify({"status": "error", "message": "frame_type must be test, light or snapshot"}), 400

        current_id = current_user.get_id()
        t = Telescope(telescope_id)
        result = t.camera.capture_photo(current_id, exposure_s=exposure_s, shutter=data.get("shutter"),
                                        frame_type=frame_type or None)
        return _command_reply(result, "Photo taken")
    except Exception:
        logger.exception("Failed to take photo")
        return jsonify({"status": "error", "message": _("Failed to take photo")})


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
    except Exception:
        logger.exception("Failed to get telescopes")
        return jsonify({"status": "error", "message": _("Failed to get telescopes")})


def _access_flags(telescope_id):
    from flask_login import current_user
    row = get_telescope_row(telescope_id)
    return {
        "control": user_can_control(current_user, row, CONTROL),
        "owner": user_can_control(current_user, row, OWNER),
    }

@interface_bp.route("/select_telescope", methods=["POST"])
def select_telescope():
    # Set the selected telescope in the user's session
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in to select telescope")}), 401

    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")

        if not telescope_id:
            return jsonify({"status": "error", "message": _("Telescope ID is required")})

        from models.tables import Telescope
        telescope = Telescope.get_telescope_by_id(telescope_id)

        if not telescope:
            return jsonify({"status": "error", "message": _("Telescope not found")})

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
            "message": _('Selected telescope: %(telescope_id)s', telescope_id=telescope_id),
            "telescope": session['selected_telescope'],
            "access": _access_flags(telescope_id),
        })

    except Exception:
        logger.exception("Failed to select telescope")
        return jsonify({"status": "error", "message": _("Failed to select telescope")})

@interface_bp.route("/get_selected_telescope", methods=["GET"])
def get_selected_telescope():
    # Get the currently selected telescope from session
    selected_telescope = session.get('selected_telescope')
    if selected_telescope:
        return jsonify({
            "status": "success",
            "telescope": selected_telescope,
            "access": _access_flags(selected_telescope.get('telescope_id')),
        })
    else:
        return jsonify({"status": "success", "telescope": None, "message": _("No telescope selected")})

@interface_bp.route("/add_telescope", methods=["POST"])
def add_telescope():
    # Add a new telescope to the database (admins; owners register through /telescopes)
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in to manage telescopes")}), 401
    if not current_user.is_admin:
        return jsonify({"status": "error", "message": _("Admin access required")}), 403

    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")
        ip_address = data.get("ipAddress") or data.get("ip_address")
        telescope_type = data.get("type") or data.get("telescope_type")

        # Validate required fields
        if not telescope_id:
            return jsonify({
                "status": "error",
                "message": _("Telescope ID is required")
            })

        from models.tables import Telescope
        result = Telescope.add_telescope(telescope_id, ip_address, telescope_type)

        return jsonify(result)

    except Exception:
        logger.exception("Failed to add telescope")
        return jsonify({"status": "error", "message": _("Failed to add telescope")})

@interface_bp.route("/remove_telescope", methods=["POST"])
def remove_telescope():
    # Remove a telescope from the database
    try:
        data = request.json or {}
        telescope_id = data.get("telescopeId") or data.get("telescope_id")

        if not telescope_id:
            return jsonify({"status": "error", "message": _("Telescope ID is required")})

        guard = _require_telescope_control_access(telescope_id, OWNER)
        if guard:
            return guard

        from models.tables import Telescope
        result = Telescope.remove_telescope(telescope_id)

        selected = session.get('selected_telescope', {})
        if result.get("status") == "success" and selected.get('telescope_id') == telescope_id:
            session.pop('selected_telescope', None)

        return jsonify(result)

    except Exception:
        logger.exception("Failed to remove telescope")
        return jsonify({"status": "error", "message": _("Failed to remove telescope")})

@interface_bp.route("/update_telescope_heartbeat", methods=["POST"])
def update_telescope_heartbeat():
    # Update the last seen timestamp for a telescope (heartbeat); telescopes do this
    # themselves over their sockets, so only admins may set it by hand
    from flask_login import current_user
    if not current_user.is_authenticated or not current_user.is_admin:
        return jsonify({"status": "error", "message": _("Admin access required")}), 403
    try:
        data = request.json
        telescope_id = data.get("telescopeId") or data.get("telescope_id")

        if not telescope_id:
            return jsonify({"status": "error", "message": _("Telescope ID is required")})

        from models.tables import Telescope
        result = Telescope.update_last_seen(telescope_id)

        return jsonify(result)

    except Exception:
        logger.exception("Failed to update telescope heartbeat")
        return jsonify({"status": "error", "message": "Failed to update telescope heartbeat"})

@interface_bp.route("/start_live_view", methods=["POST"])
def start_live_view():
    # Start live view on the telescope
    telescope_id = requested_telescope_id(request.json or {})
    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    print("Started live view")
    try:
        t = Telescope(telescope_id)
        result = t.camera.start_live_view()
        return _command_reply(result, "Live view started")
    except Exception:
        logger.exception("Failed to start live view")
        return jsonify({"status": "error", "message": _("Failed to start live view")})

@interface_bp.route("/stop_live_view", methods=["POST"])
def stop_live_view():
    # Stop live view on the telescope
    telescope_id = requested_telescope_id(request.json or {})
    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    print("Stopped live view")
    try:
        t = Telescope(telescope_id)
        result = t.camera.stop_live_view()
        return _command_reply(result, "Live view stopped")
    except Exception:
        logger.exception("Failed to stop live view")
        return jsonify({"status": "error", "message": _("Failed to stop live view")})


@interface_bp.route("/liveview_ticket", methods=["GET"])
def liveview_ticket():
    # A one-minute ticket to watch a telescope over the live-view socket
    from flask_login import current_user
    from app.liveview import make_watch_ticket, watch_url

    telescope_id = requested_telescope_id({})
    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard
    ticket = make_watch_ticket(telescope_id, current_user.id)
    resp = jsonify({"status": "success", "url": watch_url(telescope_id, ticket)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


@interface_bp.route("/telescope_status", methods=["GET"])
def telescope_status():
    # The latest status message the telescope pushed, its age, and its capture plan
    from app.WebsocketServer import get_latest_status
    from app.liveview import viewer_counts
    from models.capture_plan import ACTIVE_PLAN_STATES, CapturePlan

    telescope_id = requested_telescope_id({})
    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    age_s, status = get_latest_status(telescope_id)
    row = get_telescope_row(telescope_id)
    plan = (
        CapturePlan.query.filter(CapturePlan.telescope_id == row.id, CapturePlan.state.in_(ACTIVE_PLAN_STATES))
        .order_by(CapturePlan.created_at.desc())
        .first()
    )
    return jsonify({
        "status": "success",
        "telescope_id": telescope_id,
        "age_s": round(age_s, 1) if age_s is not None else None,
        "telescope_status": status,
        "viewers": viewer_counts.count(telescope_id),
        "plan": plan.to_dict() if plan else None,
        "access": _access_flags(telescope_id),
        **_booking_window(row),
    })


def _booking_window(row):
    # The user's booking that covers now, and when the next booking starts: how long
    # a capture can run
    from flask_login import current_user
    from app.telescope_access import approved_booking, next_booking_start

    iso = lambda value: value.isoformat() + "Z" if value else None
    booking = approved_booking(row, user_id=current_user.id)
    return {
        "booking": {"start": iso(booking.start_utc), "end": iso(booking.end_utc)} if booking else None,
        "next_booking_start": iso(next_booking_start(row)),
    }


class _BadParam(ValueError):
    pass


def _param(params, name, kind=str, required=False, choices=None, minimum=None, maximum=None):
    # One parameter of a /command request, checked and converted
    value = params.get(name)
    if value is None or value == "":
        if required:
            raise _BadParam(f"{name} is required")
        return None
    try:
        if kind is bool:
            value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
        elif kind is float:
            value = float(value)
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError
        elif kind is int:
            value = int(value)
        else:
            value = str(value).strip()[:200]
    except (TypeError, ValueError):
        raise _BadParam(f"{name} must be a {'number' if kind in (int, float) else kind.__name__}")
    if choices is not None and value not in choices:
        raise _BadParam(f"{name} must be one of {', '.join(map(str, choices))}")
    if minimum is not None and value < minimum:
        raise _BadParam(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise _BadParam(f"{name} must be at most {maximum}")
    return value


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


TRACKING_RATES = ("sidereal", "lunar", "solar", "king", "custom")
LIVEVIEW_CODECS = ("auto", "h264", "jpeg")
RECORDING_KINDS = ("video", "timelapse")

# action -> (access level, plain, call). Plain actions are status reads: their result is
# data even when it has an `error` field, and only a bare {"error": ...} is a failure.
TELESCOPE_ACTIONS = {
    # Scope
    "park": (CONTROL, False, lambda t, p, uid: t.scope.park()),
    "unpark": (CONTROL, False, lambda t, p, uid: t.scope.unpark()),
    "set_park_here": (CONTROL, False, lambda t, p, uid: t.scope.set_park_here()),
    "track_object": (CONTROL, False, lambda t, p, uid: t.scope.track_object(_param(p, "name", required=True))),
    "get_planets": (CONTROL, True, lambda t, p, uid: t.scope.get_planets()),
    "set_tracking_rate": (CONTROL, False, lambda t, p, uid: t.scope.set_tracking_rate(
        _param(p, "rate", required=True, choices=TRACKING_RATES),
        _param(p, "custom_arcsec_per_s", float, minimum=-3600, maximum=3600))),
    "estop": (CONTROL, False, lambda t, p, uid: t.scope.estop()),
    "clear_estop": (OWNER, False, lambda t, p, uid: t.scope.clear_estop()),
    "estop_status": (CONTROL, True, lambda t, p, uid: t.scope.estop_status()),
    "solve_and_sync": (CONTROL, False, lambda t, p, uid: t.scope.solve_and_sync(_param(p, "source"))),
    "centre_target": (CONTROL, False, lambda t, p, uid: t.scope.centre_target(_param(p, "source"))),
    "polar_align": (CONTROL, False, lambda t, p, uid: t.scope.polar_align(
        _param(p, "step_deg", float, minimum=1, maximum=90), _param(p, "source"))),
    "job_status": (CONTROL, True, lambda t, p, uid: t.scope.job_status(
        _param(p, "name", required=True, choices=tuple(JOB_NAMES.values())))),
    "get_alignment": (CONTROL, True, lambda t, p, uid: t.scope.get_alignment()),
    "clear_alignment": (CONTROL, False, lambda t, p, uid: t.scope.clear_alignment()),
    "mount_status": (CONTROL, True, lambda t, p, uid: t.scope.mount_status()),
    "dither": (CONTROL, False, lambda t, p, uid: t.scope.dither(_param(p, "arcmin", float, minimum=0, maximum=60))),

    # Live view
    "set_liveview_quality": (CONTROL, False, lambda t, p, uid: t.liveview.set_quality(_param(p, "quality", required=True))),
    "set_liveview_focus": (CONTROL, False, lambda t, p, uid: t.liveview.set_focus(_param(p, "on", bool, required=True))),
    "set_camera_zoom": (CONTROL, False, lambda t, p, uid: t.liveview.set_zoom(
        _param(p, "level", float, required=True, minimum=1, maximum=20),
        _param(p, "x", float, minimum=0, maximum=1), _param(p, "y", float, minimum=0, maximum=1))),
    "set_liveview_options": (CONTROL, False, lambda t, p, uid: t.liveview.set_options(
        codec=_param(p, "codec", choices=LIVEVIEW_CODECS),
        bitrate_kbps=_param(p, "bitrate_kbps", int, minimum=100, maximum=20000),
        gop_s=_param(p, "gop_s", float, minimum=0.5, maximum=10),
        jpeg_quality=_param(p, "jpeg_quality", int, minimum=50, maximum=98),
        max_fps=_param(p, "max_fps", float, minimum=0.2, maximum=30))),
    "liveview_snapshot": (CONTROL, False, lambda t, p, uid: t.liveview.snapshot(uid)),
    "start_recording": (CONTROL, False, lambda t, p, uid: t.liveview.start_recording(
        _param(p, "kind", required=True, choices=RECORDING_KINDS), _param(p, "interval_s", float, minimum=0.1, maximum=3600))),
    "stop_recording": (CONTROL, False, lambda t, p, uid: t.liveview.stop_recording()),
    "recording_status": (CONTROL, True, lambda t, p, uid: t.liveview.recording_status()),
    "liveview_stats": (CONTROL, True, lambda t, p, uid: t.liveview.stats()),

    # Camera
    "camera_status": (CONTROL, True, lambda t, p, uid: t.camera.get_status()),

    # Uploads
    "upload_queue": (CONTROL, True, lambda t, p, uid: t.uploads.queue()),
    "upload_pending": (CONTROL, False, lambda t, p, uid: t.uploads.upload_pending(_param(p, "kind", choices=("raw", "jpeg", "video", "all")) or "raw")),
    "retry_failed_uploads": (CONTROL, False, lambda t, p, uid: t.uploads.retry_failed()),
    "upload_offline_photos": (CONTROL, False, lambda t, p, uid: t.uploads.upload_offline(uid)),

    # Device: the telescope's owner or an admin, whatever the Pi allows
    "client_status": (OWNER, True, lambda t, p, uid: t.device.client_status()),
    "set_clock": (OWNER, False, lambda t, p, uid: t.device.set_clock(_param(p, "utc") or _utc_now_iso())),
    "set_horizon": (OWNER, False, lambda t, p, uid: t.device.set_horizon(
        _param(p, "min_alt_deg", float, minimum=-10, maximum=90), _param(p, "profile"), _param(p, "stop_when_set", bool))),
    "esp_info": (OWNER, True, lambda t, p, uid: t.device.esp_info()),
    "esp_driver_status": (OWNER, True, lambda t, p, uid: t.device.esp_driver_status()),
    "update_client": (OWNER, False, lambda t, p, uid: t.device.update_client()),
    "update_firmware": (OWNER, False, lambda t, p, uid: t.device.update_firmware()),
}

# Actions that start a background job, and the getJobStatus name to poll
JOB_ACTIONS = {
    "park": JOB_NAMES["park"],
    "dither": JOB_NAMES["dither"],
    "solve_and_sync": JOB_NAMES["solveAndSync"],
    "centre_target": JOB_NAMES["centreTarget"],
    "polar_align": JOB_NAMES["polarAlign"],
}


@interface_bp.route("/command", methods=["POST"])
def telescope_command():
    # One route for the client's commands: {"action": ..., "params": {...}, "telescope_id": ...}
    from flask_login import current_user

    data = request.get_json(silent=True) or {}
    action = data.get("action")
    spec = TELESCOPE_ACTIONS.get(action)
    if spec is None:
        return jsonify({"status": "error", "message": _('Unknown action: %(action)s', action=action)}), 400
    level, plain, run = spec

    telescope_id = requested_telescope_id(data)
    guard = _require_telescope_control_access(telescope_id, level)
    if guard:
        return guard

    params = data.get("params") or {}
    if not isinstance(params, dict):
        return jsonify({"status": "error", "message": "params must be an object"}), 400

    try:
        result = run(Telescope(telescope_id), params, current_user.get_id())
    except _BadParam as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    if plain:
        if isinstance(result, dict) and set(result) == {"error"}:
            return jsonify({"status": "error", "message": result_error(result), "result": result})
        return jsonify({"status": "success", "result": result})
    if action in JOB_ACTIONS:
        return _command_reply(result, job=JOB_ACTIONS[action])
    return _command_reply(result)

@interface_bp.route("/motor_command", methods=["POST"])
def motor_command():
    # Execute motor commands on the telescope
    try:
        data = request.json or {}
        telescope_id = requested_telescope_id(data)
        guard = _require_telescope_control_access(telescope_id)
        if guard:
            return guard

        command = data.get("command")
        args = data.get("args")
        motor_id = data.get("motor_id", "motor1")

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
            return jsonify({"status": "error", "message": _('Unknown motor command: %(command)s', command=command)}), 400

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
        return _command_reply(result, f"Motor command '{command}' executed successfully")

    except Exception:
        logger.exception("Failed to execute motor command")
        return jsonify({"status": "error", "message": _("Failed to execute motor command")}), 500

@interface_bp.route("/get_motors", methods=["POST"])
def get_motors():
    # Get list of available motors for the selected telescope
    try:
        data = request.json or {}
        telescope_id = requested_telescope_id(data)
        guard = _require_telescope_control_access(telescope_id)
        if guard:
            return guard

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
