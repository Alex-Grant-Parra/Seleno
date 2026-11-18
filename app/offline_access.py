"""The seleno.org app talking straight to a telescope, offline.

In offline mode a telescope makes its own Wi-Fi hotspot with no internet
(SelenoClient: core/networking/hotspot.py). Phones on it still use the
seleno.org app, installed and cached from here: its pages run on the phone,
and its service worker (static/js/star_map_sw.js) sends the requests that are
about the telescope to the telescope itself (SelenoClient:
core/networking/phone_link.py, utils/phone_api.py). The star map stays the
app's own; only what it wants tracked goes to the telescope.

A page from https://seleno.org can only talk https, so each telescope gets:
* a name, <slug>.tel.<APP_DOMAIN>, pointing at its hotspot address
  (OFFLINE_HOTSPOT_ADDRESS, 10.42.0.1), published on Cloudflare;
* a Let's Encrypt certificate for it (app/acme.py, DNS-01), renewed here and
  fetched by the telescope while online (/api/telescope/offline_credentials);
* a secret, shared with the telescope, that users' offline passes are signed
  with. While online the app asks /interface/offline_passes for a pass for
  each telescope it may use (owner, shared, or a booking: only for the
  booking's time) and keeps them on the phone.

Without CLOUDFLARE_API_TOKEN nothing is published or issued: the rest works,
and telescopes are told their certificate is pending.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

PASS_DAYS = 30
RENEW_DAYS = 30
JOB_INTERVAL_S = 6 * 3600
API_PORT = 8443
LIVEVIEW_PORT = 8444

_job_lock = threading.Lock()
_wake = threading.Event()


def app_domain():
    return (os.getenv("APP_DOMAIN") or "seleno.org").strip().lower()


def name_suffix():
    return (os.getenv("OFFLINE_TLS_SUFFIX") or f"tel.{app_domain()}").strip().lower().strip(".")


def hotspot_address():
    return os.getenv("OFFLINE_HOTSPOT_ADDRESS") or "10.42.0.1"


def app_origins():
    domain = app_domain()
    extra = [o.strip().rstrip("/") for o in (os.getenv("OFFLINE_APP_ORIGINS") or "").split(",") if o.strip()]
    return [f"https://{domain}", f"https://www.{domain}", *extra]


def slug_for(row):
    """A DNS label for the telescope: its name, lower case, and its row id so
    two telescopes with the same name never share one."""
    base = re.sub(r"[^a-z0-9]+", "-", (row.telescope_id or "").lower()).strip("-")[:40] or "telescope"
    return f"{base}-{row.id}"


def host_for(record):
    return f"{record.slug}.{name_suffix()}"


def record_for(row, create=True):
    """The telescope's TelescopeOffline row (made the first time)."""
    from app.db import db
    from models.telescope_offline import TelescopeOffline, _register_cleanup

    _register_cleanup()
    record = db.session.get(TelescopeOffline, row.id)
    if record is None and create:
        record = TelescopeOffline(telescope_row_id=row.id, slug=slug_for(row), secret=secrets.token_urlsafe(32))
        db.session.add(record)
        db.session.commit()
        wake_job()
    return record


# --- passes ------------------------------------------------------------------------------

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def sign(secret, prefix, payload):
    """prefix.<base64url JSON>.<base64url HMAC-SHA256>, as the telescope checks it
    (SelenoClient core/networking/phone_link.py verify)."""
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = hmac.new(secret.encode(), f"{prefix}.{body}".encode(), hashlib.sha256).digest()
    return f"{prefix}.{body}.{_b64(mac)}"


def make_pass(record, row, user, role, nbf=None, exp=None):
    now = time.time()
    payload = {"t": row.telescope_id, "u": user.id, "n": user.username, "r": role,
               "nbf": int(nbf if nbf is not None else now - 300),
               "exp": int(exp if exp is not None else now + PASS_DAYS * 86400)}
    return sign(record.secret, "v1", payload), payload


def _epoch(dt):
    from calendar import timegm
    return timegm(dt.timetuple())


def passes_for(user):
    """[{telescope_id, host, api_port, liveview_port, pass, role, exp}] for every
    telescope the user may use offline: their own, shared with them, and
    bookings (each pass only for the booking's time) in the next PASS_DAYS."""
    from app.db import db
    from app.telescope_access import APPROVED_BOOKING_STATE, utc_now
    from models.booking import TelescopeBooking
    from models.tables import Telescope
    from models.telescope_share import TelescopeShare

    if not getattr(user, "is_authenticated", False) or not user.can_control_telescopes:
        return []
    out = []

    def add(row, role, nbf=None, exp=None):
        if row is None or row.is_disabled or (row.type or "").lower() not in ("telescope", "developer"):
            return
        record = record_for(row)
        token, payload = make_pass(record, row, user, role, nbf, exp)
        out.append({"telescope_id": row.telescope_id, "host": host_for(record), "api_port": API_PORT,
                    "liveview_port": LIVEVIEW_PORT, "pass": token, "role": role, "nbf": payload["nbf"],
                    "exp": payload["exp"], "certificate": bool(record.cert_pem)})

    seen = set()
    for row in Telescope.query.filter_by(user_id=user.id).all():
        seen.add(row.id)
        add(row, "owner")
    for share in TelescopeShare.query.filter_by(user_id=user.id).all():
        if share.telescope_id not in seen:
            seen.add(share.telescope_id)
            add(db.session.get(Telescope, share.telescope_id), "shared")
    now = utc_now()
    try:
        bookings = (TelescopeBooking.query
                    .filter(TelescopeBooking.requester_user_id == user.id,
                            TelescopeBooking.status == APPROVED_BOOKING_STATE,
                            TelescopeBooking.end_utc > now,
                            TelescopeBooking.start_utc < now + timedelta(days=PASS_DAYS))
                    .all())
    except Exception:
        logger.exception("Couldn't read bookings for offline passes")
        bookings = []
    for booking in bookings:
        if booking.telescope_id in seen:
            continue
        add(db.session.get(Telescope, booking.telescope_id), "booking", nbf=_epoch(booking.start_utc),
            exp=_epoch(booking.end_utc))
    return out


# --- what the telescope gets --------------------------------------------------------------

def offline_pack():
    """Basic mode's featured targets and the named objects, for the telescope
    to answer the app's search and suggestions offline (SelenoClient
    utils/offline_pack.py)."""
    from controllers.interface_api import resolve_target
    from models import star_catalog
    from models.album import FeaturedTarget

    targets = []
    for target in FeaturedTarget.query.filter_by(enabled=True).order_by(FeaturedTarget.sort_order).all():
        entry = {"designation": target.designation, "name": target.name, "track_name": target.name,
                 "kind": target.kind, "preset": target.preset, "size_arcmin": target.size_arcmin,
                 "blurb": target.blurb, "wiki": target.wiki}
        key = target.designation.lower()
        if key != "moon" and key not in ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune"):
            resolved = resolve_target(target.designation)
            if resolved is None:
                continue
            entry.update(ra=round(resolved["ra"], 5), dec=round(resolved["dec"], 5))
        targets.append(entry)
    catalog = star_catalog.get_catalog()
    rows = []
    # Every named object and every Messier object (the catalogue folds the
    # Messier numbers into the name records as "messier": "M31")
    for designation, record in (catalog.name_records or {}).items():
        messier = record.get("messier")
        if not (record.get("name") or messier):
            continue
        entry = star_catalog.lookup(designation)
        if entry is None:
            continue
        aliases = {a for a in record.get("aliases", ()) if a}
        if messier:
            aliases.add(messier)
        label = record.get("name") or messier or designation
        rows.append([designation, label, round(entry["ra"], 5), round(entry["dec"], 5),
                     None if entry["mag"] is None else round(entry["mag"], 2), sorted(aliases)])
    return {"featured_targets": targets, "objects": rows, "built": datetime.utcnow().isoformat() + "Z"}


def credentials_for(row):
    record = record_for(row)
    ready = bool(record.cert_pem and record.key_pem and record.cert_expires and record.cert_expires > datetime.utcnow())
    if not ready:
        wake_job()
    from app import cloudflare_dns
    return {
        "telescope_id": row.telescope_id,
        "host": host_for(record),
        "address": hotspot_address(),
        "origins": app_origins(),
        "secret": record.secret,
        "cert": record.cert_pem if ready else None,
        "key": record.key_pem if ready else None,
        "expires": record.cert_expires.isoformat() + "Z" if record.cert_expires else None,
        "certificate": "ready" if ready else ("pending" if cloudflare_dns.configured() else "not set up on the server"),
        "api_port": API_PORT,
        "liveview_port": LIVEVIEW_PORT,
        "pack": offline_pack(),
    }


# --- names and certificates ---------------------------------------------------------------

def _account_key():
    """The ACME account key, kept in instance/ (owner-only)."""
    from app import acme

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instance", "acme_account.pem")
    if os.path.exists(path):
        with open(path) as f:
            return acme.load_key(f.read())
    key = acme.new_key()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(acme.key_pem(key))
    return key


def ensure(record, dns=None, client=None):
    """Publish the name and get (or renew) the certificate for one telescope."""
    from app import acme
    from app.db import db

    host = host_for(record)
    if not record.dns_published:
        dns.set_a(host, hotspot_address())
        record.dns_published = True
        db.session.commit()
    due = record.cert_expires is None or record.cert_expires < datetime.utcnow() + timedelta(days=RENEW_DAYS)
    if not due:
        return False
    client = client or acme.Client(_account_key())
    chain, key, not_after = client.issue(host, dns)
    record.cert_pem, record.key_pem, record.cert_expires, record.last_error = chain, key, not_after, None
    db.session.commit()
    logger.info("Offline certificate for %s until %s", host, not_after)
    return True


def run_job(app):
    """Every telescope's name and certificate, as needed. Telescopes that haven't
    been seen for 60 days aren't renewed (they get a new one when they ask)."""
    from app import cloudflare_dns
    from app.db import db
    from models.tables import Telescope
    from models.telescope_offline import TelescopeOffline

    if not cloudflare_dns.configured():
        return 0
    done = 0
    with _job_lock, app.app_context():
        dns = cloudflare_dns.Cloudflare()
        for record in TelescopeOffline.query.all():
            row = db.session.get(Telescope, record.telescope_row_id)
            if row is None or row.is_disabled:
                continue
            if record.cert_pem and row.last_seen and time.time() - row.last_seen > 60 * 86400:
                continue
            try:
                done += bool(ensure(record, dns))
            except Exception as exc:
                logger.warning("Offline name/certificate for %s: %s", row.telescope_id, exc)
                record.last_error = str(exc)[:500]
                db.session.commit()
    return done


def wake_job():
    _wake.set()


def start_job(app):
    def loop():
        while True:
            try:
                run_job(app)
            except Exception:
                logger.exception("Offline certificate job failed")
            _wake.wait(JOB_INTERVAL_S)
            _wake.clear()
    threading.Thread(target=loop, name="offline-certificates", daemon=True).start()
