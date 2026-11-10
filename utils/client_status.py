"""The client's state in one dictionary: getClientStatus for the server,
the periodic status push (core/networking/websocket.py) and the local
page. Each part is best-effort; a part that fails is left out.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# What this client understands beyond the v0.8 commands; the server only
# sends the new arguments to clients that list them.
CAPABILITIES: List[str] = ["capture_frame_type", "upload_pending_filter", "liveview_jpeg_options", "dead_zones"]


def _part(fn: Callable[[], Any]) -> Any:
	try:
		return fn()
	except Exception as exc:
		return {"error": str(exc)[:80]}


def _or_none(fn: Callable[[], Any]) -> Any:
	# For parts the server reads field by field: nothing rather than an error.
	try:
		return fn()
	except Exception:
		return None


def tracking_part() -> Dict[str, Any]:
	from core.hardware import tracking
	from utils.telescope_state import get_parked, get_telescope_coords

	target = tracking._target_object if tracking._tracking_active else None
	coords = get_telescope_coords() or {}
	out: Dict[str, Any] = {
		"state": tracking._panel_state,
		"target": (target or {}).get("name"),
		"ra": coords.get("right_ascension"),
		"dec": coords.get("declination"),
		"parked": get_parked(),
	}
	for name in ("current_rate", "alignment_summary"):
		fn = getattr(tracking, name, None)
		if callable(fn):
			out[name.replace("current_", "")] = _part(fn)
	return out


def site_part() -> Optional[Dict[str, Any]]:
	"""Where the telescope is: decimal degrees, east positive."""
	from utils.location import get_current_location

	loc = get_current_location() or {}
	if loc.get("latitude") is None or loc.get("longitude") is None:
		return None
	site: Dict[str, Any] = {"lat": round(float(loc["latitude"]), 5), "lon": round(float(loc["longitude"]), 5)}
	elevation = loc.get("elevation_m", loc.get("altitude"))
	if elevation is not None:
		site["elevation_m"] = round(float(elevation))
	return site


def dead_zones_part() -> Dict[str, Any]:
	"""{"zones": [...]}: where the telescope never points (utils/dead_zones.py)."""
	from utils import dead_zones

	return dead_zones.status_part()


def camera_part() -> Dict[str, Any]:
	from core.camera.service import get_camera_service
	from utils.handlers import camera_readings

	return {**get_camera_service().status(), **camera_readings()}


def collect() -> Dict[str, Any]:
	from core.camera.recorder import get_recorder
	from core.camera.stream import get_live_stream
	from utils import clock, photos
	from utils.uploads import get_upload_queue

	def esp32() -> Dict[str, Any]:
		from utils.esp32_events import estop_active
		from utils.esp32_state import esp32_state

		return {"connected": esp32_state.is_available(), "estop": estop_active()}

	def photo_run() -> Dict[str, Any]:
		from utils.panel_ui import get_panel_ui

		return get_panel_ui().photo_run.status()

	def solve() -> Optional[Dict[str, Any]]:
		from utils.plate_solve import last_solve

		return last_solve()

	return {
		"type": "status",
		"capabilities": list(CAPABILITIES),
		"site": _or_none(site_part),
		"dead_zones": _or_none(dead_zones_part),
		"solve": _or_none(solve),
		"tracking": _part(tracking_part),
		"camera": _part(camera_part),
		"liveview": _part(lambda: get_live_stream().stats()),
		"recording": _part(lambda: get_recorder().status()),
		"photos": _part(photo_run),
		"uploads": _part(lambda: get_upload_queue().status()),
		"clock": _part(clock.status),
		"disk": _part(photos.disk_text),
		"esp32": _part(esp32),
	}
