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
        return jsonify({"status": "error", "message": "No camera setting given"}), 400
    return jsonify({"status": "success", "message": "Settings updated", "changed": changed})

