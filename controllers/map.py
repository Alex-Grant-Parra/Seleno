import math
import os

import requests
from flask import Blueprint, jsonify, render_template, request

map_bp = Blueprint("map", __name__)

DEFAULT_BORTLE_WMS_URL = "https://deneb.lightpollutionmap.info/geoserver/ows"
# World Atlas 2015 (Falchi et al.): artificial zenith sky brightness in mcd/m².
DEFAULT_BORTLE_WMS_LAYER = "PostGIS:WA_2015"

# Natural sky brightness used by the World Atlas (about 22.0 mag/arcsec²).
NATURAL_SKY_MCD = 0.171

# Lower SQM bound (mag/arcsec²) for Bortle classes 1-8; a sky brighter
# than the last bound is class 9. Keep in sync with the key in templates/map.html.
BORTLE_SQM_BOUNDS = [21.99, 21.89, 21.69, 20.49, 19.50, 18.94, 18.38, 17.80]


def _bortle_wms_url():
    return os.getenv("BORTLE_WMS_URL", DEFAULT_BORTLE_WMS_URL)


def _bortle_wms_layer():
    return os.getenv("BORTLE_WMS_LAYER", DEFAULT_BORTLE_WMS_LAYER)


def sqm_from_artificial(artificial_mcd):
    total_cd = (max(artificial_mcd, 0.0) + NATURAL_SKY_MCD) / 1000.0
    return -2.5 * math.log10(total_cd / 108000.0)


def bortle_from_sqm(sqm):
    for level, bound in enumerate(BORTLE_SQM_BOUNDS, start=1):
        if sqm >= bound:
            return level
    return 9


@map_bp.route("/map")
def map_page():
    return render_template(
        "map.html",
        open_meteo_tile_url=os.getenv("OPEN_METEO_TILE_URL", ""),
        open_meteo_attribution=os.getenv("OPEN_METEO_ATTRIBUTION", ""),
        bortle_tile_url=os.getenv("BORTLE_TILE_URL", ""),
        bortle_wms_url=_bortle_wms_url(),
        bortle_wms_layer=_bortle_wms_layer(),
        bortle_wms_time=os.getenv("BORTLE_WMS_TIME", ""),
        bortle_fixed_url=os.getenv("BORTLE_FIXED_URL", ""),
        bortle_attribution=os.getenv(
            "BORTLE_ATTRIBUTION",
            "© <a href='https://www.lightpollutionmap.info' target='_blank'>Light Pollution Map Contributors</a>",
        ),
        satellite_tile_url=os.getenv("SATELLITE_TILE_URL", ""),
        satellite_attribution=os.getenv("SATELLITE_ATTRIBUTION", ""),
    )


@map_bp.route("/map/bortle")
def bortle_at_point():
    """Look up sky brightness at a point. Done server-side because the
    light pollution map server doesn't allow browser (CORS) requests."""
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is None or lon is None or not (-90 <= lat <= 90):
        return jsonify({"error": "lat and lon are required"}), 400
    lon = ((lon + 180.0) % 360.0) - 180.0

    # Query the centre pixel of a tiny 3x3 box around the point.
    d = 0.001
    params = {
        "service": "WMS",
        "version": "1.1.1",
        "request": "GetFeatureInfo",
        "layers": _bortle_wms_layer(),
        "query_layers": _bortle_wms_layer(),
        "styles": "",
        "srs": "EPSG:4326",
        "bbox": f"{lon - d},{lat - d},{lon + d},{lat + d}",
        "width": 3,
        "height": 3,
        "x": 1,
        "y": 1,
        "info_format": "application/json",
    }
    try:
        # The server rejects the default python-requests User-Agent with a 403.
        resp = requests.get(
            _bortle_wms_url(),
            params=params,
            headers={"User-Agent": "TelescopeControl-WorldMap/1.0"},
            timeout=8,
        )
        resp.raise_for_status()
        features = resp.json().get("features") or []
        props = features[0].get("properties") if features else None
        value = None
        if props:
            value = props.get("GRAY_INDEX")
            if value is None:
                value = next((v for v in props.values() if isinstance(v, (int, float))), None)
    except (requests.RequestException, ValueError, AttributeError):
        return jsonify({"error": "lookup failed"}), 502

    if value is None or not math.isfinite(float(value)) or float(value) < 0:
        # No data here (e.g. open ocean outside the atlas).
        return jsonify({"bortle": None})

    artificial = float(value)
    sqm = sqm_from_artificial(artificial)
    return jsonify({
        "bortle": bortle_from_sqm(sqm),
        "sqm": round(sqm, 2),
        "artificial_mcd": round(artificial, 4),
    })
