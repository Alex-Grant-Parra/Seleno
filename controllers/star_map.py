import logging
from flask import Blueprint, Response, jsonify, render_template, request, session
from flask_babel import gettext as _
from app.i18n import cache_by_language, request_language
from datetime import datetime, timezone
from typing import Optional
from models import star_catalog

from astrophysics.planetary_model import getAllCelestialData
from astrophysics.V1_Keplarian.convert import convert
from app.telescopeLink import Telescope, result_error
from app.telescope_access import telescope_access_error

logger = logging.getLogger(__name__)

star_map_bp = Blueprint("star_map", __name__)

# Bodies the Pi locates itself with trackObject, from its own site: the Moon's
# parallax alone shifts it by up to about 1 degree
_TRACK_BY_NAME = {"moon", "mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune", "pluto"}


def _require_telescope_control_access(telescope_id):
    # Owner, approved booking covering now, or admin; None when allowed
    return telescope_access_error(telescope_id)

_CELESTIAL_DEFAULT_VMAGS = {
    "sun": -26.74,
    "moon": -12.70,
    "pluto": 14.0,
}


def _celestial_magnitude(obj_name, coords, default=30):
    value = coords.get("vmag")
    if value is not None:
        try:
            return float(value)
        except Exception:
            pass
    return _CELESTIAL_DEFAULT_VMAGS.get(obj_name.lower(), default)


def _add_celestial_phase_fields(payload, coords):
    phase_name = coords.get("phase_name")
    if phase_name:
        payload["phase_name"] = phase_name

    phase_angle = coords.get("phase_angle_deg")
    if phase_angle is not None:
        try:
            payload["phase_angle_deg"] = float(phase_angle)
        except Exception:
            pass

    moon_illum = coords.get("moon_illumination_fraction")
    if moon_illum is not None:
        try:
            payload["moon_illumination_fraction"] = float(moon_illum)
        except Exception:
            pass

    moon_elong = coords.get("moon_elongation_deg")
    if moon_elong is not None:
        try:
            payload["moon_elongation_deg"] = float(moon_elong)
        except Exception:
            pass

    return payload

def loadStarsFromTables(tables=None):
    # Served from the cached in-memory catalogue; `tables` is accepted for
    # backwards compatibility but the catalogue always covers all three.
    catalog = star_catalog.get_catalog()
    return star_catalog.to_json_rows(catalog, 0, catalog.known_count, include_unknown=True)


def get_all_celestial_objects(_dt: Optional[datetime] = None):

    all_objects = loadStarsFromTables()

    # Get celestial objects positions for current UTC date/time
    if _dt is None:
        _dt = datetime.utcnow()
    celestial_data = getAllCelestialData(_dt.year, _dt.month, _dt.day, _dt.hour, _dt.minute, _dt.second)

    for obj_name, coords in celestial_data.items():
        ra_h, ra_m, ra_s = coords["ra"]
        dec_d, dec_m, dec_s = coords["dec"]
        mag = _celestial_magnitude(obj_name, coords)

        ra_deg = convert.HrMinSecToDegrees(ra_h, ra_m, ra_s) * 15
        dec_deg = convert.HrMinSecToDegrees(dec_d, dec_m, dec_s)

        obj_payload = {
            "name": obj_name.capitalize(),
            "ra": ra_deg,
            "dec": dec_deg,
            "mag": mag,
            "icon": f"/static/icons/planets/{obj_name.lower()}.png",
            "type": "planet"
        }
        all_objects.append(_add_celestial_phase_fields(obj_payload, coords))

    return all_objects


@star_map_bp.route("/api/stars")
def get_stars():
    # Parameters and defaults
    dt_str = request.args.get("datetime")
    min_mag = request.args.get("minMag", type=float)
    max_mag = request.args.get("maxMag", type=float)
    mag_limit = request.args.get("mag", type=float)  # backward compatibility
    limit = request.args.get("limit", type=int)
    include_planets = request.args.get("include_planets", default="false").lower() in ("1", "true", "yes")
    include_unknown = request.args.get("unknown", default="false").lower() in ("1", "true", "yes")

    if min_mag is None:
        # Include negative magnitudes for very bright stars (e.g., Sirius ~ -1.46)
        min_mag = -2.0
    if max_mag is None:
        max_mag = 20.0
    if mag_limit is not None and mag_limit < max_mag:
        max_mag = mag_limit

    _dt = None
    if dt_str:
        try:
            _dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if _dt.tzinfo is not None:
                _dt = _dt.astimezone(timezone.utc)
        except Exception:
            _dt = None

    planets = []
    if include_planets:
        if _dt is None:
            _dt = datetime.utcnow()
        celestial_data = getAllCelestialData(_dt.year, _dt.month, _dt.day, _dt.hour, _dt.minute, _dt.second)
        for obj_name, coords in celestial_data.items():
            ra_h, ra_m, ra_s = coords["ra"]
            dec_d, dec_m, dec_s = coords["dec"]
            mag = _celestial_magnitude(obj_name, coords)

            ra_deg = convert.HrMinSecToDegrees(ra_h, ra_m, ra_s) * 15
            dec_deg = convert.HrMinSecToDegrees(dec_d, dec_m, dec_s)

            obj_payload = {
                "name": obj_name.capitalize(),
                "ra": ra_deg,
                "dec": dec_deg,
                "mag": mag,
                "icon": f"/static/icons/planets/{obj_name.lower()}.png",
                "type": "planet"
            }
            planets.append(_add_celestial_phase_fields(obj_payload, coords))

    # Stars come from the cached, magnitude-sorted catalogue: a band is a
    # contiguous slice, so this costs a slice + serialise instead of a scan.
    catalog = star_catalog.get_catalog()
    lo, hi = catalog.band_range(min_mag, max_mag)
    if limit is not None and limit > 0 and (hi - lo) > limit:
        hi = lo + limit  # the catalogue is sorted, so this keeps the brightest
    all_stars = star_catalog.to_json_rows(
        catalog, lo, hi,
        include_unknown=include_unknown,
        limit=limit if (limit is not None and limit > 0) else None,
    )

    if include_planets:
        return jsonify(all_stars + planets)
    else:
        return jsonify(all_stars)


def _binary_response(payload, compressed, etag, extra_headers=None):
    resp = Response(payload, mimetype="application/octet-stream")
    if compressed:
        resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["ETag"] = etag
    # The catalogue is static; let browsers reuse it across visits
    resp.headers["Cache-Control"] = "public, max-age=604800"
    for key, value in (extra_headers or {}).items():
        resp.headers[key] = value
    return resp


@star_map_bp.route("/api/stars_bin")
def get_stars_binary():
    """Compact binary form of a magnitude band, for the star map front-end.

    Layout: 20-byte header, then float32 ra[], dec[], mag[] (NaN = unknown
    magnitude), then the newline-separated names. Roughly a fifth the size of
    the equivalent JSON and it parses straight into typed arrays.
    """
    start = request.args.get("start", type=int)
    end = request.args.get("end", type=int)
    min_mag = request.args.get("minMag", type=float)
    max_mag = request.args.get("maxMag", type=float)
    limit = request.args.get("limit", type=int)
    include_unknown = request.args.get("unknown", default="false").lower() in ("1", "true", "yes")
    include_names = request.args.get("names", default="true").lower() in ("1", "true", "yes")

    catalog = star_catalog.get_catalog()
    version = catalog.version
    etag = f'W/"{version}-{request.query_string.decode("ascii", "ignore")}"'

    # Nothing changed since the browser last asked? Let it reuse its copy.
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag,
                                             "Cache-Control": "public, max-age=604800"})

    if start is not None or end is not None:
        # Index slice, as handed out by /api/stars_bands. Bands addressed this
        # way are exactly disjoint, so the client never receives a star twice.
        lo = max(0, start or 0)
        hi = min(catalog.count, end if end is not None else catalog.count)
        hi = max(lo, hi)
        include_unknown = False  # unknown objects are addressable as their own slice
    else:
        lo, hi = catalog.band_range(
            -30.0 if min_mag is None else min_mag,
            30.0 if max_mag is None else max_mag,
        )
    if limit is not None and limit > 0 and (hi - lo) > limit:
        hi = lo + limit

    accepts_gzip = "gzip" in (request.headers.get("Accept-Encoding") or "")
    key = (version, lo, hi, include_unknown, include_names)
    payload = star_catalog.cached_payload(
        key,
        lambda: star_catalog.encode_binary(catalog, lo, hi,
                                           include_unknown=include_unknown,
                                           include_names=include_names),
        compress=accepts_gzip,
    )
    return _binary_response(payload, accepts_gzip, etag, {
        "X-Star-Count": str((hi - lo) + (catalog.unknown_count if include_unknown else 0)),
    })


@star_map_bp.route("/api/stars_bands")
def get_star_bands():
    """Index ranges the client should load, brightest first.

    The catalogue is sorted by magnitude, so each band is a contiguous index
    slice. The client fetches them in order: the sky is usable after the first
    one and the rest stream in without re-sending anything already held. The
    objects with no recorded magnitude form the final band.
    """
    catalog = star_catalog.get_catalog()
    edges = [4.0, 6.0, 7.5, 9.0, 10.0, 11.0]
    bands = []
    cursor = 0
    for edge in edges:
        _lo, hi = catalog.band_range(catalog.min_mag, edge)
        if hi <= cursor:
            continue
        bands.append({"start": cursor, "end": hi, "count": hi - cursor,
                      "maxMag": edge, "kind": "known"})
        cursor = hi
    if cursor < catalog.known_count:
        bands.append({"start": cursor, "end": catalog.known_count,
                      "count": catalog.known_count - cursor,
                      "maxMag": catalog.max_mag, "kind": "known"})
    if catalog.unknown_count:
        bands.append({"start": catalog.known_count, "end": catalog.count,
                      "count": catalog.unknown_count,
                      "maxMag": None, "kind": "unknown"})
    return jsonify({
        "version": catalog.version,
        "total": catalog.count,
        "known": catalog.known_count,
        "unknown": catalog.unknown_count,
        "minMag": catalog.min_mag,
        "maxMag": catalog.max_mag,
        "bands": bands,
    })


@star_map_bp.route("/api/constellations")
def get_constellations():
    """Constellation figures for the star map, from ConstellationsTable and
    ConstellationLinesTable. Each line is returned as the coordinates of the two
    catalogue stars it joins."""
    figures = star_catalog.get_constellations(request_language())
    resp = jsonify({"count": len(figures), "constellations": figures})
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return cache_by_language(resp)


@star_map_bp.route("/api/star_names")
def get_star_names():
    """Names for the star map's labels and click-through panel.

    Returns one entry per named catalogue object: its designation, displayed
    name, Bayer designation and the magnitude the map draws it at, so the
    client can label the brightest ones without another lookup.
    """
    catalog = star_catalog.get_catalog()
    lang = request_language()
    entries = []
    for designation, record in star_catalog.get_star_names().items():
        proper = star_catalog.display_name(designation, lang)
        if not proper:
            continue
        i = catalog.find(designation)
        if i is None:
            continue
        mag = catalog.mag[i]
        entries.append({
            "id": designation,
            "name": proper,
            "bayer": record.get("bayer", ""),
            "mag": None if mag != mag else round(mag, 2),  # NaN check
        })
    entries.sort(key=lambda e: (e["mag"] is None, e["mag"] if e["mag"] is not None else 0))
    resp = jsonify({"count": len(entries), "stars": entries})
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return cache_by_language(resp)


@star_map_bp.route("/api/search_suggest")
def search_suggest():
    """Up to 8 objects for the search box's suggestions as the visitor types:
    names in any language (forgiving small typos) or an exact catalogue
    number. Each has its kind, key, displayed name (in ?lang=) and the name
    the query matched."""
    query = (request.args.get("q") or "").strip()[:120]
    if len(query) < 2:
        return jsonify({"query": query, "matches": []})
    matches = star_catalog.search(query, request_language(), limit=8)
    resp = jsonify({"query": query, "matches": [
        {key: m[key] for key in ("kind", "key", "name", "matched", "match")} for m in matches]})
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return cache_by_language(resp)


@star_map_bp.route("/api/stars_meta")
def get_stars_meta():
    # Magnitude extremes across the star tables, from the cached catalogue.
    # Objects with no recorded magnitude are reported separately rather than
    # being folded in at a made-up value.
    catalog = star_catalog.get_catalog()
    return jsonify({
        "minMag": catalog.min_mag,
        "maxMag": catalog.max_mag,
        "count": catalog.count,
        "knownCount": catalog.known_count,
        "unknownCount": catalog.unknown_count,
    })

_geomag_model = None


def _get_geomag_model():
    # WMM2025 (valid 2025-2030), parsed once on first use
    global _geomag_model
    if _geomag_model is None:
        from pygeomag import GeoMag
        _geomag_model = GeoMag(coefficients_file="wmm/WMM_2025.COF")
    return _geomag_model


@star_map_bp.route("/api/magnetic_declination")
def get_magnetic_declination():
    """Magnetic declination at a location, from the World Magnetic Model.

    Phone compasses point at magnetic north; the star map's phone-orientation
    mode adds this (degrees, east positive) to turn their headings into true
    ones. Returns 400 unless lat and lon are given and in range.
    """
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    # Written as "not in range" so NaN is rejected too
    if lat is None or lon is None or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "lat and lon are required"}), 400

    from pygeomag import decimal_year_from_date
    year = decimal_year_from_date(datetime.now(timezone.utc))
    result = _get_geomag_model().calculate(
        glat=lat, glon=lon, alt=0, time=year, allow_date_outside_lifespan=True)
    resp = jsonify({"declination": round(result.d, 3), "model": "WMM2025"})
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@star_map_bp.route("/api/planets")
def get_planets():
    # Returns only planets, sun, and moon; accepts optional datetime param
    dt_str = request.args.get("datetime")
    _dt = None
    if dt_str:
        try:
            _dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            if _dt.tzinfo is not None:
                _dt = _dt.astimezone(timezone.utc)
        except Exception:
            _dt = None
    if _dt is None:
        _dt = datetime.utcnow()

    celestial_data = getAllCelestialData(_dt.year, _dt.month, _dt.day, _dt.hour, _dt.minute, _dt.second)
    lang = request_language()
    planets = []
    for obj_name, coords in celestial_data.items():
        ra_h, ra_m, ra_s = coords["ra"]
        dec_d, dec_m, dec_s = coords["dec"]
        mag = _celestial_magnitude(obj_name, coords)

        ra_deg = convert.HrMinSecToDegrees(ra_h, ra_m, ra_s) * 15
        dec_deg = convert.HrMinSecToDegrees(dec_d, dec_m, dec_s)

        obj_payload = {
            "name": obj_name.capitalize(),  # the ID: tracking and /star_info use it
            "label": star_catalog.body_name(obj_name, lang) or obj_name.capitalize(),
            "ra": ra_deg,
            "dec": dec_deg,
            "mag": mag,
            "icon": f"/static/icons/planets/{obj_name.lower()}.png",
            "type": "planet"
        }
        planets.append(_add_celestial_phase_fields(obj_payload, coords))
    return jsonify(planets)

@star_map_bp.route("/StarMap")
def star_map():
    # The client will fetch stars and planets via APIs progressively
    selected_telescope = session.get('selected_telescope')
    return render_template("star_map.html", stars=[], selected_telescope=selected_telescope)

@star_map_bp.route("/star_info/<star_name>")
def star_info(star_name):
    _now = datetime.utcnow()
    celestial_data = getAllCelestialData(_now.year, _now.month, _now.day, _now.hour, _now.minute, _now.second)
    obj_name_lower = star_name.lower()
    if obj_name_lower in celestial_data:
        coords = celestial_data[obj_name_lower]
        ra_h, ra_m, ra_s = coords["ra"]
        dec_d, dec_m, dec_s = coords["dec"]
        mag = _celestial_magnitude(obj_name_lower, coords)

        ra_deg = convert.HrMinSecToDegrees(ra_h, ra_m, ra_s) * 15
        dec_deg = convert.HrMinSecToDegrees(dec_d, dec_m, dec_s)

        obj_payload = {
            "name": star_name.capitalize(),
            "label": star_catalog.body_name(obj_name_lower, request_language()) or star_name.capitalize(),
            "ra": ra_deg,
            "dec": dec_deg,
            "mag": mag,
            "type": "planet"
        }
        return jsonify(_add_celestial_phase_fields(obj_payload, coords))

    # Served from the in-memory catalogue rather than the three tables
    entry = star_catalog.lookup(star_name)
    if entry is not None:
        mag = entry["mag"]  # None when the catalogue has no magnitude
        record = entry["record"]

        response_data = {
            "name": entry["name"],
            "ra": entry["ra"],
            "dec": entry["dec"],
            "mag": mag,
            "type": "star"
        }
        if mag is None:
            response_data["magUnknown"] = True
        if record.get("magSource"):
            # V-Mag was filled in by scripts/import_star_names.py
            response_data["magSource"] = record["magSource"]
        if record.get("bayer"):
            response_data["bayer"] = record["bayer"]
        if record.get("var"):
            response_data["variableId"] = record["var"]
        friendly = star_catalog.display_name(entry["name"], request_language())
        if friendly:
            response_data['friendlyName'] = friendly
        return jsonify(response_data)

    return jsonify({"error": "Star not found"}), 404

@star_map_bp.route("/track_star", methods=["POST"])
def track_star():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({
            "status": "error",
            "error": "Must be logged in",
            "message": _("Must be logged in to control telescope")
        }), 401

    data = request.get_json()
    ra = data.get("ra")
    dec = data.get("dec")
    name = data.get("name")
    mag = data.get("mag")

    if ra is None or dec is None:
        print("Missing RA/DEC in request")
        return jsonify({"error": "Missing RA/DEC"}), 400

    # Check if a telescope is selected
    selected_telescope = session.get('selected_telescope')
    telescope_id = selected_telescope.get('telescope_id') if selected_telescope else None
    
    if not telescope_id:
        # No telescope selected - client should redirect to interface
        print(f"[TRACKING] No telescope selected for {name}")
        return jsonify({
            "status": "error",
            "error": "No telescope selected",
            "redirect": True,
            "message": _("Please select a telescope in the Interface page to begin tracking")
        }), 422

    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    try:
        # Create telescope instance and send coordinates
        t = Telescope(telescope_id)
        if str(name or "").strip().lower() in _TRACK_BY_NAME:
            print(f"\n[TRACKING] Sending {name} to telescope {telescope_id} by name\n", flush=True)
            result = t.scope.track_object(str(name).strip())
        else:
            print(f"\n[TRACKING] Sending {name} coordinates to telescope {telescope_id}")
            print(f"[TRACKING] RA: {ra}°, DEC: {dec}°, Mag: {mag}\n", flush=True)

            # Send track command with coordinates to the telescope
            result = t.send_command("trackCoordinates", kwargs={
                "name": name,
                "ra": ra,
                "dec": dec,
                "mag": mag
            })

        # A go-to below the horizon, near the Sun, while parked... is refused as a result
        refusal = result_error(result)
        if refusal:
            print(f"[TRACKING] Telescope refused {name}: {refusal}", flush=True)
            return jsonify({
                "status": "error",
                "error": refusal,
                "message": refusal,
                "telescope_id": telescope_id,
                "result": result
            })

        # Store in sesh
        session["selectedObject"] = {
            "name": name,
            "ra": ra,
            "dec": dec,
            "mag": mag
        }
        
        return jsonify({
            "status": "tracking",
            "ra": ra,
            "dec": dec,
            "telescope_id": telescope_id,
            "result": result,
            "redirect": True
        })
        
    except Exception:
        logger.exception("[TRACKING ERROR] Failed to send coordinates")
        return jsonify({
            "status": "error",
            "error": "Tracking command failed",
            "message": _("Failed to send tracking command")
        }), 500

@star_map_bp.route("/get_tracking_status", methods=["GET"])
def get_tracking_status():
    # Get the current tracking status from the session
    selected_object = session.get("selectedObject")
    
    if selected_object:
        return jsonify({
            "status": "success",
            "tracking": True,
            "object": {
                "name": selected_object.get("name"),
                "ra": selected_object.get("ra"),
                "dec": selected_object.get("dec"),
                "mag": selected_object.get("mag")
            }
        })
    else:
        return jsonify({
            "status": "success",
            "tracking": False,
            "object": None
        })

@star_map_bp.route("/stop_tracking", methods=["POST"])
def stop_tracking():
    # Stop tracking the current object
    # Check if a telescope is selected
    selected_telescope = session.get('selected_telescope')
    telescope_id = selected_telescope.get('telescope_id') if selected_telescope else None
    
    if not telescope_id:
        print(f"[TRACKING] No telescope selected for stop command")
        return jsonify({
            "status": "error",
            "error": "No telescope selected",
            "message": _("No telescope selected")
        }), 422

    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    try:
        # Create telescope instance and send stop tracking command
        t = Telescope(telescope_id)
        print(f"\n[TRACKING] Stopping tracking on telescope {telescope_id}\n", flush=True)

        # Send stop tracking command to the telescope
        result = t.send_command("stopTracking")

        # Clear session tracking data
        session.pop("selectedObject", None)

        refusal = result_error(result)
        if refusal:
            return jsonify({
                "status": "error",
                "error": refusal,
                "message": refusal,
                "telescope_id": telescope_id,
                "result": result
            })

        print(f"[TRACKING] Tracking stopped successfully")
        
        return jsonify({
            "status": "stopped",
            "message": _("Tracking stopped successfully"),
            "telescope_id": telescope_id,
            "result": result
        })
        
    except Exception:
        logger.exception("[TRACKING ERROR] Failed to stop tracking")
        # Still clear session even if command failed
        session.pop("selectedObject", None)
        return jsonify({
            "status": "error",
            "error": "Stop tracking command failed",
            "message": _("Failed to stop tracking")
        }), 500

@star_map_bp.route("/api/telescope_position", methods=["GET"])
def get_telescope_position():
    # Get the current position (RA/DEC) of the telescope
    # Check if a telescope is selected
    selected_telescope = session.get('selected_telescope')
    telescope_id = selected_telescope.get('telescope_id') if selected_telescope else None
    
    if not telescope_id:
        return jsonify({
            "status": "error",
            "error": "No telescope selected",
            "message": _("No telescope selected")
        }), 422

    guard = _require_telescope_control_access(telescope_id)
    if guard:
        return guard

    try:
        # Create telescope instance and get current coordinates
        t = Telescope(telescope_id)
        print(f"[TELESCOPE] Getting coordinates for {telescope_id}", flush=True)
        coords = t.motor.get_current_coordinates()
        
        print(f"[TELESCOPE] Got response: {coords}", flush=True)
        
        # Extract RA and DEC from various possible response formats
        ra = None
        dec = None
        
        if coords and isinstance(coords, dict):
            if "result" in coords:
                result = coords["result"]
                if isinstance(result, dict):
                    if "current_right_ascension" in result:
                        ra = result.get("current_right_ascension")
                    elif "ra" in result:
                        ra = result.get("ra")

                    if "current_declination" in result:
                        dec = result.get("current_declination")
                    elif "dec" in result:
                        dec = result.get("dec")
            
            # Fall back to top-level keys
            if ra is None or dec is None:
                if ra is None:
                    if "current_right_ascension" in coords:
                        ra = coords.get("current_right_ascension")
                    elif "ra" in coords:
                        ra = coords.get("ra")

                if dec is None:
                    if "current_declination" in coords:
                        dec = coords.get("current_declination")
                    elif "dec" in coords:
                        dec = coords.get("dec")
        
        print(f"[TELESCOPE] Extracted RA: {ra}, DEC: {dec}", flush=True)
        
        if ra is not None and dec is not None:
            try:
                ra_float = float(ra)
                dec_float = float(dec)
                print(f"[TELESCOPE] Success! RA: {ra_float}°, DEC: {dec_float}°", flush=True)
                return jsonify({
                    "status": "success",
                    "ra": ra_float,
                    "dec": dec_float,
                    "telescope_id": telescope_id
                })
            except (ValueError, TypeError):
                return jsonify({
                    "status": "error",
                    "error": "Failed to parse coordinates as numbers",
                    "message": _("Telescope returned non-numeric coordinates"),
                    "telescope_id": telescope_id
                }), 500
        else:
            return jsonify({
                "status": "error",
                "error": "No coordinates in response",
                "message": f"Response structure: {coords}",
                "telescope_id": telescope_id
            }), 500
            
    except Exception:
        logger.exception("[TELESCOPE POSITION ERROR] Failed to get coordinates")
        return jsonify({
            "status": "error",
            "error": "Failed to get telescope position",
            "message": _("Failed to get telescope position")
        }), 500

@star_map_bp.route("/api/debug/session", methods=["GET"])
def debug_session():
    # Debug endpoint to check session state
    selected_telescope = session.get('selected_telescope')
    return jsonify({
        "status": "success",
        "selected_telescope": selected_telescope,
        "session_keys": list(session.keys())
    })
