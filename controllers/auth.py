from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
import os
from flask_login import login_user, logout_user, current_user, login_required
import logging
sec_logger = logging.getLogger('security')
import json
import ipaddress
import urllib.parse
import urllib.request
from utility.hash import hash_password, check_password
from models.user import User
from models.trusted_device import TrustedDevice
from app.db import db
from utility.emailer import send_email
from datetime import datetime
from flask_babel import gettext as _


auth_bp = Blueprint('auth', __name__)


def is_local_connection():
    # Check if the request is coming from localhost, 127.0.0.1, or a local IP address
    # Get the client IP address
    client_ip = request.remote_addr
    
    # Handle X-Forwarded-For header for reverse proxies
    xff_header = request.headers.get('X-Forwarded-For')
    if xff_header:
        # Take the first IP from the comma-separated list
        client_ip = xff_header.split(',')[0].strip()
    
    # Configurable local IPs/hosts
    local_ips = os.getenv('LOCAL_IP_ADDRESSES', '127.0.0.1,localhost,192.168.0').split(',')
    local_ips = [ip.strip() for ip in local_ips if ip.strip()]

    # Check for localhost variations and configured local IPs
    if client_ip in local_ips or client_ip in ['::1']:
        return True
    
    # Check if the host starts with any configured local host/IP (handles ports like localhost:8080)
    if any(request.host.startswith(h) for h in local_ips):
        return True
    
    # Check for private IP ranges (192.168.x.x, 10.x.x.x, 172.16-31.x.x)
    try:
        ip_obj = ipaddress.ip_address(client_ip)
        if ip_obj.is_private or ip_obj.is_loopback:
            return True
    except ValueError:
        # Invalid IP format, not a local connection
        pass
    
    return False


def _captcha_context(config_key: str):
    captcha_required_config = bool(current_app.config.get(config_key, True))
    captcha_site_key = (current_app.config.get('TURNSTILE_SITE_KEY') or '').strip()
    captcha_secret_key = (current_app.config.get('TURNSTILE_SECRET_KEY') or '').strip()
    captcha_enabled = bool(captcha_site_key)
    captcha_required = bool(captcha_required_config and captcha_site_key and captcha_secret_key)
    return captcha_required, captcha_enabled, captcha_site_key


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

@auth_bp.route("/login", methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        trust_device = request.form.get('trust_device') == 'on'
        
        user = User.query.filter_by(username=username).first()
        if user and check_password(password, user.password):
            # Prevent disabled accounts from logging in
            try:
                if not user.is_enabled():
                    flash(_('This account has been disabled. Contact an administrator.'), 'danger')
                    sec_logger.info(json.dumps({'event': 'login_disabled_attempt', 'user_id': user.id, 'username': username}))
                    return render_template('login.html')
            except Exception:
                pass
            # Check if the request is coming from a local connection
            is_local = is_local_connection()
            
            # Check if this device is already trusted
            is_trusted, device_name = TrustedDevice.is_device_trusted(user.id)
            
            if is_local:
                # Skip 2FA for local connections (localhost, 127.0.0.1, private IPs)
                login_user(user)
                flash(_('Login successful! (2FA bypassed for local connection)'), 'success')
                sec_logger.info(json.dumps({'event': 'login_success', 'user_id': user.id, 'method': 'local_bypass'}))
                return redirect(url_for('home.home'))
            elif is_trusted:
                # Skip 2FA for trusted devices
                login_user(user)
                flash(_('Login successful! (Trusted device: %(device)s)', device=device_name), 'success')
                sec_logger.info(json.dumps({'event': 'login_success', 'user_id': user.id, 'method': 'trusted_device', 'device': device_name}))
                return redirect(url_for('home.home'))
            else:
                # Regular 2FA flow for other connections
                session['user_id'] = user.id  # Store user ID in session
                session['trust_device'] = trust_device  # Store trust device preference
                
                # Generate and send the 2FA code
                totp_code = user.generate_totp_code()
                # Auth-related email sender
                send_email(current_app, 'auth', [user.get_email()], _("Your 2FA Code"), _("Your 2FA code is %(code)s. Please enter this code to complete your login.", code=totp_code))
                flash(_('Check your email for the 2FA code to complete the login.'), 'info')
                sec_logger.info(json.dumps({'event': 'login_2fa_required', 'user_id': user.id}))
                return redirect(url_for('auth.login_2fa'))
        else:
            flash(_('Invalid credentials, please try again.'), 'danger')
            sec_logger.info(json.dumps({'event': 'login_failed', 'username': username}))

    return render_template('login.html')

@auth_bp.route("/logout")
def logout():
    resp = redirect(url_for('home.home'))
    try:
        sec_logger.info(json.dumps({'event': 'logout', 'user_id': current_user.get_id() if current_user else None}))
    except Exception:
        pass
    logout_user()
    session.clear()  # Clear the session
    flash(_('Logged out successfully'), 'info')
    return resp

@auth_bp.route("/register", methods=['GET', 'POST'])
def register():
    captcha_required, captcha_enabled, captcha_site_key = _captcha_context('REGISTER_CAPTCHA_REQUIRED')

    if request.method == 'POST':
        if captcha_required:
            xff = request.headers.get('X-Forwarded-For')
            remote_ip = xff.split(',')[0].strip() if xff else request.remote_addr
            turnstile_token = (request.form.get('cf-turnstile-response') or '').strip()
            is_valid_captcha, captcha_errors = _verify_turnstile_token(turnstile_token, remote_ip)
            if not is_valid_captcha:
                current_app.logger.warning('Register CAPTCHA failed: %s', ','.join(captcha_errors or []))
                flash(_('Please complete the CAPTCHA before creating your account.'), 'danger')
                return redirect(url_for('auth.register'))

        username = request.form['username']
        email = request.form['email']
        password = request.form['password']
        # Ensure the user agreed to Terms of Service
        agree_tos = request.form.get('agree_tos')
        if not agree_tos:
            flash(_('You must agree to the Terms of Service to create an account.'), 'danger')
            return redirect(url_for('auth.register'))

        user_with_same_username = User.query.filter_by(username=username).first()
        users = User.query.all()
        email_taken = any(u.get_email() == email for u in users)

        if user_with_same_username and email_taken:
            flash(_('Username and email already taken. Please choose another two.'), 'danger')
            return redirect(url_for('auth.register'))

        if user_with_same_username:
            flash(_('Username already taken. Please choose another one.'), 'danger')
            return redirect(url_for('auth.register'))

        if email_taken:
            flash(_('Email already used. Please choose another one.'), 'danger')
            return redirect(url_for('auth.register'))

        hashed_password = hash_password(password)
        new_user = User(username=username, email=email, password=hashed_password, created_at=datetime.utcnow())
        new_user.set_email(email)
        new_user.set_totp_secret()  # Generate TOTP secret

        
        db.session.add(new_user)
        db.session.commit()

        login_user(new_user)
        flash(_('Account created and logged in successfully!'), 'success')
        session['_flashes'] = []  # Clear flash messages manually
        return redirect(url_for('home.home'))

    return render_template(
        'register.html',
        captcha_required=captcha_required,
        captcha_enabled=captcha_enabled,
        captcha_site_key=captcha_site_key
    )


@auth_bp.route("/forgot_password", methods=['GET', 'POST'])
def forgot_password():
    captcha_required, captcha_enabled, captcha_site_key = _captcha_context('FORGOT_PASSWORD_CAPTCHA_REQUIRED')

    if request.method == 'POST':
        if captcha_required:
            xff = request.headers.get('X-Forwarded-For')
            remote_ip = xff.split(',')[0].strip() if xff else request.remote_addr
            turnstile_token = (request.form.get('cf-turnstile-response') or '').strip()
            is_valid_captcha, captcha_errors = _verify_turnstile_token(turnstile_token, remote_ip)
            if not is_valid_captcha:
                current_app.logger.warning('Forgot-password CAPTCHA failed: %s', ','.join(captcha_errors or []))
                flash(_('Please complete the CAPTCHA before requesting a reset link.'), 'danger')
                return redirect(url_for('auth.forgot_password'))

        email = request.form['email']
        
        # Search for the user by comparing the decrypted email
        users = User.query.all()  # Get all users from the database
        found_user = None
        for u in users:
            if u.get_email() == email:  # Decrypt email and compare
                found_user = u
                break

        if found_user:
            # Check if request is from local connection
            if is_local_connection():
                reset_token = found_user.get_reset_token()
                flash(_('Password reset token (local connection): %(token)s', token=reset_token), 'info')
                flash(_('Use this token to access the reset link directly.'), 'info')
                return redirect(url_for('auth.login'))
            else:
                # Generate a reset token
                reset_token = found_user.get_reset_token()

                # Send an email with the token link
                reset_url = url_for('auth.reset_password', token=reset_token, _external=True)
                try:
                    send_email(current_app, 'auth', [email], _("Password Reset Request"), _("Click the following link to reset your password: %(url)s", url=reset_url))
                    flash(_('A password reset link has been sent to your email.'), 'info')
                except Exception as e:
                    flash(_("Error sending email: %(error)s", error=str(e)), 'danger')
                    return redirect(url_for('auth.forgot_password'))

                return redirect(url_for('auth.login'))
        else:
            flash(_('No account found with that email address.'), 'danger')

    return render_template(
        'forgot_password.html',
        captcha_required=captcha_required,
        captcha_enabled=captcha_enabled,
        captcha_site_key=captcha_site_key
    )


# Reset password route
@auth_bp.route("/reset_password/<token>", methods=['GET', 'POST'])
def reset_password(token):
    user = User.verify_reset_token(token)

    if not user:
        flash(_('That is an invalid or expired token.'), 'danger')
        return redirect(url_for('auth.forgot_password'))

    # Print statement to console
    print(f"Password reset link clicked for user ID: {user.id}")

    if request.method == 'POST':
        password = request.form['password']
        user.password = hash_password(password)  # Update password
        db.session.commit()
        flash(_('Your password has been updated!'), 'success')
        return redirect(url_for('auth.login'))

    return render_template('reset_password.html', token=token)


@auth_bp.route("/login/2FA", methods=['GET', 'POST'])
def login_2fa():
    if request.method == 'POST':
        user_id = session.get('user_id')
        trust_device = session.get('trust_device', False)
        
        if not user_id:
            flash(_('Session expired. Please log in again.'), 'danger')
            return redirect(url_for('auth.login'))

        user = User.query.get(user_id)
        # Ensure account is still enabled before completing 2FA
        if user:
            try:
                if not user.is_enabled():
                    flash(_('This account has been disabled. Contact an administrator.'), 'danger')
                    sec_logger.info(json.dumps({'event': 'login_2fa_disabled_attempt', 'user_id': user.id}))
                    session.pop('user_id', None)
                    session.pop('trust_device', None)
                    return redirect(url_for('auth.login'))
            except Exception:
                pass
        totp_code = request.form['totp']

        if user.verify_2fa_code(totp_code):
            if trust_device:
                device_name, token = TrustedDevice.trust_device(user.id, trust_for_days=30)
                # Set a cookie that will persist for the trust period, httponly to reduce XSS risk
                resp = redirect(url_for('home.home'))
                resp.set_cookie('trusted_device_token', token, max_age=30*24*60*60, httponly=True, samesite='Lax')
                flash(_('Login successful! Device "%(device)s" is now trusted for 30 days.', device=device_name), 'success')
                login_user(user)
                session.pop('user_id', None)  # Remove user_id from session
                session.pop('trust_device', None)  # Remove trust_device from session
                return resp
            else:
                flash(_('Login successful!'), 'success')
            
            login_user(user)
            session.pop('user_id', None)  # Remove user_id from session
            session.pop('trust_device', None)  # Remove trust_device from session
            return redirect(url_for('home.home'))
        else:
            flash(_('Invalid 2FA code. Please try again.'), 'danger')

    return render_template('login_2fa.html')


@auth_bp.route("/trusted_devices")
@login_required
def trusted_devices():
    # for old route
    return redirect(url_for('profile.profile_trusted_devices'))


@auth_bp.route("/revoke_device/<int:device_id>", methods=['POST'])
@login_required
def revoke_device(device_id):
    # Revoke trust for a specific device
    if TrustedDevice.revoke_device(current_user.id, device_id):
        flash(_('Device trust revoked successfully.'), 'success')
    else:
        flash(_('Device not found or could not be revoked.'), 'danger')
    return redirect(url_for('auth.trusted_devices'))


@auth_bp.route("/device_info")
@login_required
def device_info():
    # Debug route to show device fingerprint information
    # Get device info from TrustedDevice
    fingerprint = TrustedDevice.generate_device_fingerprint()
    device_name = TrustedDevice.get_device_name()

    # Sanitize and normalize IP address header: prefer X-Forwarded-For but validate it
    xff = request.headers.get('X-Forwarded-For') or request.environ.get('HTTP_X_FORWARDED_FOR')
    if xff:
        # X-Forwarded-For can contain a comma-separated list; take the first valid-looking entry
        ip = xff.split(',')[0].strip()
    else:
        ip = request.remote_addr or request.environ.get('REMOTE_ADDR', 'Unknown')

    # Mask fingerprint to avoid leaking full device identifiers in debug view
    masked_fingerprint = None
    if fingerprint:
        # Show only first and last 4 chars (e.g. abcd...wxyz) to reduce info leakage
        fp = str(fingerprint)
        if len(fp) > 8:
            masked_fingerprint = f"{fp[:4]}...{fp[-4:]}"
        else:
            masked_fingerprint = fp

    # Render a template which uses Jinja2 auto-escaping to avoid XSS
    return render_template('device_info.html',
                           device_name=device_name,
                           fingerprint=masked_fingerprint,
                           user_agent=request.headers.get('User-Agent', 'Unknown'),
                           host=request.host)
