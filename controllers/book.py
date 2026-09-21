from datetime import datetime, timezone

from flask import Blueprint, render_template, request, jsonify, redirect, url_for, flash, current_app
from flask_login import login_required, current_user

from app.db import db
from app.booking_service import (
    search_telescopes,
    request_booking,
    set_booking_decision,
    admin_override_booking,
    save_telescope_metadata,
    add_availability_block,
    get_owner_bookings,
    get_user_bookings,
    get_owner_for_telescope,
    get_user,
    to_utc_windows,
)
from models.booking import TelescopeBooking, TelescopeAvailabilityBlock
from utility.emailer import send_email


book_bp = Blueprint('book', __name__)


def _require_booking_access():
    if getattr(current_user, 'is_limited', False):
        return jsonify({'status': 'error', 'message': 'Limited accounts cannot book sessions.'}), 403
    return None


@book_bp.route('/book', methods=['GET'])
@login_required
def book_page():
    guard = _require_booking_access()
    if guard:
        return guard
    return render_template('book.html')


@book_bp.route('/book/search', methods=['POST'])
@login_required
def book_search():
    guard = _require_booking_access()
    if guard:
        return guard

    payload = request.get_json(silent=True) or request.form

    date_list = payload.get('dates')
    if isinstance(date_list, str):
        date_list = [d.strip() for d in date_list.split(',') if d.strip()]

    start_time = (payload.get('start_time') or '').strip()
    end_time = (payload.get('end_time') or '').strip()
    timezone_name = (payload.get('timezone') or 'UTC').strip()
    location_query = (payload.get('location_query') or '').strip()

    if not date_list or not start_time or not end_time:
        return jsonify({'status': 'error', 'message': 'dates, start_time, and end_time are required'}), 400

    try:
        results = search_telescopes(location_query, date_list, start_time, end_time, timezone_name)
        return jsonify({'status': 'success', 'results': results})
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@book_bp.route('/book/request', methods=['POST'])
@login_required
def book_request():
    guard = _require_booking_access()
    if guard:
        return guard

    payload = request.get_json(silent=True) or request.form

    try:
        telescope_id = int(payload.get('telescope_id'))
    except Exception:
        return jsonify({'status': 'error', 'message': 'Invalid telescope_id'}), 400

    date_list = payload.get('dates')
    if isinstance(date_list, str):
        date_list = [d.strip() for d in date_list.split(',') if d.strip()]

    start_time = (payload.get('start_time') or '').strip()
    end_time = (payload.get('end_time') or '').strip()
    timezone_name = (payload.get('timezone') or 'UTC').strip()
    note = (payload.get('note') or '').strip()

    if not date_list or not start_time or not end_time:
        return jsonify({'status': 'error', 'message': 'dates, start_time, and end_time are required'}), 400

    booking_ids, err = request_booking(
        requester_user_id=current_user.id,
        telescope_id=telescope_id,
        date_list=date_list,
        start_time_hhmm=start_time,
        end_time_hhmm=end_time,
        user_timezone=timezone_name,
        note=note,
    )
    if err:
        return jsonify({'status': 'error', 'message': err}), 400

    owner = get_owner_for_telescope(telescope_id)
    if owner:
        try:
            windows = to_utc_windows(date_list, start_time, end_time, timezone_name)
            body = (
                f"New booking request for your telescope.\n\n"
                f"Requester: {current_user.username}\n"
                f"Telescope ID: {telescope_id}\n"
                f"Requested windows (UTC):\n" +
                "\n".join([f"- {s.isoformat()} to {e.isoformat()}" for s, e in windows]) +
                f"\n\nReview requests: {url_for('book.owner_bookings_page', _external=True)}"
            )
            send_email(
                current_app,
                'support',
                [owner.get_email()],
                'SELENO Booking Request Pending Approval',
                body,
            )
        except Exception:
            pass

    return jsonify({'status': 'success', 'booking_ids': booking_ids, 'message': 'Booking request submitted and pending approval.'})


@book_bp.route('/book/my', methods=['GET'])
@login_required
def my_bookings_page():
    guard = _require_booking_access()
    if guard:
        return guard

    bookings = get_user_bookings(current_user.id)
    return render_template('book_my.html', bookings=bookings)


@book_bp.route('/book/owner', methods=['GET'])
@login_required
def owner_bookings_page():
    guard = _require_booking_access()
    if guard:
        return guard

    bookings = get_owner_bookings(current_user.id)
    return render_template('book_owner.html', bookings=bookings)


@book_bp.route('/book/owner/booking/<int:booking_id>/decision', methods=['POST'])
@login_required
def owner_booking_decision(booking_id):
    guard = _require_booking_access()
    if guard:
        return guard

    payload = request.get_json(silent=True) or request.form
    decision = (payload.get('decision') or '').strip().lower()

    booking = db.session.get(TelescopeBooking, booking_id)
    if not booking:
        return jsonify({'status': 'error', 'message': 'Booking not found'}), 404

    if booking.owner_user_id != current_user.id and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Not allowed'}), 403

    updated, err = set_booking_decision(
        actor_user_id=current_user.id,
        booking_id=booking_id,
        decision=decision,
        is_admin=current_user.is_admin,
    )
    if err:
        return jsonify({'status': 'error', 'message': err}), 400

    requester = get_user(updated.requester_user_id)
    if requester:
        try:
            verb = {
                'reserved': 'approved',
                'rejected': 'rejected',
                'cancelled': 'cancelled by the owner',
            }.get(updated.status, updated.status)
            send_email(
                current_app,
                'support',
                [requester.get_email()],
                f"SELENO Booking {updated.status.title()}",
                (
                    f"Your booking #{updated.id} has been {verb}.\n"
                    f"Telescope ID: {updated.telescope_id}\n"
                    f"UTC window: {updated.start_utc.isoformat()} to {updated.end_utc.isoformat()}\n"
                    f"Details: {url_for('book.my_bookings_page', _external=True)}"
                ),
            )
        except Exception:
            pass

    return jsonify({'status': 'success', 'booking_status': updated.status})


@book_bp.route('/book/telescope/<int:telescope_id>/settings', methods=['GET', 'POST'])
@login_required
def telescope_booking_settings(telescope_id):
    guard = _require_booking_access()
    if guard:
        return guard

    from models.tables import Telescope

    telescope = db.session.get(Telescope, telescope_id)
    if not telescope:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('book.owner_bookings_page'))

    if telescope.user_id != current_user.id and not current_user.is_admin:
        flash('Not allowed.', 'danger')
        return redirect(url_for('book.owner_bookings_page'))

    if request.method == 'POST':
        payload = request.get_json(silent=True) or request.form.to_dict(flat=True)
        try:
            save_telescope_metadata(
                actor_user_id=current_user.id,
                telescope=telescope,
                payload=payload,
                is_admin=current_user.is_admin,
            )
            flash('Telescope booking settings saved.', 'success')
        except Exception as exc:
            flash(f'Failed to save settings: {exc}', 'danger')

        return redirect(url_for('book.telescope_booking_settings', telescope_id=telescope.id))

    availability_blocks = TelescopeAvailabilityBlock.query.filter_by(telescope_id=telescope.id).order_by(TelescopeAvailabilityBlock.start_utc.asc()).all()
    return render_template('book_telescope_settings.html', telescope=telescope, availability_blocks=availability_blocks)


@book_bp.route('/book/telescope/<int:telescope_id>/availability', methods=['POST'])
@login_required
def add_telescope_availability(telescope_id):
    guard = _require_booking_access()
    if guard:
        return guard

    from models.tables import Telescope

    telescope = db.session.get(Telescope, telescope_id)
    if not telescope:
        return jsonify({'status': 'error', 'message': 'Telescope not found'}), 404

    if telescope.user_id != current_user.id and not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Not allowed'}), 403

    payload = request.get_json(silent=True) or request.form
    block_type = (payload.get('block_type') or 'unavailable').strip().lower()
    if block_type not in {'available', 'unavailable'}:
        return jsonify({'status': 'error', 'message': 'block_type must be available or unavailable'}), 400

    try:
        start_utc = datetime.fromisoformat((payload.get('start_utc') or '').replace('Z', '+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = datetime.fromisoformat((payload.get('end_utc') or '').replace('Z', '+00:00')).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return jsonify({'status': 'error', 'message': 'start_utc and end_utc must be ISO timestamps'}), 400

    if end_utc <= start_utc:
        return jsonify({'status': 'error', 'message': 'end_utc must be after start_utc'}), 400

    try:
        row = add_availability_block(current_user.id, telescope, block_type, start_utc, end_utc)
        return jsonify({'status': 'success', 'id': row.id})
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@book_bp.route('/admin/bookings/<int:booking_id>/override', methods=['POST'])
@login_required
def admin_booking_override(booking_id):
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Admin access required'}), 403

    payload = request.get_json(silent=True) or request.form
    new_status = (payload.get('new_status') or '').strip().lower()

    booking, err = admin_override_booking(current_user.id, booking_id, new_status)
    if err:
        return jsonify({'status': 'error', 'message': err}), 400

    return jsonify({'status': 'success', 'booking_status': booking.status})
