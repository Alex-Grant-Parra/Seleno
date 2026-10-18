"""Data for the redesigned interface page: the sky at the telescope's site tonight,
targets worth photographing now, and the newest frames a telescope sent back."""
import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app import sky
from app.telescope_access import get_telescope_row, requested_telescope_id, telescope_access_error

logger = logging.getLogger(__name__)

interface_api_bp = Blueprint("interface_api", __name__, url_prefix="/interface/api")

PLANETS = ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")
# A site to two decimals (about a kilometre) is plenty for the sky and keeps a
# telescope's exact location to its owner
SITE_DECIMALS = 2
# A featured target is good tonight when it spends this long above 30 degrees in the dark
GOOD_MINUTES = 60


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def telescope_site(row):
    """(lat, lon, source) of a telescope: set on the telescope, reported in its status,
    or recorded with its latest upload. None when nothing says."""
    from app.WebsocketServer import get_latest_status
    from models.photo import Photo

    if row.latitude is not None and row.longitude is not None:
        return row.latitude, row.longitude, "telescope"
    _, status = get_latest_status(row.telescope_id)
    site = (status or {}).get("site") if isinstance(status, dict) else None
    if isinstance(site, dict):
        lat, lon = _float(site.get("lat", site.get("latitude"))), _float(site.get("lon", site.get("longitude")))
        if lat is not None and lon is not None:
            return lat, lon, "status"
    photo = (Photo.query.filter(Photo.telescope_id == row.id, Photo.site_lat.isnot(None), Photo.site_lon.isnot(None))
             .order_by(Photo.uploaded_at.desc()).first())
    if photo is not None:
        return photo.site_lat, photo.site_lon, "upload"
    return None


def _guarded_telescope():
    telescope_id = requested_telescope_id({})
    guard = telescope_access_error(telescope_id)
    if guard:
        return None, guard
    return get_telescope_row(telescope_id), None


def _no_site():
    return jsonify({"status": "success", "site": None,
                    "message": "This telescope's location isn't known yet. Its owner can set it on the telescope's settings page."})


def planet_positions(when):
    """{name: (ra, dec)} for the planets now, from the site's planetary model."""
    from astrophysics.planetary_model import getAllCelestialData
    from astrophysics.V1_Keplarian.convert import convert

    data = getAllCelestialData(when.year, when.month, when.day, when.hour, when.minute, when.second)
    out = {}
    for name in PLANETS:
        entry = data.get(name)
        if not entry:
            continue
        ra = convert.HrMinSecToDegrees(*entry["ra"]) * 15.0
        dec = convert.HrMinSecToDegrees(*entry["dec"])
        out[name] = (ra % 360.0, dec)
    return out


def resolve_target(name, ra=None, dec=None, when=None):
    """{name, ra, dec} for a designation, a planet, the Moon, or given coordinates."""
    from models import star_catalog

    when = when or _utcnow()
    if ra is not None and dec is not None:
        return {"name": name or "Target", "ra": ra % 360.0, "dec": max(-90.0, min(90.0, dec))}
    if not name:
        return None
    key = name.strip().lower()
    if key == "moon":
        moon_ra, moon_dec, _, _ = sky.moon_position(sky.julian_day(when))
        return {"name": "Moon", "ra": moon_ra, "dec": moon_dec, "moving": True}
    if key in PLANETS:
        planets = planet_positions(when)
        if key in planets:
            p_ra, p_dec = planets[key]
            return {"name": key.capitalize(), "ra": p_ra, "dec": p_dec, "moving": True}
        return None
    entry = star_catalog.lookup(name)
    if entry is None:
        designation = star_catalog.find_by_proper_name(name)
        entry = star_catalog.lookup(designation) if designation else None
    if entry is None:
        return None
    return {"name": entry["name"], "ra": float(entry["ra"]), "dec": float(entry["dec"])}


@interface_api_bp.route("/sky")
def sky_now():
    """The sky at the selected telescope's site: Sun, twilight, the Moon, and (with
    ?target=, or ?ra=&dec=) a target's altitude, transit and hours above 30 degrees."""
    row, guard = _guarded_telescope()
    if guard:
        return guard
    site = telescope_site(row)
    if site is None:
        return _no_site()
    lat, lon, source = site
    when = _utcnow()
    target = None
    name = (request.args.get("target") or "").strip()[:120]
    ra, dec = _float(request.args.get("ra")), _float(request.args.get("dec"))
    if name or (ra is not None and dec is not None):
        label = (request.args.get("label") or "").strip()[:120]
        target = resolve_target(name or label, ra, dec, when)
        if target is None:
            return jsonify({"status": "error", "message": f"Couldn't find {name}"}), 404
    min_alt = _float(request.args.get("min_alt"))
    min_alt = 30.0 if min_alt is None else max(0.0, min(80.0, min_alt))
    summary = sky.sky_summary(lat, lon, when, target=target, min_alt=min_alt)
    summary["site"] = {"lat": round(lat, SITE_DECIMALS), "lon": round(lon, SITE_DECIMALS), "source": source}
    return jsonify({"status": "success", **summary})


@interface_api_bp.route("/targets")
def targets_now():
    """Basic mode's suggestions: the featured targets with how they fare tonight,
    the best placed first."""
    from models.album import FeaturedTarget

    row, guard = _guarded_telescope()
    if guard:
        return guard
    site = telescope_site(row)
    when = _utcnow()
    rows = FeaturedTarget.query.filter_by(enabled=True).order_by(FeaturedTarget.sort_order).all()
    planets = {}
    try:
        planets = planet_positions(when)
    except Exception:
        logger.exception("Planet positions failed")
    night = moon = None
    jd = sky.julian_day(when)
    moon_ra, moon_dec, _, _ = sky.moon_position(jd)
    if site is not None:
        lat, lon, _ = site
        night = sky.night_window(lat, lon, when)
        fraction, _, phase = sky.moon_phase(jd)
        moon = {"alt": round(sky.moon_altitude(lat, lon, jd), 1), "illumination": round(fraction, 3), "phase": phase}
    out = []
    for target in rows:
        key = target.designation.lower()
        if key == "moon":
            coords = (moon_ra, moon_dec)
        elif key in planets:
            coords = planets[key]
        else:
            resolved = resolve_target(target.designation)
            coords = (resolved["ra"], resolved["dec"]) if resolved else None
        if coords is None:
            continue
        entry = {
            "designation": target.designation,
            "name": target.name,
            "kind": target.kind,
            "preset": target.preset,
            "size_arcmin": target.size_arcmin,
            "blurb": target.blurb,
            "wiki": target.wiki,
            "ra": round(coords[0], 4),
            "dec": round(coords[1], 4),
            "moving": key == "moon" or key in planets,
        }
        if site is not None:
            info = sky.target_tonight(coords[0], coords[1], lat, lon, when, night=night)
            info["moon_separation"] = round(sky.separation_deg(coords[0], coords[1], moon_ra, moon_dec), 1)
            entry.update({
                "alt": info["alt"],
                "rising": info["rising"],
                "best_alt": info["best_alt"],
                "best_time": _iso(info["best_time"]),
                "transit": _iso(info["transit"]),
                "minutes_above": info["minutes_above"],
                "moon_separation": info["moon_separation"],
                "never_rises": bool(info["never_rises"]),
                "good_tonight": bool(info["minutes_above"] >= GOOD_MINUTES),
                "verdict": sky.verdict(info, {"moon": moon, "sun": {"alt": sky.sun_altitude(lat, lon, jd)}}),
            })
        out.append(entry)
    if site is not None:
        # Good tonight first; within each, up now and high, then what rises later, then the rest
        def score(t):
            up_now = t["alt"] >= 20
            later = (t["best_alt"] or -90) >= 25
            return (not t["good_tonight"], 0 if up_now else 1 if later else 2, -(t["minutes_above"] or 0), -t["alt"])
        out.sort(key=score)
    return jsonify({
        "status": "success",
        "site": None if site is None else {"lat": round(site[0], SITE_DECIMALS), "lon": round(site[1], SITE_DECIMALS)},
        "night": None if night is None else {"start": _iso(night[0]), "end": _iso(night[1])},
        "moon": moon,
        "targets": out,
    })


@interface_api_bp.route("/latest_captures")
def latest_captures():
    """The newest frames from the selected telescope (?since= an ISO time, ?limit=),
    so the page can show a test shot as soon as it arrives."""
    from models.album import FrameMeta
    from models.photo import Photo

    row, guard = _guarded_telescope()
    if guard:
        return guard
    try:
        limit = max(1, min(50, int(request.args.get("limit", 12))))
    except ValueError:
        limit = 12
    query = Photo.query.filter(Photo.telescope_id == row.id, Photo.owner_id == int(current_user.get_id()),
                               Photo.kind != "raw")
    since = request.args.get("since")
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if since_dt.tzinfo is not None:
                since_dt = since_dt.astimezone(timezone.utc).replace(tzinfo=None)
            query = query.filter(Photo.uploaded_at > since_dt)
        except ValueError:
            return jsonify({"status": "error", "message": "since must be an ISO time"}), 400
    photos = query.order_by(Photo.uploaded_at.desc()).limit(limit).all()
    names = [p.filename for p in photos]
    meta = {}
    if names:
        meta = {m.filename: m for m in FrameMeta.query.filter(FrameMeta.owner_id == int(current_user.get_id()),
                                                              FrameMeta.filename.in_(names))}
    raws = {p.pair_id for p in Photo.query.filter(Photo.telescope_id == row.id, Photo.kind == "raw",
                                                  Photo.pair_id.in_([p.pair_id for p in photos if p.pair_id]))} if photos else set()
    frames = []
    for photo in photos:
        m = meta.get(photo.filename)
        frames.append({
            "name": photo.filename,
            "kind": photo.kind,
            "frame_type": photo.frame_type,
            "label": photo.label,
            "target": photo.target,
            "captured_at": _iso(photo.captured_at),
            "uploaded_at": _iso(photo.uploaded_at),
            "exposure_s": photo.exposure_s,
            "iso": photo.iso,
            "plan_id": photo.plan_id,
            "has_raw": bool(photo.pair_id and photo.pair_id in raws),
            "url": f"/album/photo/{photo.filename}",
            "thumb": f"/album/thumb/{photo.filename}?size=sm",
            "preview": f"/album/thumb/{photo.filename}?size=md",
            "metrics": m.metrics if m is not None else None,
        })
    return jsonify({"status": "success", "frames": frames, "now": _iso(_utcnow())})
