"""The seleno.org app talking straight to a telescope on its offline hotspot
(app/offline_access.py explains it all).

    GET /api/telescope/offline_credentials   the telescope, with its API token
                                             (Authorization: Bearer): its name,
                                             certificate, pass secret, the app's
                                             origins and the offline pack
    GET /interface/offline_passes            the app, logged in: a pass for each
                                             telescope the user may use offline
"""
from flask import Blueprint, Response, jsonify, request
from flask_login import current_user

from app import offline_access

offline_access_bp = Blueprint("offline_access", __name__)


def _telescope_from_token():
    from security.token_store import verify_token

    auth = (request.headers.get("Authorization") or "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return None, Response("API token required", status=401, mimetype="text/plain")
    rec, reason = verify_token(token)
    if not rec:
        return None, Response(f"Token refused: {reason}", status=401, mimetype="text/plain")
    if (rec.client_type or "").strip().lower() not in ("telescope", "developer"):
        return None, Response("Only telescope tokens", status=403, mimetype="text/plain")
    return rec, None


@offline_access_bp.route("/api/telescope/offline_credentials", methods=["GET"])
def offline_credentials():
    row, error = _telescope_from_token()
    if error:
        return error
    resp = jsonify(offline_access.credentials_for(row))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@offline_access_bp.route("/interface/offline_passes", methods=["GET"])
def offline_passes():
    if not current_user.is_authenticated:
        return jsonify({"status": "error", "message": "Log in first"}), 401
    resp = jsonify({"status": "success", "telescopes": offline_access.passes_for(current_user),
                    "origin": request.host_url.rstrip("/")})
    resp.headers["Cache-Control"] = "no-store"
    return resp
