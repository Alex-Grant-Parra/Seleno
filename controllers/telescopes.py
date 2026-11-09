import secrets
import logging
from datetime import datetime

from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user

from app.db import db
from security.token_store import token_hash

sec_logger = logging.getLogger('security')

telescopes_bp = Blueprint('telescopes', __name__)


@telescopes_bp.route('/telescopes')
@login_required
def my_telescopes():
    from models.tables import Telescope
    # Show the current user's registered telescopes
    telescopes = Telescope.query.filter_by(user_id=current_user.id).order_by(Telescope.token_created_at.desc()).all()
    generated_token = session.pop('new_user_telescope_token', None)
    generated_name = session.pop('new_user_telescope_name', None)
    return render_template(
        'my_telescopes.html',
        telescopes=telescopes,
        generated_token=generated_token,
        generated_name=generated_name,
    )


@telescopes_bp.route('/telescopes/register', methods=['POST'])
@login_required
def register_telescope():
    from models.tables import Telescope
    # Register a new telescope under the current user's account
    name = (request.form.get('name') or '').strip()
    if not name:
        flash('Telescope name is required.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    if len(name) > 100:
        flash('Telescope name must be 100 characters or fewer.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    # Prevent duplicate names across the whole table
    existing = Telescope.query.filter_by(telescope_id=name).first()
    if existing:
        flash(f'A telescope named "{name}" already exists. Please choose a different name.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    raw_token = secrets.token_urlsafe(32)
    digest = token_hash(raw_token)

    rec = Telescope(
        telescope_id=name,
        type='telescope',
        token_hash=digest,
        token_prefix=raw_token[:12],
        token_created_at=datetime.utcnow(),
        user_id=current_user.id,
        is_approved=False,  # requires admin approval
        is_disabled=False,
    )
    db.session.add(rec)
    db.session.commit()

    sec_logger.info(
        f"user_telescope_registered: name={name}, user_id={current_user.id}, "
        f"token_prefix={raw_token[:12]}..., pending_approval=True"
    )

    # One-time token reveal via session
    session['new_user_telescope_token'] = raw_token
    session['new_user_telescope_name'] = name
    flash(
        f'Telescope "{name}" registered. Copy your token now — it will not be shown again. '
        'It will become active once an admin approves it.',
        'success',
    )
    return redirect(url_for('telescopes.my_telescopes'))


@telescopes_bp.route('/telescopes/<int:telescope_id>/revoke', methods=['POST'])
@login_required
def revoke_my_telescope(telescope_id):
    from models.tables import Telescope
    # Allow a user to revoke (delete) one of their own telescopes
    rec = Telescope.query.filter_by(id=telescope_id, user_id=current_user.id).first()
    if not rec:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    name = rec.telescope_id
    db.session.delete(rec)
    db.session.commit()
    sec_logger.info(f"user_telescope_revoked: name={name}, user_id={current_user.id}")
    flash(f'Telescope "{name}" has been removed.', 'success')
    return redirect(url_for('telescopes.my_telescopes'))


# --- one telescope: its page and its exclusion zones ---------------------------------------
# Exclusion zones (app/dead_zones.py) are parts of the sky the telescope never points at.
# The Pi owns them and enforces them; these pages show what it last reported and send
# changes to it with setDeadZones, so it has to be connected to save.

def _own_telescope(telescope_pk):
    # The telescope row if the current user owns it (or is an admin), else None
    from models.tables import Telescope
    rec = db.session.get(Telescope, telescope_pk)
    if rec is None or (rec.user_id != current_user.id and not current_user.is_admin):
        return None
    return rec


def _connected(rec):
    from app.WebsocketServer import client_manager
    return client_manager.get_client(rec.telescope_id) is not None


def _zones_reply(rec):
    from app.WebsocketServer import get_latest_status
    from app.dead_zones import site_from_status, zones_from_status

    age_s, status = get_latest_status(rec.telescope_id)
    zones = zones_from_status(status)
    return {
        "status": "success",
        "telescope": rec.telescope_id,
        "connected": _connected(rec),
        "reported": zones is not None,
        "zones": zones or [],
        "site": site_from_status(status) or _site_from_row(rec),
        "age_s": round(age_s, 1) if age_s is not None else None,
    }


def _site_from_row(rec):
    # The latitude and longitude typed into the booking settings, when the telescope
    # hasn't reported its own
    try:
        lat, lon = float(rec.latitude), float(rec.longitude)
    except (TypeError, ValueError):
        return None
    return {"lat": lat, "lon": lon} if -90 <= lat <= 90 and -180 <= lon <= 360 else None


@telescopes_bp.route('/telescopes/<int:telescope_id>')
@login_required
def telescope_page(telescope_id):
    rec = _own_telescope(telescope_id)
    if rec is None:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))
    return render_template('telescope_detail.html', telescope=rec)


@telescopes_bp.route('/telescopes/named/<path:name>')
@login_required
def telescope_page_by_name(name):
    # From pages that know a telescope by its name (the interface)
    from models.tables import Telescope
    rec = Telescope.query.filter_by(telescope_id=name).first()
    if rec is None or _own_telescope(rec.id) is None:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))
    return redirect(url_for('telescopes.telescope_page', telescope_id=rec.id))


@telescopes_bp.route('/telescopes/<int:telescope_id>/zones', methods=['GET'])
@login_required
def telescope_zones(telescope_id):
    from flask import jsonify
    rec = _own_telescope(telescope_id)
    if rec is None:
        return jsonify({"status": "error", "message": "Telescope not found"}), 404
    resp = jsonify(_zones_reply(rec))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@telescopes_bp.route('/telescopes/<int:telescope_id>/zones', methods=['POST'])
@login_required
def save_telescope_zones(telescope_id):
    from flask import jsonify
    from app.dead_zones import clean_zones
    from app.telescopeLink import Telescope as TelescopeLink, result_error
    from app.WebsocketServer import update_status_part

    rec = _own_telescope(telescope_id)
    if rec is None:
        return jsonify({"status": "error", "message": "Telescope not found"}), 404
    data = request.get_json(silent=True) or {}
    try:
        zones = clean_zones(data.get("zones"))
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    if not _connected(rec):
        return jsonify({"status": "error", "message": "The telescope is offline. Exclusion zones are kept on the "
                        "telescope, so it has to be connected to change them."}), 409
    result = TelescopeLink(rec.telescope_id).device.set_dead_zones(zones)
    error = result_error(result)
    if error:
        return jsonify({"status": "error", "message": error}), 502
    if isinstance(result, dict) and isinstance(result.get("zones"), list):
        update_status_part(rec.telescope_id, "dead_zones", {"zones": result["zones"]})
    sec_logger.info(f"telescope_zones_set: name={rec.telescope_id}, user_id={current_user.id}, count={len(zones)}")
    return jsonify(_zones_reply(rec))


@telescopes_bp.route('/telescopes/<int:telescope_id>/zones/map')
@login_required
def telescope_zone_map(telescope_id):
    # The star map at the telescope's site, for drawing exclusion zones on the sky. Only
    # reached from the telescope's own page, and only by its owner.
    rec = _own_telescope(telescope_id)
    if rec is None:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))
    reply = _zones_reply(rec)
    zone_editor = {
        "id": rec.id,
        "name": rec.telescope_id,
        "zones_url": url_for('telescopes.telescope_zones', telescope_id=rec.id),
        "back_url": url_for('telescopes.telescope_page', telescope_id=rec.id),
        "site": reply["site"],
    }
    return render_template("star_map.html", stars=[], selected_telescope=None, zone_editor=zone_editor)
