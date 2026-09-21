from flask import Blueprint, render_template, abort, flash, redirect, url_for
from flask_login import login_required, current_user
import os

# Define the blueprint for error routes
error_bp = Blueprint('error', __name__)

# List of valid error codes that have templates
VALID_ERROR_CODES = [400, 401, 403, 404, 500, 502, 503, 504]
_TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), '..', 'templates')
ERROR_TEMPLATES = {
    c: f'errors/{c}.html' for c in VALID_ERROR_CODES
    if os.path.exists(os.path.join(_TEMPLATES_DIR, 'errors', f'{c}.html'))
}
ERROR_STATUS = {t: c for c, t in ERROR_TEMPLATES.items()}

def _admin_guard():
    if not current_user.is_authenticated or not current_user.is_admin:
        flash('Admin access required.', 'danger')
        return redirect(url_for('home.home'))
    return None

# Testing routes for manually triggering error pages
@error_bp.route('/errors/<int:code>')
@login_required
def test_error(code):
    # Manually trigger error pages for testing purposes - Admin only
    
    guard = _admin_guard()
    if guard:
        return guard
    
    # Look up the template for a known error code
    template_path = ERROR_TEMPLATES.get(code)
    if template_path is None:
        abort(404)  # If invalid code, show 404

    # Render the error template directly
    return render_template(template_path), ERROR_STATUS[template_path]

@error_bp.route('/errors')
@login_required
def error_list():
    # Show a list of available error pages for testing - Admin only
    
    guard = _admin_guard()
    if guard:
        return guard
        
    return render_template('error_list.html', error_codes=VALID_ERROR_CODES)

@error_bp.app_errorhandler(404)
def page_not_found(e):
    return render_template("errors/404.html"), 404

@error_bp.app_errorhandler(500)
def internal_error(e):
    return render_template("errors/500.html"), 500

@error_bp.app_errorhandler(403)
def forbidden(e):
    return render_template("errors/403.html"), 403

@error_bp.app_errorhandler(401)
def unauthorized(e):
    return render_template("errors/401.html"), 401

@error_bp.app_errorhandler(502)
def bad_gateway(e):
    return render_template("errors/502.html"), 502

@error_bp.app_errorhandler(503)
def service_unavailable(e):
    return render_template("errors/503.html"), 503

@error_bp.app_errorhandler(504)
def gateway_timeout(e):
    return render_template("errors/504.html"), 504

@error_bp.app_errorhandler(400)
def bad_request(e):
    return render_template("errors/400.html"), 400
