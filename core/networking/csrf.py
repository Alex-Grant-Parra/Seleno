import time
import requests
from typing import Optional, Dict, Any
from utils.log import get_logger

log = get_logger("csrf")

# Reusable HTTP session for cookies and connection pooling
SESSION = requests.Session()

# Simple in-memory cache for CSRF token
_CSRF_TOKEN: Optional[str] = None
_CSRF_FETCH_TS: float = 0.0
_CSRF_TTL_SECONDS = 10 * 60  # Refresh every 10 minutes


def _extract_csrf_from_json(data: Dict[str, Any]) -> Optional[str]:
    # Try common keys to find a CSRF token in a JSON payload.
    for key in (
        "csrfToken",
        "csrf_token",
        "token",
        "csrf",
        "xsrfToken",
        "XSRFToken",
    ):
        val = data.get(key)
        if isinstance(val, str) and val:
            return val
    # Fallback: search any string value containing 'csrf' or 'xsrf'
    for k, v in data.items():
        if isinstance(v, str) and ("csrf" in k.lower() or "xsrf" in k.lower()):
            return v
    return None


def get_csrf_token(server_url: str, force_refresh: bool = False) -> Optional[str]:
    # Fetch and cache a CSRF token from <server_url>/security/csrf-token.
    # Returns the token string if found, otherwise None. Uses a shared session so
    # any cookie-based associations are preserved.
    global _CSRF_TOKEN, _CSRF_FETCH_TS

    now = time.monotonic()
    # If we have a cached token and it's fresh, reuse it only if we still have cookies
    if (
        not force_refresh
        and _CSRF_TOKEN
        and (now - _CSRF_FETCH_TS) < _CSRF_TTL_SECONDS
        and len(SESSION.cookies) > 0
    ):
        return _CSRF_TOKEN

    endpoint = f"{server_url}/security/csrf-token"
    try:
        resp = SESSION.get(endpoint, timeout=10)
        resp.raise_for_status()
        token: Optional[str] = None
        # Try JSON first
        try:
            data = resp.json()
            if isinstance(data, dict):
                token = _extract_csrf_from_json(data)
        except ValueError:
            # Not JSON; try plain text
            text = resp.text.strip()
            if text:
                token = text

        if token:
            _CSRF_TOKEN = token
            _CSRF_FETCH_TS = now
            return token
        else:
            log.info("CSRF endpoint responded but no token was found in the payload.")
            return None
    except requests.RequestException as e:
        log.warning(f"Failed to fetch CSRF token: {e}")
        return None
