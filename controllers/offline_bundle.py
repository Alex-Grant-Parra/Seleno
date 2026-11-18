"""The offline server bundle, for telescopes to download while online.

A telescope in offline mode with its hotspot on runs a copy of this server on
its Pi (app/offline_mode.py), so phones on the hotspot get the interface, star
map and album. scripts/build_offline_bundle.py makes the copy; telescopes
fetch it here with their API token (`Authorization: Bearer <token>`), from the
front panel's "Get phone app". If-None-Match with the version they have
answers 304 when it's current.
"""
import json
import os

from flask import Blueprint, Response, request, send_file

BUNDLE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "downloads", "offline")
BUNDLE_NAME = "seleno-offline.tar.gz"
VERSION_NAME = "version.json"

offline_bundle_bp = Blueprint("offline_bundle", __name__, url_prefix="/api/telescope")


def _reply(status, message=""):
    return Response(message, status=status, mimetype="text/plain")


def bundle_info():
    """{"version", "sha256", "bytes", ...} of the built bundle, or None."""
    try:
        with open(os.path.join(BUNDLE_DIR, VERSION_NAME)) as f:
            info = json.load(f)
    except (OSError, ValueError):
        return None
    if not os.path.isfile(os.path.join(BUNDLE_DIR, BUNDLE_NAME)):
        return None
    return info if isinstance(info, dict) and info.get("version") else None


@offline_bundle_bp.route("/offline_bundle", methods=["GET", "HEAD"])
def offline_bundle():
    from security.token_store import verify_token

    auth = (request.headers.get("Authorization") or "").strip()
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        return _reply(401, "API token required")
    rec, reason = verify_token(token)
    if not rec:
        return _reply(401, f"Token refused: {reason}")
    if (rec.client_type or "").strip().lower() not in ("telescope", "developer"):
        return _reply(403, "Only telescope tokens can download the offline server")

    info = bundle_info()
    if info is None:
        return _reply(404, "No offline server has been built on this server yet")
    version = str(info["version"])
    headers = {"ETag": f'"{version}"', "X-Seleno-Bundle-Version": version,
               "X-Seleno-Bundle-Sha256": str(info.get("sha256") or "")}
    if request.headers.get("If-None-Match", "").strip().strip('"') == version:
        resp = _reply(304)
        resp.headers.update(headers)
        return resp
    resp = send_file(os.path.join(BUNDLE_DIR, BUNDLE_NAME), mimetype="application/gzip",
                     as_attachment=True, download_name=BUNDLE_NAME, conditional=False, max_age=0)
    resp.headers.update(headers)
    return resp
