import hashlib
import hmac
import os
import secrets
import time
import jwt
from datetime import datetime, timedelta
from flask_login import UserMixin, login_required, current_user
from app.db import db
from dotenv import load_dotenv  # Import load_dotenv
from flask import Blueprint, jsonify, current_app
from utility.encryption import EncryptedString

# Load environment variables from .env file
load_dotenv()

# Ensure encryption key is configured (utility.encryption will raise otherwise)

class User(UserMixin, db.Model):
    __tablename__ = "user"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    # Store encrypted; transparently returns plaintext when accessed
    email = db.Column(EncryptedString(150), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    AccountType = db.Column(db.String(32), nullable=False, default="Standard")  # "Standard", "Administrator", or "Limited"
    totp_secret = db.Column(EncryptedString(64))
    current_2fa_code = db.Column(EncryptedString(64))
    night_mode = db.Column(db.Boolean, default=False, nullable=False)  # Night mode preference
    # Persistent enabled flag for quick checks (new column)
    is_enabled_flag = db.Column('is_enabled', db.Boolean, default=True, nullable=False)
    # When the account was created (new column)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    # Note: we don't add a persistent is_enabled column here to avoid altering existing DB schema
    # Instead we rely on AccountStatusHistory model to determine current enabled/disabled state.

    @property
    def is_admin(self):
        return self.AccountType == "Administrator"

    @property
    def is_standard(self):
        return self.AccountType == "Standard"

    @property
    def is_limited(self):
        return self.AccountType == "Limited"

    @property
    def can_book_sessions(self):
        return not self.is_limited

    @property
    def can_control_telescopes(self):
        return not self.is_limited

    def set_email(self, email):
        # Assign plaintext; type handles encryption on write
        self.email = email

    def get_email(self):
        # Column accessor returns plaintext
        return self.email

    # Password reset links: a signed JWT that is good for 30 minutes and only
    # until the password changes (it carries a digest of the current hash), so
    # a used or leaked link stops working once the password has been reset.
    def _password_stamp(self):
        return hashlib.sha256((self.password or '').encode('utf-8')).hexdigest()[:16]

    @staticmethod
    def _reset_secret():
        secret = current_app.config.get('SECRET_KEY')
        if not secret:
            raise RuntimeError('SECRET_KEY is not configured')
        return secret

    def get_reset_token(self, expires_sec=1800):
        return jwt.encode(
            {'reset_password': self.id, 'pw': self._password_stamp(),
             'exp': datetime.utcnow() + timedelta(seconds=expires_sec)},
            self._reset_secret(),
            algorithm='HS256'
        )

    @staticmethod
    def verify_reset_token(token):
        try:
            payload = jwt.decode(token, User._reset_secret(), algorithms=['HS256'])
            user = db.session.get(User, int(payload['reset_password']))
        except Exception:
            return None
        if user is None or not hmac.compare_digest(str(payload.get('pw', '')), user._password_stamp()):
            return None
        return user

    # Flask-Login properties
    @property
    def is_active(self):
        # User is active only if their account is enabled
        try:
            return bool(self.is_enabled())
        except Exception:
            return True

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    def get_id(self):
        return str(self.id)

    # TOTP setup
    def set_totp_secret(self):
        self.totp_secret = secrets.token_hex(8)

    # Emailed login codes: six hex characters from the secrets module, good for
    # TWO_FA_CODE_TTL_S and TWO_FA_MAX_ATTEMPTS wrong guesses. The issue time and
    # the guesses so far are kept with the (encrypted) code as "code|issued|tries".
    TWO_FA_CODE_TTL_S = 600
    TWO_FA_MAX_ATTEMPTS = 5

    def generate_totp_code(self):
        code = secrets.token_hex(3)
        self.current_2fa_code = f"{code}|{int(time.time())}|0"
        db.session.commit()
        return code

    def verify_2fa_code(self, code):
        stored = self.current_2fa_code or ''
        parts = stored.split('|')
        if len(parts) != 3:
            # No code issued (or one from before codes expired): ask for a new login
            self.current_2fa_code = None
            db.session.commit()
            return False
        expected, issued, tries = parts[0], int(parts[1] or 0), int(parts[2] or 0)
        if time.time() - issued > self.TWO_FA_CODE_TTL_S or tries >= self.TWO_FA_MAX_ATTEMPTS:
            self.current_2fa_code = None
            db.session.commit()
            return False
        if hmac.compare_digest(expected.lower(), (code or '').strip().lower()):
            self.current_2fa_code = None
            db.session.commit()
            return True
        tries += 1
        self.current_2fa_code = None if tries >= self.TWO_FA_MAX_ATTEMPTS else f"{expected}|{issued}|{tries}"
        db.session.commit()
        return False

    def get_account_type(self):
        return self.AccountType

    # Night mode preference methods
    def set_night_mode(self, is_night_mode):
        # Set the user's night mode preference
        self.night_mode = bool(is_night_mode)
        db.session.commit()

    def get_night_mode(self):
        # Get the user's night mode preference
        return bool(self.night_mode) if self.night_mode is not None else False

    @property
    def account_age(self):
        """Return account age as a datetime.timedelta (UTC now - created_at)."""
        try:
            if not self.created_at:
                return None
            return datetime.utcnow() - self.created_at
        except Exception:
            return None

    def account_age_days(self):
        """Return integer days since account creation (0 if unknown)."""
        td = self.account_age
        if not td:
            return 0
        return max(0, td.days)

    # Account enabled/disabled helpers that consult AccountStatusHistory
    def is_enabled(self):
        # Prefer the persistent flag when available
        try:
            if hasattr(self, 'is_enabled_flag') and self.is_enabled_flag is not None:
                return bool(self.is_enabled_flag)
        except Exception:
            pass

        # Fallback to history lookup
        try:
            from models.user import AccountStatusHistory
            status = AccountStatusHistory.query.filter_by(user_id=self.id).order_by(AccountStatusHistory.changed_at.desc()).first()
            if status is None:
                return True
            return bool(status.enabled)
        except Exception:
            return True

    def set_enabled(self, enabled, changed_by_id=None, reason=None):
        from models.user import AccountStatusHistory
        # Update persistent flag and history
        self.is_enabled_flag = bool(enabled)
        new_status = AccountStatusHistory(user_id=self.id, enabled=bool(enabled), changed_by=changed_by_id, reason=reason)
        db.session.add(new_status)
        db.session.commit()
        return new_status


user_bp = Blueprint("user", __name__)


class AccountStatusHistory(db.Model):
    __tablename__ = 'account_status_history'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    changed_by = db.Column(db.Integer, nullable=True)  # user id of admin who changed status
    reason = db.Column(EncryptedString(1024), nullable=True)
    changed_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship('User', backref='status_history')

@user_bp.route("/user/account_type")
@login_required
def get_account_type_route():
    return jsonify({"account_type": current_user.get_account_type()})

@user_bp.route("/user/night_mode", methods=["GET"])
@login_required
def get_night_mode():
    # Get the current user's night mode preference
    try:
        night_mode = current_user.get_night_mode()
        return jsonify({"status": "success", "night_mode": night_mode})
    except Exception:
        current_app.logger.exception('Failed to get night mode')
        return jsonify({"status": "error", "message": "Failed to get night mode"})

@user_bp.route("/user/night_mode", methods=["POST"])
@login_required
def set_night_mode():
    # Set the current user's night mode preference
    try:
        from flask import request
        data = request.get_json()
        night_mode = data.get('night_mode', False)
        
        current_user.set_night_mode(night_mode)
        
        return jsonify({
            "status": "success", 
            "message": f"Night mode {'enabled' if night_mode else 'disabled'}",
            "night_mode": night_mode
        })
    except Exception:
        current_app.logger.exception('Failed to set night mode')
        return jsonify({"status": "error", "message": "Failed to set night mode"})
