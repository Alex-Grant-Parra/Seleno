"""Language support (English + Spanish) via Flask-Babel.

How text gets translated:
  * Templates:  {{ _('Start tracking') }}
  * Python:     from flask_babel import gettext as _   ->  _('Booking confirmed')
                (use lazy_gettext for module-level constants)
  * JavaScript: _('Start tracking')  - served from /i18n/catalog.js

The Spanish lives in translations/es/LC_MESSAGES/messages.po. English is the
text written in the code, so it has no catalogue of its own. Run
`python scripts/translations.py update` after marking new text.
"""
import json
import os
from functools import lru_cache
from urllib.parse import urlsplit

from flask import Blueprint, Response, has_request_context, redirect, request, url_for
from flask_babel import Babel, get_locale

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TRANSLATIONS_DIR = os.path.join(BASE_DIR, "translations")

DEFAULT_LANGUAGE = "en"
# Shown in their own language so people can always find theirs
LANGUAGES = {"en": "English", "es": "Español"}
COOKIE_NAME = "lang"
COOKIE_MAX_AGE = 365 * 24 * 3600

i18n_bp = Blueprint("i18n", __name__)


def select_locale():
    # Background work (threads, the websocket server) has no visitor: English
    if not has_request_context():
        return DEFAULT_LANGUAGE
    chosen = request.cookies.get(COOKIE_NAME)
    if chosen in LANGUAGES:
        return chosen
    return request.accept_languages.best_match(LANGUAGES.keys()) or DEFAULT_LANGUAGE


def compile_catalogs():
    """Rebuild any .mo that is older than its .po, so edits show after a restart."""
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    for lang in LANGUAGES:
        po_path = os.path.join(TRANSLATIONS_DIR, lang, "LC_MESSAGES", "messages.po")
        mo_path = po_path[:-3] + ".mo"
        if not os.path.exists(po_path):
            continue
        if os.path.exists(mo_path) and os.path.getmtime(mo_path) >= os.path.getmtime(po_path):
            continue
        try:
            with open(po_path, "rb") as f:
                catalog = read_po(f, locale=lang)
            with open(mo_path, "wb") as f:
                write_mo(f, catalog)  # fuzzy (unconfirmed) entries are left out
        except Exception as e:
            print(f"Could not compile {po_path}: {e}")


@lru_cache(maxsize=None)
def _js_strings(lang, mtime):
    """Translations of strings used in static/js, for the browser."""
    from babel.messages.pofile import read_po

    po_path = os.path.join(TRANSLATIONS_DIR, lang, "LC_MESSAGES", "messages.po")
    with open(po_path, "rb") as f:
        catalog = read_po(f, locale=lang)
    out = {}
    for msg in catalog:
        if not msg.id or not msg.string or msg.fuzzy:
            continue
        if not any(path.startswith("static/js/") for path, _ in msg.locations):
            continue
        # Same key as gettext: "context\x04text" when the text has a context
        key = (msg.context + "\x04" if msg.context else "") + (msg.id[0] if isinstance(msg.id, tuple) else msg.id)
        out[key] = list(msg.string) if isinstance(msg.id, tuple) else msg.string
    return out


def _safe_next(target):
    """Only allow redirects back to this site."""
    if not target:
        return "/"
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


@i18n_bp.route("/lang/<code>")
def set_language(code):
    resp = redirect(_safe_next(request.args.get("next")))
    if code in LANGUAGES:
        resp.set_cookie(COOKIE_NAME, code, max_age=COOKIE_MAX_AGE,
                        samesite="Lax", secure=request.is_secure, httponly=True)
    return resp


def _po_mtime(lang):
    po_path = os.path.join(TRANSLATIONS_DIR, lang, "LC_MESSAGES", "messages.po")
    return int(os.path.getmtime(po_path)) if os.path.exists(po_path) else 0


def catalog_url(lang):
    """The script's URL names the language and the .po version, so caches keep each apart."""
    return url_for("i18n.js_catalog", lang=lang, v=_po_mtime(lang))


@i18n_bp.route("/i18n/catalog.js")
def js_catalog():
    requested = request.args.get("lang")
    lang = requested if requested in LANGUAGES else str(get_locale() or DEFAULT_LANGUAGE)
    po_path = os.path.join(TRANSLATIONS_DIR, lang, "LC_MESSAGES", "messages.po")
    strings = {}
    if lang != DEFAULT_LANGUAGE and os.path.exists(po_path):
        strings = _js_strings(lang, os.path.getmtime(po_path))
    body = (
        "window.I18N_LANG=" + json.dumps(lang) + ";"
        "window.I18N=" + json.dumps(strings, ensure_ascii=False) + ";"
        # _('Hello {name}', {name: 'Ana'}) - placeholders use {braces}.
        # _p('button', 'Live view off') - the same English with its own translation.
        "window._=function(s,v,c){var o=window.I18N[c?c+'\\u0004'+s:s];if(Array.isArray(o))o=o[0];o=o||s;"
        "if(v)for(var k in v)o=o.split('{'+k+'}').join(v[k]);return o;};"
        "window._p=function(c,s,v){return window._(s,v,c);};"
    )
    resp = Response(body, mimetype="application/javascript")
    if requested in LANGUAGES and request.args.get("v"):
        # Versioned URL (see catalog_url): safe to keep for a long time
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        resp.headers["Cache-Control"] = "no-store"
    return resp


def init_i18n(app):
    app.config.setdefault("BABEL_DEFAULT_LOCALE", DEFAULT_LANGUAGE)
    app.config.setdefault("BABEL_TRANSLATION_DIRECTORIES", TRANSLATIONS_DIR)
    compile_catalogs()
    Babel(app, locale_selector=select_locale)
    app.register_blueprint(i18n_bp)

    @app.context_processor
    def _language_context():
        current = str(get_locale() or DEFAULT_LANGUAGE)
        return {"current_language": current, "languages": LANGUAGES, "catalog_url": catalog_url}
