from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, send_file
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import json
import mimetypes
import uuid
import tempfile
import shutil
import imghdr
import urllib.parse
import urllib.request

from app.db import db
from models.contact import ContactMessage
from models.contact_thread import ContactMessageEntry


contact_bp = Blueprint('contact', __name__)


ALLOWED_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.log', '.txt', '.fits'}
ALLOWED_TICKET_STATUSES = {'new', 'in_progress', 'resolved', 'closed'}


def _allowed_file(filename: str) -> bool:
    _, ext = os.path.splitext(filename.lower())
    return ext in ALLOWED_EXTENSIONS


def _store_contact_attachment(uploaded_file):
    # Store an uploaded support attachment 
    if not uploaded_file or not getattr(uploaded_file, 'filename', None):
        return None, None, None

    orig_fname = secure_filename(uploaded_file.filename)
    if not orig_fname:
        return None, None, 'Invalid attachment filename.'

    if not _allowed_file(orig_fname):
        return None, None, 'Invalid file type. Allowed: .png, .jpg, .jpeg, .log, .txt, .fits'

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    upload_base = ContactMessage.uploads_dir(base_dir)
    try:
        os.makedirs(upload_base, exist_ok=True)
    except Exception:
        pass

    tmp_path = None
    try:
        tmp_dir = tempfile.gettempdir()
        tmp_name = f"contact_{uuid.uuid4().hex}"
        tmp_path = os.path.join(tmp_dir, tmp_name)
        uploaded_file.save(tmp_path)

        max_bytes = current_app.config.get('MAX_CONTENT_LENGTH') or (128 * 1024 * 1024)
        size = os.path.getsize(tmp_path)
        if size > max_bytes:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
            return None, None, 'Uploaded file is too large.'

        _, ext = os.path.splitext(orig_fname.lower())
        if ext in ('.png', '.jpg', '.jpeg'):
            verified = False
            try:
                from PIL import Image
                with Image.open(tmp_path) as im:
                    im.verify()
                verified = True
            except Exception:
                try:
                    img_type = imghdr.what(tmp_path)
                    if img_type in ('png', 'jpeg'):
                        verified = True
                except Exception:
                    verified = False

            if not verified:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
                return None, None, 'Uploaded image appears to be invalid or corrupted.'

        ts = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
        target_dir = os.path.join(upload_base, ts)
        os.makedirs(target_dir, exist_ok=True)
        stored_name = f"{uuid.uuid4().hex}{ext}"
        saved_path = os.path.join(target_dir, stored_name)

        shutil.move(tmp_path, saved_path)
        try:
            os.chmod(saved_path, 0o600)
        except Exception:
            pass

        return saved_path, orig_fname, None
    finally:
        try:
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _verify_turnstile_token(token: str, remote_ip: str = None):
    secret = (current_app.config.get('TURNSTILE_SECRET_KEY') or '').strip()
    if not secret:
        return False, ['missing-secret']
    if not token:
        return False, ['missing-input-response']

    payload = {
        'secret': secret,
        'response': token
    }
    if remote_ip:
        payload['remoteip'] = remote_ip

    body = urllib.parse.urlencode(payload).encode('utf-8')
    req = urllib.request.Request(
        'https://challenges.cloudflare.com/turnstile/v0/siteverify',
        data=body,
        headers={'Content-Type': 'application/x-www-form-urlencoded'}
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return False, ['verification-failed']

    if data.get('success'):
        return True, []
    return False, data.get('error-codes', ['verification-failed'])


# Support landing and user tickets
@contact_bp.route('/support')
def support_home():
    # Minimal Support landing page with links to contact form and tickets list
    return render_template('support.html')


@contact_bp.route('/support/tickets')
def support_tickets():
    # List of the current user's tickets. If not logged in, show prompt
    tickets = []
    require_login = False
    try:
        if getattr(current_user, 'is_authenticated', False):
            tickets = (
                ContactMessage
                .query
                .filter(ContactMessage.user_id == current_user.id)
                .order_by(ContactMessage.created_at.desc())
                .all()
            )
        else:
            require_login = True
    except Exception:
        tickets = []
    return render_template('support_tickets.html', tickets=tickets, require_login=require_login)


@contact_bp.route('/contact', methods=['GET'])
def contact_form():
    prefill_email = ''
    try:
        if current_user.is_authenticated:
            prefill_email = current_user.get_email()
    except Exception:
        prefill_email = ''

    # Options for the dropdown
    type_options = [
        '🔧 Technical Issue / Bug Report',
        '🔭 Telescope Tracking Problem',
        '📷 Camera or Imaging Issue',
        '📚 Help / How-To Question',
        '⭐ Database / Star Catalog Issue',
        '💡 Feature Request',
        '🔒 Privacy or Account Issue',
        '🧪 Experimental Feature Feedback',
        '🌐 Website / Interface Feedback',
        '❓Other'
    ]
    captcha_required_config = bool(current_app.config.get('CONTACT_CAPTCHA_REQUIRED', True))
    captcha_site_key = (current_app.config.get('TURNSTILE_SITE_KEY') or '').strip()
    captcha_secret_key = (current_app.config.get('TURNSTILE_SECRET_KEY') or '').strip()
    captcha_enabled = bool(captcha_site_key)
    captcha_required = bool(captcha_required_config and captcha_site_key and captcha_secret_key)
    return render_template(
        'contact.html',
        prefill_email=prefill_email,
        type_options=type_options,
        captcha_required=captcha_required,
        captcha_enabled=captcha_enabled,
        captcha_site_key=captcha_site_key
    )


@contact_bp.route('/contact', methods=['POST'])
def submit_contact():
    email = (request.form.get('email') or '').strip()
    title = (request.form.get('title') or '').strip()
    message_type = (request.form.get('message_type') or '').strip()
    message = (request.form.get('message') or '').strip()
    client_meta_raw = request.form.get('client_meta')

    captcha_required_config = bool(current_app.config.get('CONTACT_CAPTCHA_REQUIRED', True))
    captcha_site_key = (current_app.config.get('TURNSTILE_SITE_KEY') or '').strip()
    captcha_secret_key = (current_app.config.get('TURNSTILE_SECRET_KEY') or '').strip()
    captcha_required = bool(captcha_required_config and captcha_site_key and captcha_secret_key)
    if captcha_required:
        turnstile_token = (request.form.get('cf-turnstile-response') or '').strip()
        is_valid_captcha, captcha_errors = _verify_turnstile_token(
            turnstile_token,
            request.headers.get('X-Forwarded-For', request.remote_addr)
        )
        if not is_valid_captcha:
            current_app.logger.warning('Contact CAPTCHA failed: %s', ','.join(captcha_errors or []))
            flash('Please complete the CAPTCHA before sending your ticket.', 'danger')
            return redirect(url_for('contact.contact_form'))

    # Basic validation
    if not email or not message_type or not title or not message:
        flash('Please fill out email, type, title, and message.', 'danger')
        return redirect(url_for('contact.contact_form'))

    # Gather server-side metadata
    meta = {
        'server_received_at': datetime.utcnow().isoformat() + 'Z',
        'ip': request.headers.get('X-Forwarded-For', request.remote_addr),
        'user_agent': request.headers.get('User-Agent', ''),
        'accept_language': request.headers.get('Accept-Language', ''),
        'referer': request.headers.get('Referer', ''),
        'url': request.url,
    }
    if title:
        meta['title'] = title

    # Merge client-provided metadata JSON
    try:
        if client_meta_raw:
            client_meta = json.loads(client_meta_raw)
            # Avoid excessive data
            if isinstance(client_meta, dict):
                meta.update(client_meta)
    except Exception:
        pass

    # Handle optional file upload 
    file = request.files.get('attachment')
    saved_path, original_filename, upload_error = _store_contact_attachment(file)
    if upload_error:
        flash(upload_error, 'danger')
        return redirect(url_for('contact.contact_form'))

    if original_filename:
        try:
            if isinstance(meta, dict):
                meta['original_filename'] = original_filename
        except Exception:
            pass

    # Create DB record
    cm = ContactMessage(
        user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None),
        email=email,
        message_type=message_type,
        message=message,
        file_path=saved_path,
        status='new'
    )
    cm.meta = meta
    db.session.add(cm)
    db.session.flush()  # get cm.id before creating entries

    # Seed first conversation entry with the user's initial message
    try:
        first_entry = ContactMessageEntry(
            contact_id=cm.id,
            author='user',
            user_id=(current_user.id if getattr(current_user, 'is_authenticated', False) else None),
            subject=title or None,
            body=message,
            file_path=saved_path,
            original_filename=original_filename
        )
        db.session.add(first_entry)
    except Exception:
        pass

    db.session.commit()

    # Send confirmation email (best effort)
    try:
        from utility.emailer import send_email
        # Prefer user-provided title in subject when available
        effective_title = (title or message_type or 'Your message')
        subject = f"We received your message (Ticket #{cm.id}) - {effective_title}"
        body = (
            f"Hi,\n\nThanks for contacting us. We've received your message under ticket #{cm.id}.\n"
            f"Type: {message_type}\nSent at: {cm.created_at} UTC\n\n"
            "We'll get back to you as soon as possible.\n\n— Telescope Control"
        )
        send_email(current_app, 'support', [email], subject, body, reply_to=current_app.config.get('MAIL_SUPPORT_SENDER'))
    except Exception:
        # Do not fail submission if email fails
        pass

    flash('Thanks! Your message has been sent. We will email you shortly.', 'success')
    return redirect(url_for('contact.contact_thank_you', ticket_id=cm.id))


@contact_bp.route('/contact/thank-you')
def contact_thank_you():
    ticket_id = request.args.get('ticket_id')
    title = None
    try:
        if ticket_id:
            from models.contact import ContactMessage
            cm = ContactMessage.query.get(int(ticket_id))
            if cm and cm.meta:
                title = cm.meta.get('title')
    except Exception:
        title = None
    return render_template('contact_thank_you.html', ticket_id=ticket_id, title=title)


@contact_bp.route('/contact/ping')
def contact_ping():
    # Lightweight endpoint for latency checks
    return jsonify({'ok': True, 'ts': datetime.utcnow().isoformat() + 'Z'})


# Admin: list and view/respond to messages
@contact_bp.route('/admin/contact')
@login_required
def admin_contact_list():
    # Basic admin guard: rely on User.is_admin
    if not getattr(current_user, 'is_admin', False):
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))

    q = ContactMessage.query.order_by(ContactMessage.created_at.desc()).all()
    return render_template('admin_contact_list.html', messages=q)


@contact_bp.route('/admin/contact/bulk-status', methods=['POST'])
@login_required
def admin_contact_bulk_status():
    if not getattr(current_user, 'is_admin', False):
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))

    selected_ids_raw = request.form.getlist('ticket_ids')
    new_status = (request.form.get('status') or '').strip()

    if new_status not in ALLOWED_TICKET_STATUSES:
        flash('Invalid status selection.', 'danger')
        return redirect(url_for('contact.admin_contact_list'))

    selected_ids = []
    for raw_id in selected_ids_raw:
        try:
            selected_ids.append(int(raw_id))
        except Exception:
            continue

    selected_ids = list(set(selected_ids))
    if not selected_ids:
        flash('Select at least one ticket to update.', 'warning')
        return redirect(url_for('contact.admin_contact_list'))

    try:
        tickets = ContactMessage.query.filter(ContactMessage.id.in_(selected_ids)).all()
        updated_count = 0
        for ticket in tickets:
            if ticket.status != new_status:
                ticket.status = new_status
                updated_count += 1

        db.session.commit()
        flash(f'Updated {updated_count} ticket(s) to {new_status}.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to update tickets: {e}', 'danger')

    return redirect(url_for('contact.admin_contact_list'))


@contact_bp.route('/admin/contact/<int:message_id>', methods=['GET', 'POST'])
@login_required
def admin_contact_detail(message_id):
    if not getattr(current_user, 'is_admin', False):
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))

    msg = ContactMessage.query.get_or_404(message_id)
    # Load conversation entries
    entries = ContactMessageEntry.query.filter_by(contact_id=msg.id).order_by(ContactMessageEntry.created_at.asc()).all()
    # Backfill for old tickets without entries
    if not entries:
        seed = ContactMessageEntry(
            contact_id=msg.id,
            author='user',
            user_id=msg.user_id,
            subject=(msg.meta or {}).get('title'),
            body=msg.message,
            file_path=msg.file_path,
            original_filename=(msg.meta or {}).get('original_filename')
        )
        db.session.add(seed)
        db.session.commit()
        entries = [seed]

    if request.method == 'POST':
        action = request.form.get('action')
        status = request.form.get('status') or msg.status
        provided_title = None
        try:
            provided_title = (msg.meta or {}).get('title')
        except Exception:
            provided_title = None
        default_subject = f"Re: {(provided_title or 'Your contact request')} (Ticket #{msg.id})"
        reply_subject = request.form.get('reply_subject') or default_subject
        reply_body = request.form.get('reply_body')

        # Update status if changed
        if status and status != msg.status:
            msg.status = status

        if action == 'send_reply' and reply_body:
            try:
                from utility.emailer import send_email
                send_email(current_app, 'support', [msg.email], reply_subject, reply_body, reply_to=current_app.config.get('MAIL_SUPPORT_SENDER'))
                msg.admin_response = reply_body
                msg.responded_at = datetime.utcnow()
                # Add to threaded history
                try:
                    db.session.add(ContactMessageEntry(contact_id=msg.id, author='admin', user_id=getattr(current_user, 'id', None), subject=reply_subject, body=reply_body))
                except Exception:
                    pass
                flash('Reply sent to user.', 'success')
            except Exception as e:
                flash(f'Failed to send email: {e}', 'danger')

        db.session.commit()
        return redirect(url_for('contact.admin_contact_detail', message_id=msg.id))

    return render_template('admin_contact_detail.html', msg=msg, entries=entries)


@contact_bp.route('/admin/contact/<int:message_id>/delete', methods=['POST'])
@login_required
def admin_contact_delete(message_id):
    # Admin-only hard delete of a support ticket and its conversation entries
    if not getattr(current_user, 'is_admin', False):
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))

    msg = ContactMessage.query.get_or_404(message_id)

    # Best effort cleanup of attachments on disk
    try:
        paths_to_remove = set()
        if msg.file_path:
            paths_to_remove.add(msg.file_path)

        try:
            for entry in ContactMessageEntry.query.filter_by(contact_id=msg.id).all():
                if entry.file_path:
                    paths_to_remove.add(entry.file_path)
        except Exception:
            pass

        for path in paths_to_remove:
            try:
                if path and os.path.exists(path):
                    os.remove(path)
                parent_dir = os.path.dirname(path)
                if parent_dir and os.path.isdir(parent_dir) and not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass
    except Exception:
        # Non fatal, continue with DB delete
        pass

    try:
        db.session.delete(msg)
        db.session.commit()
        flash(f'Ticket #{message_id} deleted.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Failed to delete ticket: {e}', 'danger')

    return redirect(url_for('contact.admin_contact_list'))


@contact_bp.route('/support/tickets/<int:message_id>', methods=['GET', 'POST'])
@login_required
def support_ticket_detail(message_id):
    # User must own the ticket
    msg = ContactMessage.query.get_or_404(message_id)
    if msg.user_id != current_user.id:
        flash('You do not have access to this ticket.', 'danger')
        return redirect(url_for('contact.support_tickets'))

    if request.method == 'POST':
        reply_body = (request.form.get('reply_body') or '').strip()
        attachment = request.files.get('attachment')
        attachment_path, original_filename, upload_error = _store_contact_attachment(attachment)
        if upload_error:
            flash(upload_error, 'danger')
            return redirect(url_for('contact.support_ticket_detail', message_id=msg.id))

        if reply_body or attachment_path:
            db.session.add(
                ContactMessageEntry(
                    contact_id=msg.id,
                    author='user',
                    user_id=current_user.id,
                    body=reply_body or '[Attachment uploaded]',
                    file_path=attachment_path,
                    original_filename=original_filename
                )
            )
            # Set status to in_progress/new when user replies
            if msg.status in ('resolved', 'closed'):
                msg.status = 'in_progress'
            db.session.commit()
            flash('Your reply has been added to the ticket.', 'success')
            return redirect(url_for('contact.support_ticket_detail', message_id=msg.id))
        flash('Please enter a message or attach a file.', 'danger')
        return redirect(url_for('contact.support_ticket_detail', message_id=msg.id))

    entries = ContactMessageEntry.query.filter_by(contact_id=msg.id).order_by(ContactMessageEntry.created_at.asc()).all()
    if not entries:
        seed = ContactMessageEntry(
            contact_id=msg.id,
            author='user',
            user_id=msg.user_id,
            subject=(msg.meta or {}).get('title'),
            body=msg.message,
            file_path=msg.file_path,
            original_filename=(msg.meta or {}).get('original_filename')
        )
        db.session.add(seed)
        db.session.commit()
        entries = [seed]
    return render_template('support_ticket_detail.html', msg=msg, entries=entries)


@contact_bp.route('/support/tickets/<int:message_id>/entries/<int:entry_id>/attachment')
@login_required
def support_ticket_entry_attachment(message_id, entry_id):
    msg = ContactMessage.query.get_or_404(message_id)
    if msg.user_id != current_user.id:
        flash('You do not have access to this ticket.', 'danger')
        return redirect(url_for('contact.support_tickets'))

    entry = ContactMessageEntry.query.filter_by(id=entry_id, contact_id=msg.id).first_or_404()
    if not entry.file_path or not os.path.exists(entry.file_path):
        return jsonify({'error': 'Attachment not found'}), 404

    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        upload_base = ContactMessage.uploads_dir(base_dir)
        real_upload_base = os.path.realpath(upload_base)
        real_path = os.path.realpath(entry.file_path)
        if not real_path.startswith(real_upload_base):
            return jsonify({'error': 'Attachment not accessible'}), 403
    except Exception:
        return jsonify({'error': 'Attachment access error'}), 500

    mime, _ = mimetypes.guess_type(entry.file_path)
    download_name = entry.original_filename or os.path.basename(entry.file_path)

    try:
        return send_file(entry.file_path, mimetype=mime or 'application/octet-stream', as_attachment=True, download_name=download_name)
    except TypeError:
        try:
            return send_file(entry.file_path, mimetype=mime or 'application/octet-stream', as_attachment=True, attachment_filename=download_name)
        except Exception:
            current_app.logger.exception('Failed to send attachment')
            return jsonify({'error': 'Failed to send attachment'}), 500
    except Exception:
        current_app.logger.exception('Failed to send attachment')
        return jsonify({'error': 'Failed to send attachment'}), 500


@contact_bp.route('/admin/contact/<int:message_id>/attachment')
@login_required
def admin_contact_attachment(message_id):
    # Admin only access to attachments
    if not getattr(current_user, 'is_admin', False):
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))

    msg = ContactMessage.query.get_or_404(message_id)
    if not msg.file_path or not os.path.exists(msg.file_path):
        return jsonify({'error': 'Attachment not found'}), 404

    # Ensure the file is inside the uploads directory
    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        upload_base = ContactMessage.uploads_dir(base_dir)
        real_upload_base = os.path.realpath(upload_base)
        real_path = os.path.realpath(msg.file_path)
        if not real_path.startswith(real_upload_base):
            return jsonify({'error': 'Attachment not accessible'}), 403
    except Exception:
        return jsonify({'error': 'Attachment access error'}), 500

    mime, _ = mimetypes.guess_type(msg.file_path)
    as_attachment = True
    try:
        if mime and mime.startswith('image/'):
            as_attachment = False
    except Exception:
        as_attachment = True

    # Determine download filename
    download_name = None
    try:
        download_name = (msg.meta or {}).get('original_filename')
    except Exception:
        download_name = None
    if not download_name:
        download_name = os.path.basename(msg.file_path)

    try:
        return send_file(msg.file_path, mimetype=mime or 'application/octet-stream', as_attachment=as_attachment, download_name=download_name)
    except TypeError:
        # Fallback for older Flask versions: use attachment_filename
        try:
            return send_file(msg.file_path, mimetype=mime or 'application/octet-stream', as_attachment=as_attachment, attachment_filename=download_name)
        except Exception:
            current_app.logger.exception('Failed to send attachment')
            return jsonify({'error': 'Failed to send attachment'}), 500
    except Exception:
        current_app.logger.exception('Failed to send attachment')
        return jsonify({'error': 'Failed to send attachment'}), 500


@contact_bp.route('/admin/contact/<int:message_id>/entries/<int:entry_id>/attachment')
@login_required
def admin_contact_entry_attachment(message_id, entry_id):
    if not getattr(current_user, 'is_admin', False):
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))

    msg = ContactMessage.query.get_or_404(message_id)
    entry = ContactMessageEntry.query.filter_by(id=entry_id, contact_id=msg.id).first_or_404()
    if not entry.file_path or not os.path.exists(entry.file_path):
        return jsonify({'error': 'Attachment not found'}), 404

    try:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        upload_base = ContactMessage.uploads_dir(base_dir)
        real_upload_base = os.path.realpath(upload_base)
        real_path = os.path.realpath(entry.file_path)
        if not real_path.startswith(real_upload_base):
            return jsonify({'error': 'Attachment not accessible'}), 403
    except Exception:
        return jsonify({'error': 'Attachment access error'}), 500

    mime, _ = mimetypes.guess_type(entry.file_path)
    as_attachment = True
    try:
        if mime and mime.startswith('image/'):
            as_attachment = False
    except Exception:
        as_attachment = True

    download_name = entry.original_filename or os.path.basename(entry.file_path)
    try:
        return send_file(entry.file_path, mimetype=mime or 'application/octet-stream', as_attachment=as_attachment, download_name=download_name)
    except TypeError:
        try:
            return send_file(entry.file_path, mimetype=mime or 'application/octet-stream', as_attachment=as_attachment, attachment_filename=download_name)
        except Exception:
            current_app.logger.exception('Failed to send attachment')
            return jsonify({'error': 'Failed to send attachment'}), 500
    except Exception:
        current_app.logger.exception('Failed to send attachment')
        return jsonify({'error': 'Failed to send attachment'}), 500
