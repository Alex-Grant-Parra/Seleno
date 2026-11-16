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
    from models.telescope_share import TelescopeShare
    # Show the current user's registered telescopes, who each is shared with, and the
    # telescopes other owners have shared with them
    telescopes = Telescope.query.filter_by(user_id=current_user.id).order_by(Telescope.token_created_at.desc()).all()
    shares_by_telescope = {t.id: [] for t in telescopes}
    if telescopes:
        rows = (
            TelescopeShare.query.filter(TelescopeShare.telescope_id.in_(list(shares_by_telescope)),
                                        TelescopeShare.granted_by_user_id == current_user.id)
            .order_by(TelescopeShare.created_at.asc())
            .all()
        )
        for row in rows:
            shares_by_telescope[row.telescope_id].append(row)
    shared_with_me = (
        db.session.query(TelescopeShare, Telescope)
        .join(Telescope, Telescope.id == TelescopeShare.telescope_id)
        .filter(TelescopeShare.user_id == current_user.id,
                TelescopeShare.granted_by_user_id == Telescope.user_id)
        .order_by(Telescope.telescope_id.asc())
        .all()
    )
    owners = {}
    if shared_with_me:
        from models.user import User
        owner_ids = {t.user_id for _, t in shared_with_me}
        owners = {u.id: u.username for u in User.query.filter(User.id.in_(owner_ids)).all()}
    generated_token = session.pop('new_user_telescope_token', None)
    generated_name = session.pop('new_user_telescope_name', None)
    return render_template(
        'my_telescopes.html',
        telescopes=telescopes,
        shares_by_telescope=shares_by_telescope,
        shared_with_me=shared_with_me,
        owners=owners,
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


# --- sharing: standing access for other users -----------------------------------------------
# Only the owner shares (not admins, not people it's shared with). Those it's shared with use
# the telescope at any time without a booking, but don't manage its bookings
# (app/telescope_access.py).

MAX_SHARES_PER_TELESCOPE = 25


def _find_user(username_or_email):
    # By username, or by email when it has an @ (emails are encrypted with a random nonce,
    # so they're compared after decrypting, as login does)
    from sqlalchemy import func
    from models.user import User
    username = username_or_email
    if '@' in username:
        wanted = username.lower()
        return next((u for u in User.query.all() if (u.get_email() or '').strip().lower() == wanted), None)
    user = User.query.filter_by(username=username).first()
    if user is None:
        # Forgive the case when only one account matches
        matches = User.query.filter(func.lower(User.username) == username.lower()).limit(2).all()
        user = matches[0] if len(matches) == 1 else None
    return user


@telescopes_bp.route('/telescopes/<int:telescope_id>/share', methods=['POST'])
@login_required
def share_telescope(telescope_id):
    from models.tables import Telescope
    from models.telescope_share import TelescopeShare

    rec = Telescope.query.filter_by(id=telescope_id, user_id=current_user.id).first()
    if not rec:
        flash('Only the telescope\'s owner can share it.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    username = (request.form.get('username') or '').strip()
    if not username:
        flash('Enter the username or email of the person to share with.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    user = _find_user(username)
    if user is None:
        if '@' in username:
            flash(f'No account uses the email "{username}".', 'danger')
        else:
            flash(f'No user named "{username}".', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))
    if user.id == current_user.id:
        flash('You already own this telescope.', 'warning')
        return redirect(url_for('telescopes.my_telescopes'))
    if user.is_limited:
        flash(f'{user.username} has a limited account, which cannot control telescopes.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    existing = TelescopeShare.query.filter_by(telescope_id=rec.id, user_id=user.id).first()
    if existing and existing.granted_by_user_id == current_user.id:
        flash(f'"{rec.telescope_id}" is already shared with {user.username}.', 'info')
        return redirect(url_for('telescopes.my_telescopes'))
    count = TelescopeShare.query.filter_by(telescope_id=rec.id, granted_by_user_id=current_user.id).count()
    if count >= MAX_SHARES_PER_TELESCOPE:
        flash(f'A telescope can be shared with at most {MAX_SHARES_PER_TELESCOPE} people.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    if existing:
        # Left over from a previous owner: take it over
        existing.granted_by_user_id = current_user.id
        existing.created_at = datetime.utcnow()
    else:
        db.session.add(TelescopeShare(telescope_id=rec.id, user_id=user.id, granted_by_user_id=current_user.id))
    db.session.commit()
    sec_logger.info(f"telescope_shared: name={rec.telescope_id}, owner_id={current_user.id}, user_id={user.id}")
    flash(f'"{rec.telescope_id}" is now shared with {user.username}. They can use it at any time without a booking.', 'success')
    return redirect(url_for('telescopes.my_telescopes'))


@telescopes_bp.route('/telescopes/<int:telescope_id>/share/<int:share_id>/remove', methods=['POST'])
@login_required
def unshare_telescope(telescope_id, share_id):
    # The owner takes access away, or someone it's shared with gives theirs up
    from models.tables import Telescope
    from models.telescope_share import TelescopeShare

    share = db.session.get(TelescopeShare, share_id)
    rec = db.session.get(Telescope, telescope_id)
    if share is None or rec is None or share.telescope_id != rec.id:
        flash('That share no longer exists.', 'warning')
        return redirect(url_for('telescopes.my_telescopes'))
    is_owner = rec.user_id == current_user.id
    if not (is_owner or share.user_id == current_user.id):
        flash('Only the telescope\'s owner can change who it\'s shared with.', 'danger')
        return redirect(url_for('telescopes.my_telescopes'))

    username = share.user.username if share.user else 'that user'
    user_id, name = share.user_id, rec.telescope_id
    db.session.delete(share)
    db.session.commit()
    sec_logger.info(f"telescope_unshared: name={name}, by_user_id={current_user.id}, user_id={user_id}")
    if is_owner:
        flash(f'"{name}" is no longer shared with {username}.', 'success')
    else:
        flash(f'You no longer have shared access to "{name}".', 'success')
    return redirect(url_for('telescopes.my_telescopes'))


# --- one telescope: its page and its exclusion zones ---------------------------------------
# Exclusion zones (app/dead_zones.py) are parts of the sky the telescope never points at.
# The Pi owns them and enforces them; these pages show what it last reported and send
# changes to it with setDeadZones, so it has to be connected to save.

def _own_telescope(telescope_pk):
    # The telescope row if the current user owns it, has it shared with them (exclusion
    # zones are owner-level, like the interface's Device tab) or is an admin, else None
    from models.tables import Telescope
    from app.telescope_access import may_use_as_owner
    rec = db.session.get(Telescope, telescope_pk)
    if rec is None or not (current_user.is_admin or may_use_as_owner(current_user, rec)):
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
    return render_template('telescope_detail.html', telescope=rec,
                           is_owner=current_user.is_admin or rec.user_id == current_user.id)


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
