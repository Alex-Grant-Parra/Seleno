"""Who may drive which telescope.

A user may control a telescope when they own it, the owner has shared it with
them, they hold an approved booking that covers now, or they are an admin.
Owner-level actions (updating the Pi or its controller, setting its clock or
horizon, clearing an emergency stop) need the owner, someone it's shared with,
or an admin, whatever the Pi itself allows.

Sharing gives standing use of the telescope, not its management: only the owner
(Telescope.user_id) or an admin may manage its bookings and booking settings,
remove it, or share it (is_telescope_owner).
"""
from datetime import datetime, timezone

from flask import jsonify, request, session
from flask_login import current_user

from app.db import db
from flask_babel import gettext as _

# TelescopeBooking.status once the owner has approved the booking
APPROVED_BOOKING_STATE = 'reserved'

CONTROL = 'control'
OWNER = 'owner'


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def requested_telescope_id(data=None):
    # The telescope a request is about: the JSON body, the query string, then the session
    if data is None:
        data = request.get_json(silent=True) or {}
    return (
        data.get('telescope_id')
        or data.get('telescopeId')
        or request.args.get('telescope_id')
        or (session.get('selected_telescope') or {}).get('telescope_id')
    )


def get_telescope_row(telescope_id):
    from models.tables import Telescope
    if not telescope_id:
        return None
    return db.session.query(Telescope).filter_by(telescope_id=telescope_id).first()


def approved_booking(telescope_row, at=None, user_id=None):
    # The approved booking on this telescope that covers `at` (default now), if any
    from models.booking import TelescopeBooking
    if telescope_row is None:
        return None
    at = at or utc_now()
    query = TelescopeBooking.query.filter(
        TelescopeBooking.telescope_id == telescope_row.id,
        TelescopeBooking.status == APPROVED_BOOKING_STATE,
        TelescopeBooking.start_utc <= at,
        TelescopeBooking.end_utc > at,
    )
    if user_id is not None:
        query = query.filter(TelescopeBooking.requester_user_id == user_id)
    return query.order_by(TelescopeBooking.start_utc.asc()).first()


def next_booking_start(telescope_row, after=None):
    # Start of the next approved booking after `after`, so an owner's plan can stop before it
    from models.booking import TelescopeBooking
    after = after or utc_now()
    row = (
        TelescopeBooking.query.filter(
            TelescopeBooking.telescope_id == telescope_row.id,
            TelescopeBooking.status == APPROVED_BOOKING_STATE,
            TelescopeBooking.start_utc > after,
        )
        .order_by(TelescopeBooking.start_utc.asc())
        .first()
    )
    return row.start_utc if row else None


def is_telescope_owner(user, telescope_row):
    return (
        telescope_row is not None
        and telescope_row.user_id is not None
        and telescope_row.user_id == getattr(user, 'id', None)
    )


def has_shared_access(user, telescope_row):
    # The owner has shared this telescope with `user`. A share counts only while
    # whoever granted it still owns the telescope.
    from models.telescope_share import TelescopeShare
    user_id = getattr(user, 'id', None)
    if telescope_row is None or telescope_row.user_id is None or user_id is None:
        return False
    return db.session.query(TelescopeShare.id).filter_by(
        telescope_id=telescope_row.id,
        user_id=user_id,
        granted_by_user_id=telescope_row.user_id,
    ).first() is not None


def may_use_as_owner(user, telescope_row):
    # The owner or someone it's shared with: uses the telescope at any time, as the owner would
    return is_telescope_owner(user, telescope_row) or has_shared_access(user, telescope_row)


def user_can_control(user, telescope_row, level=CONTROL, at=None):
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_limited', False):
        return False
    if getattr(user, 'is_admin', False):
        return True
    if telescope_row is None:
        return False
    if may_use_as_owner(user, telescope_row):
        return True
    if level == OWNER:
        return False
    return approved_booking(telescope_row, at=at, user_id=user.id) is not None


def telescope_access_error(telescope_id, level=CONTROL):
    """None when the current user may act on `telescope_id`, else a JSON error response."""
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": _("Must be logged in to control telescope")}), 401
    if getattr(current_user, 'is_limited', False):
        return jsonify({"status": "error", "message": _("Limited accounts cannot control telescopes.")}), 403
    if not telescope_id:
        return jsonify({"status": "error", "message": _("No telescope selected")}), 400

    row = get_telescope_row(telescope_id)
    if row is None:
        return jsonify({"status": "error", "message": _("Telescope not found")}), 404

    if not user_can_control(current_user, row, level):
        if level == OWNER:
            message = _("Only the telescope's owner, someone it's shared with, or an admin can do that.")
        else:
            message = _("You need to own this telescope, have it shared with you, or hold a booking that covers now.")
        return jsonify({"status": "error", "message": message}), 403
    return None
