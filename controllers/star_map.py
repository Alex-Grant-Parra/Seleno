from flask import Blueprint, Response, jsonify, render_template, request, session
from datetime import datetime, timezone
from typing import Optional
from models.tables import HDSTARtable, IndexTable, NGCtable
from app.db import db
from app import star_catalog
from sqlalchemy import func

from astrophysics.planetary_model import getAllCelestialData
from astrophysics.V1_Keplarian.convert import convert
from app.telescopeLink import Telescope

star_map_bp = Blueprint("star_map", __name__)


def _require_telescope_control_access():
    from flask_login import current_user
    if getattr(current_user, 'is_limited', False):
        return jsonify({"status": "error", "message": "Limited accounts cannot control telescopes."}), 403
    return None

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

    tables = [HDSTARtable, IndexTable, NGCtable]

    all_objects = loadStarsFromTables(tables)

    # Get celestial objects positions for current UTC date/time
    if _dt is None:
        _dt = datetime.utcnow()
    celestial_data = getAllCelestialData(_dt.year, _dt.month, _dt.day, _dt.hour, _dt.minute, _dt.second)

    for obj_name, coords in celestial_data.items():
        ra_h, ra_m, ra_s = coords["ra"]
        dec_d, dec_m, dec_s = coords["dec"]
        mag = _celestial_magnitude(obj_name, coords)

        ra_deg = convert.HrMinSecToDegrees(ra_h, ra_m, ra_s) * 15
        if dec_d < 0:
            dec_deg = dec_d - dec_m / 60 - dec_s / 3600
        else:
            dec_deg = dec_d + dec_m / 60 + dec_s / 3600

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


def _star_magnitude(star, default=None):
    """Magnitude of an ORM row, or `default` when it is unknown.

    Unknown means NULL, or one of the Henry Draper "not recorded" placeholders
    (20/30/40/50) that HDSTARTable stores in place of a real magnitude.
    """
    value = getattr(star, "V_Mag", None)
    if value is None:
        return default
    try:
        value = float(value)
    except Exception:
        return default
    is_hd = getattr(type(star), "__tablename__", "") == "HDSTARTable"
    if is_hd and value in star_catalog.HD_PLACEHOLDER_MAGS:
        return default
    return value

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
            if dec_d < 0:
                dec_deg = dec_d - dec_m / 60 - dec_s / 3600
            else:
                dec_deg = dec_d + dec_m / 60 + dec_s / 3600

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
        _, hi = catalog.band_range(catalog.min_mag, edge)
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
    planets = []
    for obj_name, coords in celestial_data.items():
        ra_h, ra_m, ra_s = coords["ra"]
        dec_d, dec_m, dec_s = coords["dec"]
        mag = _celestial_magnitude(obj_name, coords)

        ra_deg = convert.HrMinSecToDegrees(ra_h, ra_m, ra_s) * 15
        if dec_d < 0:
            dec_deg = dec_d - dec_m / 60 - dec_s / 3600
        else:
            dec_deg = dec_d + dec_m / 60 + dec_s / 3600

        obj_payload = {
            "name": obj_name.capitalize(),
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

def extract_friendly_common_name(common_names_field: str) -> str:
    # Extract the first friendly name from commonNames field; skip catalog designations
    if not common_names_field:
        return ''
    parts = [p.strip() for p in common_names_field.split(',')]
    for name in parts:
        name_upper = name.upper()
        # Skip catalog designations (HD, NGC, IC, M followed by number)
        if (name_upper.startswith('HD') or 
            name_upper.startswith('NGC') or 
            name_upper.startswith('IC') or 
            (name_upper.startswith('M') and len(name) > 1 and name[1:].strip().replace(' ', '').isdigit())):
            continue
        # Found a friendly name
        return name
    return ''

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
        if dec_d < 0:
            dec_deg = dec_d - dec_m / 60 - dec_s / 3600
        else:
            dec_deg = dec_d + dec_m / 60 + dec_s / 3600

        obj_payload = {
            "name": star_name.capitalize(),
            "ra": ra_deg,
            "dec": dec_deg,
            "mag": mag,
            "type": "planet"
        }
        return jsonify(_add_celestial_phase_fields(obj_payload, coords))

    tables = [HDSTARtable, IndexTable, NGCtable]

    for table in tables:
        result = table.query.filter_by(Name=star_name).first()
        if result:
            mag = _star_magnitude(result)  # None when the catalogue has no magnitude
            response_data = {
                "name": result.Name,
                "ra": float(result.RA) if result.RA is not None else 0,
                "dec": float(result.DEC) if result.DEC is not None else 0,
                "mag": mag,
                "type": "star"
            }
            if mag is None:
                response_data["magUnknown"] = True
            # Add friendly common name if available
            common_names_raw = getattr(result, 'commonNames', None) or getattr(result, 'Common_names', None)
            if common_names_raw:
                friendly_name = extract_friendly_common_name(common_names_raw)
                if friendly_name:
                    response_data['friendlyName'] = friendly_name
            return jsonify(response_data)

    return jsonify({"error": "Star not found"}), 404

@star_map_bp.route("/track_star", methods=["POST"])
def track_star():
    from flask_login import current_user
    if not current_user.is_authenticated:
        return jsonify({
            "status": "error",
            "error": "Must be logged in",
            "message": "Must be logged in to control telescope"
        }), 401

    guard = _require_telescope_control_access()
    if guard:
        return guard
    
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
            "message": "Please select a telescope in the Interface page to begin tracking"
        }), 422
    
    try:
        # Create telescope instance and send coordinates
        t = Telescope(telescope_id)
        print(f"\n[TRACKING] Sending {name} coordinates to telescope {telescope_id}")
        print(f"[TRACKING] RA: {ra}°, DEC: {dec}°, Mag: {mag}\n", flush=True)
        
        # Send track command with coordinates to the telescope
        result = t.send_command("trackCoordinates", kwargs={
            "name": name,
            "ra": ra,
            "dec": dec,
            "mag": mag
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
        
    except Exception as e:
        print(f"[TRACKING ERROR] Failed to send coordinates: {str(e)}")
        return jsonify({
            "status": "error",
            "error": str(e),
            "message": f"Failed to send tracking command: {str(e)}"
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
    guard = _require_telescope_control_access()
    if guard:
        return guard

    # Stop tracking the current object
    # Check if a telescope is selected
    selected_telescope = session.get('selected_telescope')
    telescope_id = selected_telescope.get('telescope_id') if selected_telescope else None
    
    if not telescope_id:
        print(f"[TRACKING] No telescope selected for stop command")
        return jsonify({
            "status": "error",
            "error": "No telescope selected",
            "message": "No telescope selected"
        }), 422
    
    try:
        # Create telescope instance and send stop tracking command
        t = Telescope(telescope_id)
        print(f"\n[TRACKING] Stopping tracking on telescope {telescope_id}\n", flush=True)
        
        # Send stop tracking command to the telescope
        result = t.send_command("stopTracking")
        
        # Clear session tracking data
        session.pop("selectedObject", None)
        
        print(f"[TRACKING] Tracking stopped successfully")
        
        return jsonify({
            "status": "stopped",
            "message": "Tracking stopped successfully",
            "telescope_id": telescope_id,
            "result": result
        })
        
    except Exception as e:
        print(f"[TRACKING ERROR] Failed to stop tracking: {str(e)}")
        # Still clear session even if command failed
        session.pop("selectedObject", None)
        return jsonify({
            "status": "error",
            "error": str(e),
            "message": f"Failed to stop tracking: {str(e)}"
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
            "message": "No telescope selected"
        }), 422
    
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
            except (ValueError, TypeError) as e:
                return jsonify({
                    "status": "error",
                    "error": "Failed to parse coordinates as numbers",
                    "message": f"RA: {ra}, DEC: {dec} - {str(e)}",
                    "telescope_id": telescope_id
                }), 500
        else:
            return jsonify({
                "status": "error",
                "error": "No coordinates in response",
                "message": f"Response structure: {coords}",
                "telescope_id": telescope_id
            }), 500
            
    except Exception as e:
        print(f"[TELESCOPE POSITION ERROR] Failed to get coordinates: {str(e)}", flush=True)
        import traceback
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "error": str(e),
            "message": f"Failed to get telescope position: {str(e)}"
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
