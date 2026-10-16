"""The client's state in one dictionary: getClientStatus for the server,
the periodic status push (core/networking/websocket.py) and the local
page. Each part is best-effort; a part that fails is left out.
"""
from __future__ import annotations

from typing import Any, Callable, Dict


def _part(fn: Callable[[], Any]) -> Any:
	try:
		return fn()
	except Exception as exc:
		return {"error": str(exc)[:80]}


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


def collect() -> Dict[str, Any]:
	from core.camera.recorder import get_recorder
	from core.camera.service import get_camera_service
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

	return {
		"type": "status",
		"tracking": _part(tracking_part),
		"camera": _part(lambda: get_camera_service().status()),
		"liveview": _part(lambda: get_live_stream().stats()),
		"recording": _part(lambda: get_recorder().status()),
		"photos": _part(photo_run),
		"uploads": _part(lambda: get_upload_queue().status()),
		"clock": _part(clock.status),
		"disk": _part(photos.disk_text),
		"esp32": _part(esp32),
	}
