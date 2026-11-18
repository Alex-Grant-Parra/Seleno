"""The telescope's own settings (the front panel's Setup items), from a phone or
a browser: what the telescope's getSettings() describes, changed with
setSettings(). The owner's. On the telescope's hotspot the app sends these to
the telescope itself, which answers the same (app/offline_access.py)."""
import logging

from flask import Blueprint, jsonify, render_template, request
from flask_babel import gettext as _
from flask_login import current_user

from app.telescopeLink import Telescope, result_error
from app.telescope_access import OWNER, requested_telescope_id, telescope_access_error

logger = logging.getLogger(__name__)

telescope_settings_bp = Blueprint("telescope_settings", __name__, url_prefix="/interface/telescope_settings")

# Reading the settings waits on nothing slow; saving may restart the hotspot
GET_TIMEOUT_S = 8
SET_TIMEOUT_S = 15
MAX_CHANGES = 40


def _too_old(result):
    error = result_error(result) or ""
    return "unknown function" in error.lower() or "not found" in error.lower()


@telescope_settings_bp.route("/")
def page():
    if not current_user.is_authenticated:
        return render_template("errors/401.html"), 401
    return render_template("telescope_settings.html", telescope_id=requested_telescope_id({}))


@telescope_settings_bp.route("/data", methods=["GET"])
def read_settings():
    telescope_id = requested_telescope_id({})
    guard = telescope_access_error(telescope_id, OWNER)
    if guard:
        return guard
    result = Telescope(telescope_id).send_command("getSettings", timeout=GET_TIMEOUT_S)
    if _too_old(result):
        return jsonify({"status": "error", "message": _("This telescope's software is too old to change its settings from here. Update it first.")})
    error = result_error(result)
    if error:
        return jsonify({"status": "error", "message": error})
    return jsonify({"status": "success", "telescope_id": telescope_id, "settings": result})


@telescope_settings_bp.route("/data", methods=["POST"])
def save_settings():
    data = request.get_json(silent=True) or {}
    telescope_id = requested_telescope_id(data)
    guard = telescope_access_error(telescope_id, OWNER)
    if guard:
        return guard
    changes = data.get("changes")
    if not isinstance(changes, dict) or not changes:
        return jsonify({"status": "error", "message": _("Nothing to change")}), 400
    if len(changes) > MAX_CHANGES or not all(isinstance(k, str) and len(k) <= 64 for k in changes):
        return jsonify({"status": "error", "message": _("Too many changes at once")}), 400
    result = Telescope(telescope_id).send_command("setSettings", [changes], timeout=SET_TIMEOUT_S)
    if _too_old(result):
        return jsonify({"status": "error", "message": _("This telescope's software is too old to change its settings from here. Update it first.")})
    error = result_error(result)
    if error:
        return jsonify({"status": "error", "message": error, "result": result})
    return jsonify({"status": "success", "result": result})
