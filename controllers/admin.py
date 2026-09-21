import os
import shutil
import json
from collections import deque
from urllib.parse import urlparse

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, session, send_file
from flask_login import login_required, current_user
from models.user import User
from app.db import db
from models.user import AccountStatusHistory
from models.logging import RequestLog, SecurityLog, WebsocketSecurityLog
from models.client_release import ClientReleaseSubmission
from datetime import datetime
from security.ip_blacklist import get_blacklist
import logging

sec_logger = logging.getLogger('security')

# Define the blueprint for admin routes
admin_bp = Blueprint('admin', __name__)


def _redirect_back():
    # Redirect to the referring page on this site, falling back to the admin page
    ref = urlparse(request.referrer or '')
    if ref.netloc == request.host and ref.path.startswith('/') and not ref.path.startswith('//'):
        path = '/' + ref.path.lstrip('/\\')
        return redirect(path + ('?' + ref.query if ref.query else ''))
    return redirect(url_for('admin.admin'))

try:
    from flask_wtf.csrf import exempt
except ImportError:
    # Fallback if import fails
    def exempt(f):
        # Exempt a view from CSRF (fallback)
        return f


@admin_bp.route("/admin")
@login_required
def admin():
    # Render the admin page for administrators
    if current_user.is_admin:
        return render_template('admin.html')  # Render the admin page if the user is an admin

    # Redirect to login page with a flash message if the user is not an admin
    flash('You must be an admin to access this page.', 'warning')
    return redirect(url_for('auth.login'))  # Redirect to login if not an admin


def _admin_guard():
    # Check admin privileges and return a proper response if unauthorized
    if not current_user.is_authenticated or not current_user.is_admin:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'Admin access required.'}), 403
        flash('Admin access required.', 'danger')
        return redirect(url_for('profile.profile'))
    return None


def _parse_int_arg(name, default_value, min_value=None, max_value=None):
    # Parse and clamp integer query args to keep pagination predictable.
    raw_value = request.args.get(name, default_value)
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        parsed_value = default_value

    if min_value is not None and parsed_value < min_value:
        parsed_value = min_value
    if max_value is not None and parsed_value > max_value:
        parsed_value = max_value
    return parsed_value


def _parse_datetime_arg(name):
    # Accept ISO timestamps for optional start/end filters.
    raw_value = (request.args.get(name) or '').strip()
    if not raw_value:
        return None

    candidate = raw_value.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _get_security_log_sources():
    # Return available log stream names for DB-backed and file-backed logs.
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'security', 'logs'))
    files = []
    try:
        if os.path.exists(logs_dir):
            for fn in sorted(os.listdir(logs_dir)):
                if os.path.isfile(os.path.join(logs_dir, fn)):
                    files.append(fn)
    except Exception:
        files = []

    for db_log_name in ('requests.log', 'security.log', 'websocket_security.log'):
        if db_log_name not in files:
            files.append(db_log_name)

    return sorted(files)


def _normalize_request_row(row):
    payload = {}
    if row.headers_json:
        try:
            payload = json.loads(row.headers_json)
        except Exception:
            payload = {}

    compact_message = f"{row.method} {row.path}"
    if row.query_string:
        compact_message += f"?{row.query_string}"

    return {
        'id': row.id,
        'timestamp': row.created_at.isoformat() if row.created_at else None,
        'source': 'requests.log',
        'level': 'INFO',
        'event': 'request',
        'ip': row.client_ip,
        'method': row.method,
        'path': row.path,
        'message': compact_message,
        'raw': {
            'request_timestamp': row.request_timestamp,
            'url': row.url,
            'query_string': row.query_string,
            'remote_addr': row.remote_addr,
            'scheme': row.scheme,
            'headers': payload,
        },
    }


def _normalize_security_row(row):
    payload = {}
    details = {}
    if row.payload_json:
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            payload = {}
    if row.details_json:
        try:
            details = json.loads(row.details_json)
        except Exception:
            details = {}

    message = row.message or ''
    if not message and payload:
        message = json.dumps(payload)

    return {
        'id': row.id,
        'timestamp': row.created_at.isoformat() if row.created_at else None,
        'source': 'security.log',
        'level': row.level,
        'event': row.event_type,
        'ip': row.client_ip,
        'method': row.method,
        'path': row.path,
        'message': message,
        'raw': {
            'event_timestamp': row.event_timestamp,
            'url': row.url,
            'user_agent': row.user_agent,
            'details': details,
            'payload': payload,
        },
    }


def _normalize_websocket_row(row):
    payload = {}
    if row.payload_json:
        try:
            payload = json.loads(row.payload_json)
        except Exception:
            payload = {'payload_json': row.payload_json}

    return {
        'id': row.id,
        'timestamp': row.created_at.isoformat() if row.created_at else None,
        'source': 'websocket_security.log',
        'level': row.level,
        'event': row.event,
        'ip': row.client_ip,
        'method': None,
        'path': None,
        'message': json.dumps(payload) if payload else (row.event or ''),
        'raw': payload,
    }


def _query_file_source(source, page, page_size, filters):
    # Read bounded tail lines for file-based logs, then filter and paginate.
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'security', 'logs'))
    requested = os.path.abspath(os.path.join(logs_dir, source))
    if not requested.startswith(logs_dir + os.sep) or not os.path.exists(requested):
        return None, 'File not found'

    line_window = max(page * page_size * 4, 1500)
    line_window = min(line_window, 10000)

    try:
        with open(requested, 'r', encoding='utf-8', errors='ignore') as fh:
            recent_lines = list(deque(fh, maxlen=line_window))
    except Exception:
        sec_logger.exception("Failed to read log file %s", source)
        return None, 'Failed to read log file'

    filtered = []
    q = filters['q']
    level = filters['level']
    ip = filters['ip']
    method = filters['method']
    path_filter = filters['path']
    event_filter = filters['event']

    for idx, line in enumerate(recent_lines):
        text_line = line.rstrip('\n')
        line_lc = text_line.lower()
        if q and q not in line_lc:
            continue
        if ip and ip not in line_lc:
            continue
        if method and method not in line_lc:
            continue
        if path_filter and path_filter not in line_lc:
            continue
        if event_filter and event_filter not in line_lc:
            continue

        parts = text_line.split(' - ', 2)
        timestamp = None
        parsed_level = None
        message = text_line
        if len(parts) == 3:
            timestamp, parsed_level, message = parts
        elif len(parts) == 2:
            timestamp, message = parts

        if level and (not parsed_level or parsed_level.upper() != level.upper()):
            continue

        filtered.append({
            'id': f"file-{idx}",
            'timestamp': timestamp,
            'source': source,
            'level': parsed_level or 'INFO',
            'event': None,
            'ip': None,
            'method': None,
            'path': None,
            'message': message,
            'raw': {'line': text_line},
        })

    filtered.reverse()
    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    return {
        'items': filtered[start:end],
        'total': total,
    }, None


@admin_bp.route('/admin/user/<int:user_id>/promote', methods=['POST'])
@login_required
def promote_user(user_id):
    # Promote a user to Administrator
    guard = _admin_guard()
    if guard:
        return guard

    if current_user.id == user_id:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'You cannot change your own role.'}), 400
        flash('You cannot change your own role.', 'warning')
        return _redirect_back()

    user = User.query.get(user_id)
    if not user:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404
        flash('User not found.', 'danger')
        return _redirect_back()

    user.AccountType = 'Administrator'
    db.session.commit()
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': f'User {user.username} promoted to Administrator.'})
    flash(f'User {user.username} promoted to Administrator.', 'success')
    return _redirect_back()


@admin_bp.route('/admin/user/<int:user_id>/demote', methods=['POST'])
@login_required
def demote_user(user_id):
    # Demote a user to Standard
    guard = _admin_guard()
    if guard:
        return guard

    if current_user.id == user_id:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'You cannot change your own role.'}), 400
        flash('You cannot change your own role.', 'warning')
        return _redirect_back()

    user = User.query.get(user_id)
    if not user:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404
        flash('User not found.', 'danger')
        return _redirect_back()

    user.AccountType = 'Standard'
    db.session.commit()
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': f'User {user.username} demoted to Standard.'})
    flash(f'User {user.username} demoted to Standard.', 'success')
    return _redirect_back()


@admin_bp.route('/admin/user/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user(user_id):
    # Delete a user account
    guard = _admin_guard()
    if guard:
        return guard

    if current_user.id == user_id:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'You cannot delete your own account.'}), 400
        flash('You cannot delete your own account.', 'warning')
        return _redirect_back()

    user = User.query.get(user_id)
    if not user:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404
        flash('User not found.', 'danger')
        return _redirect_back()

    try:
        db.session.delete(user)
        db.session.commit()
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'success', 'message': f'User {user.username} deleted.'})
        flash(f'User {user.username} deleted.', 'success')
    except Exception:
        db.session.rollback()
        sec_logger.exception("Failed to delete user %s", user_id)
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'Failed to delete user.'}), 500
        flash('Failed to delete user.', 'danger')

    return _redirect_back()



@admin_bp.route('/admin/user/<int:user_id>/set_role', methods=['POST'])
@login_required
@exempt
def set_role(user_id):
    # Set a user's role (Administrator/Standard/Limited)
    guard = _admin_guard()
    if guard:
        return guard

    if current_user.id == user_id:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'You cannot change your own role.'}), 400
        flash('You cannot change your own role.', 'warning')
        return _redirect_back()

    user = User.query.get(user_id)
    if not user:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404
        flash('User not found.', 'danger')
        return _redirect_back()

    json_data = request.get_json(silent=True) if request.is_json else None
    role = (json_data or {}).get('role') or request.form.get('role')

    if not request.is_json and not request.form:
        flash('Security validation failed. Please try again.', 'danger')
        return _redirect_back()
    
    if role not in ['Administrator', 'Standard', 'Limited']:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'Invalid role specified.'}), 400
        flash('Invalid role specified.', 'danger')
        return _redirect_back()

    user.AccountType = role
    db.session.commit()
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': f'{user.username} role set to {role}.'})
    flash(f'{user.username} role set to {role}.', 'success')
    return _redirect_back()


@admin_bp.route('/admin/user/<int:user_id>/toggle_enabled', methods=['POST'])
@login_required
def toggle_enabled(user_id):
    # Toggle a user's enabled/disabled status
    guard = _admin_guard()
    if guard:
        return guard

    if current_user.id == user_id:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'You cannot change your own enabled status.'}), 400
        flash('You cannot change your own enabled status.', 'warning')
        return _redirect_back()

    user = User.query.get(user_id)
    if not user:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'User not found.'}), 404
        flash('User not found.', 'danger')
        return _redirect_back()

    current_status = user.is_enabled()
    # Record the change in AccountStatusHistory
    entry = AccountStatusHistory(user_id=user.id, enabled=(not current_status), changed_by=current_user.id, reason='Toggled by admin')
    db.session.add(entry)
    db.session.commit()
    # Update persistent flag as well
    user.is_enabled_flag = not current_status
    db.session.commit()

    message = f'User {user.username} has been {("enabled" if not current_status else "disabled") }.'
    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': message, 'enabled': not current_status})

    flash(message, 'success')
    return _redirect_back()


@admin_bp.route('/admin/security/logs')
@login_required
def security_logs():
    # Display recent account status history logs
    guard = _admin_guard()
    if guard:
        return guard

    # Fetch recent account status history
    logs = AccountStatusHistory.query.order_by(AccountStatusHistory.changed_at.desc()).limit(200).all()
    return render_template('security_logs.html', logs=logs)


@admin_bp.route('/admin/security')
@login_required
def admin_security_page():
    # Show admin security overview and blacklist stats
    guard = _admin_guard()
    if guard:
        return guard

    blacklist = get_blacklist()
    files = _get_security_log_sources()

    return render_template('admin_security.html', blacklist_stats=blacklist.get_stats(), blacklisted=sorted(list(blacklist.blacklisted_ips)), log_files=files)


@admin_bp.route('/admin/security/logs/query')
@login_required
def admin_security_query_logs():
    # Query logs with filters and pagination for the admin log explorer.
    guard = _admin_guard()
    if guard:
        return guard

    sources = _get_security_log_sources()
    source = (request.args.get('source') or 'security.log').strip()
    if source not in sources:
        return jsonify({'error': 'Invalid log source'}), 400

    page = _parse_int_arg('page', 1, min_value=1, max_value=100000)
    page_size = _parse_int_arg('page_size', 50, min_value=10, max_value=200)
    level = (request.args.get('level') or '').strip().upper()
    if level == 'ALL':
        level = ''

    filters = {
        'q': (request.args.get('q') or '').strip().lower(),
        'level': level,
        'ip': (request.args.get('ip') or '').strip().lower(),
        'method': (request.args.get('method') or '').strip().lower(),
        'path': (request.args.get('path') or '').strip().lower(),
        'event': (request.args.get('event') or '').strip().lower(),
        'start_dt': _parse_datetime_arg('start'),
        'end_dt': _parse_datetime_arg('end'),
    }

    try:
        if source == 'requests.log':
            query = RequestLog.query
            if filters['ip']:
                query = query.filter(RequestLog.client_ip.ilike(f"%{filters['ip']}%"))
            if filters['method']:
                query = query.filter(RequestLog.method.ilike(f"%{filters['method']}%"))
            if filters['path']:
                query = query.filter(RequestLog.path.ilike(f"%{filters['path']}%"))
            if filters['q']:
                like_q = f"%{filters['q']}%"
                query = query.filter(
                    (RequestLog.path.ilike(like_q)) |
                    (RequestLog.url.ilike(like_q)) |
                    (RequestLog.client_ip.ilike(like_q)) |
                    (RequestLog.method.ilike(like_q))
                )
            if filters['start_dt']:
                query = query.filter(RequestLog.created_at >= filters['start_dt'])
            if filters['end_dt']:
                query = query.filter(RequestLog.created_at <= filters['end_dt'])

            total = query.count()
            rows = query.order_by(RequestLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
            items = [_normalize_request_row(row) for row in rows]

        elif source == 'security.log':
            query = SecurityLog.query
            if filters['level']:
                query = query.filter(SecurityLog.level == filters['level'])
            if filters['event']:
                query = query.filter(SecurityLog.event_type.ilike(f"%{filters['event']}%"))
            if filters['ip']:
                query = query.filter(SecurityLog.client_ip.ilike(f"%{filters['ip']}%"))
            if filters['method']:
                query = query.filter(SecurityLog.method.ilike(f"%{filters['method']}%"))
            if filters['path']:
                query = query.filter(SecurityLog.path.ilike(f"%{filters['path']}%"))
            if filters['q']:
                like_q = f"%{filters['q']}%"
                query = query.filter(
                    (SecurityLog.message.ilike(like_q)) |
                    (SecurityLog.payload_json.ilike(like_q)) |
                    (SecurityLog.path.ilike(like_q)) |
                    (SecurityLog.url.ilike(like_q)) |
                    (SecurityLog.event_type.ilike(like_q)) |
                    (SecurityLog.client_ip.ilike(like_q))
                )
            if filters['start_dt']:
                query = query.filter(SecurityLog.created_at >= filters['start_dt'])
            if filters['end_dt']:
                query = query.filter(SecurityLog.created_at <= filters['end_dt'])

            total = query.count()
            rows = query.order_by(SecurityLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
            items = [_normalize_security_row(row) for row in rows]

        elif source == 'websocket_security.log':
            query = WebsocketSecurityLog.query
            if filters['level']:
                query = query.filter(WebsocketSecurityLog.level == filters['level'])
            if filters['event']:
                query = query.filter(WebsocketSecurityLog.event.ilike(f"%{filters['event']}%"))
            if filters['ip']:
                query = query.filter(WebsocketSecurityLog.client_ip.ilike(f"%{filters['ip']}%"))
            if filters['q']:
                like_q = f"%{filters['q']}%"
                query = query.filter(
                    (WebsocketSecurityLog.event.ilike(like_q)) |
                    (WebsocketSecurityLog.client_ip.ilike(like_q)) |
                    (WebsocketSecurityLog.client_id.ilike(like_q)) |
                    (WebsocketSecurityLog.payload_json.ilike(like_q))
                )
            if filters['start_dt']:
                query = query.filter(WebsocketSecurityLog.created_at >= filters['start_dt'])
            if filters['end_dt']:
                query = query.filter(WebsocketSecurityLog.created_at <= filters['end_dt'])

            total = query.count()
            rows = query.order_by(WebsocketSecurityLog.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
            items = [_normalize_websocket_row(row) for row in rows]

        else:
            file_result, file_error = _query_file_source(source, page, page_size, filters)
            if file_error:
                return jsonify({'error': file_error}), 404
            total = file_result['total']
            items = file_result['items']

        total_pages = max((total + page_size - 1) // page_size, 1)

        return jsonify({
            'source': source,
            'filters': {
                'q': filters['q'],
                'level': filters['level'],
                'ip': filters['ip'],
                'method': filters['method'],
                'path': filters['path'],
                'event': filters['event'],
                'start': request.args.get('start') or '',
                'end': request.args.get('end') or '',
            },
            'pagination': {
                'page': page,
                'page_size': page_size,
                'total': total,
                'total_pages': total_pages,
            },
            'items': items,
        })
    except Exception:
        sec_logger.exception("Failed to query security logs")
        return jsonify({'error': 'Failed to load logs'}), 500


@admin_bp.route('/admin/security/logfile/<path:filename>')
@login_required
def admin_security_logfile(filename):
    # Return last lines of a specified security logfile
    guard = _admin_guard()
    if guard:
        return guard

    import os

    if filename == 'requests.log':
        try:
            rows = RequestLog.query.order_by(RequestLog.id.desc()).limit(500).all()
            lines = [row.to_log_line() for row in reversed(rows)]
            return jsonify({'file': filename, 'lines': lines})
        except Exception:
            sec_logger.exception("Failed to load log %s", filename)
            return jsonify({'error': 'Failed to load log'}), 500

    if filename == 'security.log':
        try:
            rows = SecurityLog.query.order_by(SecurityLog.id.desc()).limit(500).all()
            lines = [row.to_log_line() for row in reversed(rows)]
            return jsonify({'file': filename, 'lines': lines})
        except Exception:
            sec_logger.exception("Failed to load log %s", filename)
            return jsonify({'error': 'Failed to load log'}), 500

    if filename == 'websocket_security.log':
        try:
            rows = WebsocketSecurityLog.query.order_by(WebsocketSecurityLog.id.desc()).limit(500).all()
            lines = [row.to_log_line() for row in reversed(rows)]
            return jsonify({'file': filename, 'lines': lines})
        except Exception:
            sec_logger.exception("Failed to load log %s", filename)
            return jsonify({'error': 'Failed to load log'}), 500

    # Prevent path traversal: only serve files from security/logs directory
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'security', 'logs'))
    requested = os.path.abspath(os.path.join(base_dir, filename))
    if not requested.startswith(base_dir + os.sep) or not os.path.exists(requested):
        return jsonify({'error': 'File not found'}), 404

    try:
        with open(requested, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()[-500:]
        return jsonify({'file': filename, 'lines': lines})
    except Exception:
        sec_logger.exception("Failed to read log file %s", filename)
        return jsonify({'error': 'Failed to read log file'}), 500


@admin_bp.route('/admin/blacklist/add', methods=['POST'])
@login_required
def admin_blacklist_add():
    # Add an IP address to the manual blacklist
    guard = _admin_guard()
    if guard:
        return guard

    data = request.get_json() or request.form
    ip = data.get('ip') if isinstance(data, dict) else None
    if not ip:
        return jsonify({'status': 'error', 'message': 'IP required'}), 400

    blacklist = get_blacklist()
    if blacklist.add_manual_ip(ip):
        sec_logger.info(f"manual_blacklist_add: ip={ip}, by={current_user.id}")
        return jsonify({'status': 'success', 'message': f'Added {ip} to blacklist'})
    return jsonify({'status': 'error', 'message': 'Invalid IP'}), 400


@admin_bp.route('/admin/blacklist/remove', methods=['POST'])
@login_required
def admin_blacklist_remove():
    # Remove an IP address from the manual blacklist
    guard = _admin_guard()
    if guard:
        return guard

    data = request.get_json() or request.form
    ip = data.get('ip') if isinstance(data, dict) else None
    if not ip:
        return jsonify({'status': 'error', 'message': 'IP required'}), 400

    blacklist = get_blacklist()
    if blacklist.remove_ip(ip):
        sec_logger.info(f"manual_blacklist_remove: ip={ip}, by={current_user.id}")
        return jsonify({'status': 'success', 'message': f'Removed {ip} from blacklist'})
    return jsonify({'status': 'error', 'message': 'IP not found'}), 404


@admin_bp.route('/admin/users')
@login_required
def admin_users():
    # List users and their trusted device counts
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'danger')
        return redirect(url_for('profile.profile'))

    from models.trusted_device import TrustedDevice
    users = User.query.all()
    user_stats = []

    for user in users:
        trusted_devices = TrustedDevice.get_user_trusted_devices(user.id)
        user_stats.append({
            'user': user,
            'is_enabled': user.is_enabled(),
            'trusted_devices_count': len(trusted_devices),
            'trusted_devices': trusted_devices
        })

    return render_template('admin_users.html', user_stats=user_stats)


@admin_bp.route('/admin/security/logs')
@login_required
def admin_security_logs():
    # Return a list of security log filenames
    guard = _admin_guard()
    if guard:
        return guard

    import os
    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'security', 'logs'))
    print(f"[DEBUG] logs_dir resolved to: {logs_dir}")
    files = []
    try:
        if os.path.exists(logs_dir):
            print(f"[DEBUG] logs_dir exists. Contents: {os.listdir(logs_dir)}")
            for fn in sorted(os.listdir(logs_dir)):
                file_path = os.path.join(logs_dir, fn)
                print(f"[DEBUG] Checking file: {file_path} isfile={os.path.isfile(file_path)}")
                if os.path.isfile(file_path):
                    files.append(fn)
        else:
            print(f"[DEBUG] logs_dir does not exist.")
    except Exception as e:
        print(f"[DEBUG] Exception while listing logs: {e}")
        files = []
    return {'logs': files}


# Telescope management routes
@admin_bp.route('/admin/security/tokens')
@login_required
def admin_security_tokens_legacy():
    return redirect(url_for('admin.admin_manage_telescopes'))


@admin_bp.route('/admin/telescopes')
@login_required
def admin_manage_telescopes():
    # Display telescope tokens and pending approvals
    guard = _admin_guard()
    if guard:
        return guard

    from security.token_store import list_tokens
    from models.tables import Telescope

    telescope_tokens = list_tokens()
    view_tokens = []
    for rec in telescope_tokens:
        token_data = {
            'id': rec.id,
            'truncated': (rec.token_prefix or 'token') + '...',
            'name': rec.name,
            'client_type': rec.client_type,
            'created': rec.created_at.isoformat() if rec.created_at else 'Unknown',
            'db_info': None,
            'is_approved': getattr(rec, 'is_approved', True),
            'is_disabled': getattr(rec, 'is_disabled', False),
            'user_id': getattr(rec, 'user_id', None),
        }

        try:
            # Telescope/token rows are unified in the same SQLAlchemy model.
            # Always expose available DB metadata, even if current type is not "telescope".
            telescope_name = rec.name
            token_data['db_info'] = {
                'type': rec.client_type,
                'ip_address': rec.ip_address,
                'last_seen': rec.last_seen,
                'online': Telescope.is_telescope_online(telescope_name) if telescope_name else False,
            }
        except Exception as e:
            logging.error(f"Error fetching telescope data: {e}")
        
        view_tokens.append(token_data)

    # Pending approvals: user-registered telescopes awaiting admin approval
    pending_approvals = Telescope.query.filter_by(is_approved=False).order_by(Telescope.token_created_at.asc()).all()
    pending_list = []
    for rec in pending_approvals:
        pending_list.append({
            'id': rec.id,
            'name': rec.telescope_id,
            'type': rec.type,
            'user_id': rec.user_id,
            'created': rec.token_created_at.isoformat() if rec.token_created_at else 'Unknown',
        })

    generated_token = session.pop('new_telescope_token', None)
    generated_token_name = session.pop('new_telescope_token_name', None)

    return render_template(
        'security_tokens.html',
        tokens=view_tokens,
        generated_token=generated_token,
        generated_token_name=generated_token_name,
        pending_approvals=pending_list,
    )


@admin_bp.route('/admin/telescopes/generate', methods=['POST'])
@admin_bp.route('/admin/security/tokens/generate', methods=['POST'])
@login_required
def admin_generate_telescope_token():
    # Generate a new telescope token and store for one-time display
    guard = _admin_guard()
    if guard:
        return guard

    from security.token_store import create_token_record, list_tokens
    name = (request.form.get('name') or 'unnamed').strip()
    client_type = (request.form.get('client_type') or 'observer').strip().lower()
    request.form.get('telescope_type') or None

    if client_type not in {'observer', 'telescope', 'developer'}:
        flash('Invalid telescope type selected.', 'danger')
        return redirect(url_for('admin.admin_manage_telescopes'))
    
    # Check for duplicate names
    tokens = list_tokens()
    for rec in tokens:
        if rec.name == name:
            flash(f'Token with name "{name}" already exists. Please choose a different name.', 'danger')
            return redirect(url_for('admin.admin_manage_telescopes'))

    token, _ = create_token_record(name, client_type)
    sec_logger.info(f"token_generated: token={token[:12]}..., name={name}, type={client_type}, by={current_user.id}")

    # One-time token reveal after redirect
    session['new_telescope_token'] = token
    session['new_telescope_token_name'] = name
    
    flash_msg = f'Generated token for {name}: {token} (store securely)'
    if client_type in {'telescope', 'developer'}:
        flash_msg += ' - Client added to telescope tracking database.'
    flash(flash_msg, 'success')
    return redirect(url_for('admin.admin_manage_telescopes'))


@admin_bp.route('/admin/telescopes/<int:telescope_id>/approve', methods=['POST'])
@login_required
def admin_approve_telescope(telescope_id):
    # Approve a user-registered telescope, making it active
    guard = _admin_guard()
    if guard:
        return guard

    from models.tables import Telescope
    rec = db.session.get(Telescope, telescope_id)
    if not rec:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('admin.admin_manage_telescopes'))

    rec.is_approved = True
    db.session.commit()
    sec_logger.info(f"telescope_approved: id={rec.id}, name={rec.telescope_id}, by={current_user.id}")
    flash(f'Telescope "{rec.telescope_id}" approved.', 'success')
    return redirect(url_for('admin.admin_manage_telescopes'))


@admin_bp.route('/admin/telescopes/<int:telescope_id>/toggle_disabled', methods=['POST'])
@login_required
def admin_toggle_telescope_disabled(telescope_id):
    # Force enable or disable a telescope without deleting it
    guard = _admin_guard()
    if guard:
        return guard

    from models.tables import Telescope
    rec = db.session.get(Telescope, telescope_id)
    if not rec:
        flash('Telescope not found.', 'danger')
        return redirect(url_for('admin.admin_manage_telescopes'))

    rec.is_disabled = not rec.is_disabled
    db.session.commit()
    state = 'disabled' if rec.is_disabled else 'enabled'
    sec_logger.info(f"telescope_{state}: id={rec.id}, name={rec.telescope_id}, by={current_user.id}")
    # If the telescope was disabled, ensure any active connection is disconnected
    if rec.is_disabled:
        try:
            from app.WebsocketServer import disconnect_client_by_id
            disconnected = disconnect_client_by_id(rec.telescope_id, reason='admin_disabled')
            disconnect_client_by_id(f"{rec.telescope_id}_liveview", reason='admin_disabled')
            if disconnected:
                sec_logger.info(f"Disconnected active client for disabled telescope: {rec.telescope_id}")
        except Exception:
            pass

    flash(f'Telescope "{rec.telescope_id}" has been {state}.', 'success')
    return redirect(url_for('admin.admin_manage_telescopes'))


@admin_bp.route('/admin/telescopes/revoke', methods=['POST'])
@admin_bp.route('/admin/security/tokens/revoke', methods=['POST'])
@login_required
def admin_revoke_telescope_token():
    # Revoke a telescope token
    guard = _admin_guard()
    if guard:
        return guard

    from security.token_store import get_token_by_id, revoke_token_by_id
    
    identifier = request.form.get('token')
    if not identifier:
        flash('No token specified.', 'danger')
        return redirect(url_for('admin.admin_manage_telescopes'))

    rec = get_token_by_id(identifier)
    if rec:
        revoke_token_by_id(identifier)
        sec_logger.info(f"token_revoked: token_id={rec.id}, by={current_user.id}")

        # Attempt to disconnect any active websocket clients matching this telescope name
        try:
            from app.WebsocketServer import disconnect_client_by_id
            disconnected = disconnect_client_by_id(rec.name, reason='token_revoked')
            # also try liveview client id
            disconnect_client_by_id(f"{rec.name}_liveview", reason='token_revoked')
            if disconnected:
                sec_logger.info(f"Disconnected active client for revoked telescope: {rec.name}")
        except Exception:
            pass

        flash('Telescope token revoked.', 'success')
    else:
        flash('Token not found.', 'danger')

    return redirect(url_for('admin.admin_manage_telescopes'))


@admin_bp.route('/admin/telescopes/<int:telescope_id>/type', methods=['POST'])
@login_required
def admin_update_telescope_type(telescope_id):
    guard = _admin_guard()
    if guard:
        return guard

    from security.token_store import get_token_by_id

    new_type = (request.form.get('client_type') or '').strip().lower()
    if new_type not in {'observer', 'telescope', 'developer'}:
        flash('Invalid telescope type selected.', 'danger')
        return redirect(url_for('admin.admin_manage_telescopes'))

    rec = get_token_by_id(telescope_id)
    if not rec:
        flash('Telescope token not found.', 'danger')
        return redirect(url_for('admin.admin_manage_telescopes'))

    rec.type = new_type
    db.session.commit()
    flash(f'Telescope "{rec.name}" set to type {new_type}.', 'success')
    return redirect(url_for('admin.admin_manage_telescopes'))


@admin_bp.route('/admin/telescopes/show', methods=['POST'])
@admin_bp.route('/admin/security/tokens/show', methods=['POST'])
@login_required
def admin_show_telescope_token():
    # Attempt to show a token (not available after creation)
    guard = _admin_guard()
    if guard:
        return guard

    token_id = request.json.get('token_id') if request.is_json else request.form.get('token_id')
    if not token_id:
        return jsonify({'error': 'Token ID required'}), 400

    return jsonify({'error': 'Full token display is unavailable after creation (stored hashed).'}), 410


@admin_bp.route('/admin/releases/review')
@login_required
def admin_release_review():
    guard = _admin_guard()
    if guard:
        return guard

    submissions = ClientReleaseSubmission.list_for_review()
    return render_template('admin_release_review.html', submissions=submissions)


@admin_bp.route('/admin/releases/review/<int:submission_id>/download')
@login_required
def admin_release_download(submission_id):
    guard = _admin_guard()
    if guard:
        return guard

    submission = ClientReleaseSubmission.get_by_id(submission_id)
    if not submission:
        flash('Submission not found.', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    candidate_path = submission.candidate_file_path()
    published_path = submission.published_file_path()
    file_path = candidate_path if os.path.exists(candidate_path) else published_path

    if not os.path.exists(file_path):
        flash('Submission file was not found on the server.', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    return send_file(file_path, as_attachment=True, download_name=f"{submission.version}.zip")


@admin_bp.route('/admin/releases/review/<int:submission_id>/in-progress', methods=['POST'])
@login_required
def admin_release_mark_in_progress(submission_id):
    guard = _admin_guard()
    if guard:
        return guard

    submission = ClientReleaseSubmission.get_by_id(submission_id)
    if not submission:
        flash('Submission not found.', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    submission.mark_in_progress(admin_user_id=current_user.id)
    flash(f'Release {submission.version} marked in progress.', 'info')
    return redirect(url_for('admin.admin_release_review'))


@admin_bp.route('/admin/releases/review/<int:submission_id>/approve', methods=['POST'])
@login_required
def admin_release_approve(submission_id):
    guard = _admin_guard()
    if guard:
        return guard

    submission = ClientReleaseSubmission.get_by_id(submission_id)
    if not submission:
        flash('Submission not found.', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    source_path = submission.candidate_file_path()
    if not os.path.exists(source_path):
        flash('Submission file is missing. Cannot approve.', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    target_path = submission.published_file_path()
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    try:
        if os.path.exists(target_path):
            os.remove(target_path)
        shutil.move(source_path, target_path)
    except Exception as e:
        flash(f'Failed to publish release: {e}', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    submission.mark_approved(admin_user_id=current_user.id)
    flash(f'Release {submission.version} approved and published to downloads.', 'success')
    return redirect(url_for('admin.admin_release_review'))


@admin_bp.route('/admin/releases/review/<int:submission_id>/delete', methods=['POST'])
@login_required
def admin_release_delete(submission_id):
    guard = _admin_guard()
    if guard:
        return guard

    submission = ClientReleaseSubmission.get_by_id(submission_id)
    if not submission:
        flash('Submission not found.', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    candidate_path = submission.candidate_file_path()
    published_path = submission.published_file_path()

    try:
        if os.path.exists(candidate_path):
            os.remove(candidate_path)
        if os.path.exists(published_path):
            os.remove(published_path)
    except Exception as e:
        flash(f'Failed to delete release file: {e}', 'danger')
        return redirect(url_for('admin.admin_release_review'))

    submission.mark_deleted(admin_user_id=current_user.id)
    flash(f'Release {submission.version} deleted from server.', 'warning')
    return redirect(url_for('admin.admin_release_review'))
