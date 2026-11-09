import math
import os
import re

import requests
from flask import Blueprint, Response, jsonify, render_template, request

from app import weather_cache

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


# --- Weather map data, served from our local cache (app/weather_cache.py) ---

# The browser library downloads from web workers with no origin of their own,
# so these responses need CORS headers even though they come from our site.
WEATHER_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range, If-Match, If-Unmodified-Since",
    "Access-Control-Expose-Headers": "Content-Length, Content-Range, ETag, Last-Modified",
    "Access-Control-Max-Age": "86400",
}
_RANGE_RE = re.compile(r"^bytes=(\d+)-(\d*)$")


def _weather_error(status, message):
    return Response(message, status=status, mimetype="text/plain", headers=WEATHER_CORS)


@map_bp.route("/map/weather/data_spatial/<path:path>", methods=["GET", "HEAD", "OPTIONS"])
def weather_data(path):
    """Stands in for Open-Meteo's data_spatial folder on S3: latest.json and
    byte ranges of the .om files, answered from the local cache."""
    if request.method == "OPTIONS":
        return Response(status=204, headers=WEATHER_CORS)

    try:
        if path == f"{weather_cache.DOMAIN}/latest.json":
            return Response(weather_cache.latest_meta(), mimetype="application/json",
                            headers={**WEATHER_CORS, "Cache-Control": "public, max-age=60"})

        info = weather_cache.file_info(path)
        headers = {
            **WEATHER_CORS,
            "Accept-Ranges": "bytes",
            # A run's files never change, so browsers can keep what they've read
            "Cache-Control": "public, max-age=86400, immutable",
        }
        if info.etag:
            headers["ETag"] = info.etag
        if info.last_modified:
            headers["Last-Modified"] = info.last_modified

        if request.method == "HEAD":
            response = Response(status=200, mimetype="application/octet-stream", headers=headers)
            response.headers["Content-Length"] = str(info.size)  # the whole file's size, as S3 gives it
            return response

        match = _RANGE_RE.match(request.headers.get("Range", "").strip())
        if not match:
            return _weather_error(416, "Send a single byte range")
        start = int(match.group(1))
        end = min(int(match.group(2)) if match.group(2) else info.size - 1, info.size - 1)
        if start > end:
            return _weather_error(416, "Range outside the file")
        data = weather_cache.read_range(path, start, end - start + 1)
    except weather_cache.NotFound:
        return _weather_error(404, "Not found")
    except weather_cache.UpstreamError:
        return _weather_error(502, "Weather data unavailable")
    except ValueError as exc:
        return _weather_error(416, str(exc))

    return Response(data, status=206, mimetype="application/octet-stream",
                    headers={**headers, "Content-Range": f"bytes {start}-{end}/{info.size}"})
