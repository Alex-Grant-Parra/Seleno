"""Capture plan routes for the interface page's Capture panel: presets, a live
estimate while building, start, progress, pause/resume/skip/stop, and the
user's saved plans."""
import json
import logging
from datetime import datetime

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app.capture_plans import (
    PRESETS,
    check_camera_settings,
    estimate_plan,
    learned_overhead,
    prepare_plan,
    recent_frame_sizes,
    record_plan_status,
    validate_plan,
)
from app.db import db
from app.telescopeLink import Telescope, result_error
from app.telescope_access import (
    get_telescope_row,
    is_telescope_owner,
    requested_telescope_id,
    telescope_access_error,
)
from flask_babel import gettext as _

logger = logging.getLogger(__name__)

capture_bp = Blueprint("capture", __name__, url_prefix="/interface/capture")

MAX_SAVED_PLANS = 100
CONTROL_ACTIONS = {
    "pause": "pause",
    "resume": "resume",
    "skip": "skip_step",
    "stop": "stop",
}


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _active_plan(telescope_row):
    from models.capture_plan import ACTIVE_PLAN_STATES, CapturePlan
    return (
        CapturePlan.query.filter(CapturePlan.telescope_id == telescope_row.id, CapturePlan.state.in_(ACTIVE_PLAN_STATES))
        .order_by(CapturePlan.created_at.desc())
        .first()
    )


def _may_manage_plan(row, telescope_row):
    # The plan's owner (the booking's holder), the telescope's owner, or an admin
    return (
        current_user.is_admin
        or is_telescope_owner(current_user, telescope_row)
        or row.owner_id == current_user.id
    )


@capture_bp.route("/presets", methods=["GET"])
def presets():
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in")}), 401
    return jsonify({"status": "success", "presets": PRESETS})


@capture_bp.route("/estimate", methods=["POST"])
def estimate():
    # Shape errors and the time, frames and storage a plan needs, while it's being built
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in")}), 401
    data = _json_body()
    plan = data.get("plan")
    row = get_telescope_row(requested_telescope_id(data))
    overhead = learned_overhead(row) if row else None
    result = estimate_plan(
        plan if isinstance(plan, dict) else {},
        **({"overhead_s": overhead} if overhead else {}),
        sizes=recent_frame_sizes(current_user.id),
    )
    return jsonify({"status": "success", "errors": validate_plan(plan), "estimate": result})


@capture_bp.route("/start", methods=["POST"])
def start():
    from models.capture_plan import CapturePlan

    data = _json_body()
    telescope_id = requested_telescope_id(data)
    guard = telescope_access_error(telescope_id)
    if guard:
        return guard
    row = get_telescope_row(telescope_id)

    if _active_plan(row) is not None:
        return jsonify({"status": "error", "message": _("A plan is already running on this telescope; stop it first")}), 409

    plan, context, errors = prepare_plan(current_user, row, data.get("plan"))
    if errors:
        return jsonify({"status": "error", "message": errors[0], "errors": errors}), 400

    t = Telescope(telescope_id)

    # Check settings against what the camera offers, when the plan sets any
    if any(key in step for step in plan["steps"] for key in ("iso", "shutter", "aperture", "white_balance", "image_format", "exposure_compensation")):
        choices = t.camera.get_settings()
        error = result_error(choices)
        if error:
            return jsonify({"status": "error", "message": _("Couldn't read the camera's settings: %(error)s", error=error)}), 502
        errors = check_camera_settings(plan, choices)
        if errors:
            return jsonify({"status": "error", "message": errors[0], "errors": errors}), 400

    record = CapturePlan(
        id=plan["id"],
        owner_id=context["owner_id"],
        started_by_user_id=current_user.id,
        telescope_id=row.id,
        booking_id=context["booking_id"],
        name=plan["name"],
        plan_json=json.dumps(plan),
        state="starting",
        end_by=context["end_by"],
    )
    db.session.add(record)
    db.session.commit()

    result = t.plans.start(plan)
    error = result_error(result)
    if error:
        record.state = "failed"
        record.finished_at = datetime.utcnow()
        record.last_status_json = json.dumps({"error": error})
        db.session.commit()
        return jsonify({"status": "error", "message": error})

    if record.state == "starting":
        record.state = "running"
    record.started_at = datetime.utcnow()
    db.session.commit()
    return jsonify({"status": "success", "plan": record.to_dict(), "estimate": context["estimate"], "result": result})


@capture_bp.route("/status", methods=["GET"])
def status():
    # The telescope's current plan; live=1 also asks the Pi, rather than waiting for its status push
    from models.capture_plan import CapturePlan

    telescope_id = requested_telescope_id({})
    guard = telescope_access_error(telescope_id)
    if guard:
        return guard
    row = get_telescope_row(telescope_id)

    plan_id = request.args.get("plan_id")
    record = db.session.get(CapturePlan, plan_id) if plan_id else _active_plan(row)
    if record is not None and record.telescope_id != row.id:
        record = None

    if record is not None and request.args.get("live") == "1":
        live = Telescope(telescope_id).plans.status(record.id)
        if isinstance(live, dict) and not live.get("error"):
            live.setdefault("id", record.id)
            record_plan_status(telescope_id, live)
            db.session.refresh(record)

    return jsonify({"status": "success", "plan": record.to_dict() if record else None})


@capture_bp.route("/control", methods=["POST"])
def control():
    # Pause, resume, skip a step or stop the running plan
    data = _json_body()
    action = data.get("action")
    if action not in CONTROL_ACTIONS:
        return jsonify({"status": "error", "message": "action must be pause, resume, skip or stop"}), 400

    telescope_id = requested_telescope_id(data)
    guard = telescope_access_error(telescope_id)
    if guard:
        return guard
    row = get_telescope_row(telescope_id)

    record = _active_plan(row)
    if record is None:
        return jsonify({"status": "error", "message": _("No plan is running on this telescope")}), 404
    if not _may_manage_plan(record, row):
        return jsonify({"status": "error", "message": _("Only the plan's owner, the telescope's owner or an admin can change it")}), 403

    plans = Telescope(telescope_id).plans
    result = getattr(plans, CONTROL_ACTIONS[action])()
    error = result_error(result)
    if error:
        return jsonify({"status": "error", "message": error, "result": result})

    if isinstance(result, dict):
        result.setdefault("id", record.id)
        record_plan_status(telescope_id, result)
    elif isinstance(result, str) and action in ("pause", "resume", "stop"):
        record_plan_status(telescope_id, {"id": record.id, "state": result})
    if action == "stop":
        record.stop_requested_at = datetime.utcnow()
        db.session.commit()
    db.session.refresh(record)
    return jsonify({"status": "success", "plan": record.to_dict(), "result": result})


@capture_bp.route("/history", methods=["GET"])
def history():
    # This user's recent plans on the telescope
    from models.capture_plan import CapturePlan

    telescope_id = requested_telescope_id({})
    guard = telescope_access_error(telescope_id)
    if guard:
        return guard
    row = get_telescope_row(telescope_id)
    query = CapturePlan.query.filter(CapturePlan.telescope_id == row.id)
    if not (current_user.is_admin or is_telescope_owner(current_user, row)):
        query = query.filter(CapturePlan.owner_id == current_user.id)
    rows = query.order_by(CapturePlan.created_at.desc()).limit(20).all()
    return jsonify({"status": "success", "plans": [r.to_dict() for r in rows]})


@capture_bp.route("/saved", methods=["GET"])
def saved_list():
    from models.capture_plan import SavedCapturePlan
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in")}), 401
    rows = SavedCapturePlan.query.filter_by(owner_id=current_user.id).order_by(SavedCapturePlan.updated_at.desc()).all()
    return jsonify({"status": "success", "plans": [r.to_dict() for r in rows]})


@capture_bp.route("/saved", methods=["POST"])
def saved_save():
    # Save a plan from the builder; with an id, replace that saved plan
    from models.capture_plan import SavedCapturePlan
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in")}), 401

    data = _json_body()
    plan = data.get("plan")
    errors = validate_plan(plan)
    if errors:
        return jsonify({"status": "error", "message": errors[0], "errors": errors}), 400
    keep = {key: plan[key] for key in ("name", "steps", "on_error", "upload", "park_at_end") if key in plan}
    encoded = json.dumps(keep)
    if len(encoded) > 256 * 1024:
        return jsonify({"status": "error", "message": _("The plan is too large to save")}), 400

    row = None
    if data.get("id") is not None:
        row = SavedCapturePlan.query.filter_by(id=data.get("id"), owner_id=current_user.id).first()
        if row is None:
            return jsonify({"status": "error", "message": _("Saved plan not found")}), 404
    else:
        if SavedCapturePlan.query.filter_by(owner_id=current_user.id).count() >= MAX_SAVED_PLANS:
            return jsonify({"status": "error", "message": _('You can save up to %(MAX_SAVED_PLANS)s plans', MAX_SAVED_PLANS=MAX_SAVED_PLANS)}), 400
        row = SavedCapturePlan(owner_id=current_user.id)
        db.session.add(row)
    row.name = keep["name"]
    row.plan_json = encoded
    db.session.commit()
    return jsonify({"status": "success", "plan": row.to_dict()})


@capture_bp.route("/saved/<int:plan_id>", methods=["DELETE"])
def saved_delete(plan_id):
    from models.capture_plan import SavedCapturePlan
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in")}), 401
    row = SavedCapturePlan.query.filter_by(id=plan_id, owner_id=current_user.id).first()
    if row is None:
        return jsonify({"status": "error", "message": _("Saved plan not found")}), 404
    db.session.delete(row)
    db.session.commit()
    return jsonify({"status": "success"})
