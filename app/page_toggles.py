"""On/off switches for site pages, read from config/pages.txt.

A switched-off page shows an "Under Construction" page instead. The file is
re-read whenever it changes, so edits apply without a restart.
"""
import os
import threading

PAGES_FILE = os.path.join(os.path.dirname(__file__), "..", "config", "pages.txt")

# Paths that must always work, whatever the file says
ALWAYS_ON = ("/static", "/login", "/logout", "/register", "/forgot_password",
             "/reset_password", "/admin", "/security", "/errors", "/sitemap.xml")

_lock = threading.Lock()
_cache = {"mtime": None, "states": {}}


def _parse(path):
    states = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if not key.startswith("/"):
                continue
            key = key.rstrip("/") or "/"
            states[key] = value.lower() in ("on", "yes", "true", "1", "enabled")
    return states


def _states():
    try:
        mtime = os.path.getmtime(PAGES_FILE)
    except OSError:
        return {}
    with _lock:
        if _cache["mtime"] != mtime:
            try:
                _cache["states"] = _parse(PAGES_FILE)
            except Exception as e:
                print(f"[WARNING] Could not read {PAGES_FILE}: {e}")
                _cache.setdefault("states", {})
            _cache["mtime"] = mtime
        return _cache["states"]


def is_page_disabled(path):
    """True if the most specific matching line in pages.txt says off."""
    path = path.rstrip("/") or "/"
    if any(path == p or path.startswith(p + "/") for p in ALWAYS_ON):
        return False
    states = _states()
    if path == "/":
        return states.get("/") is False
    best, best_len = None, -1
    for key, on in states.items():
        if key == "/":
            continue
        if (path == key or path.startswith(key + "/")) and len(key) > best_len:
            best, best_len = on, len(key)
    return best is False
