"""The Pi's tabs in the front-panel menu.

The ESP32 draws the menu; these definitions are sent to it as JSON
({"cmd":"ui","action":"menu"}), and changes come back as events that
utils/panel_ui.py routes to the callbacks here.

Adding an option is one line in build_panels(). Item kinds:

	Info(id, label, get)                         read-only value
	Action(id, label, run, confirm=...)          SELECT runs run(); return a
	                                             string to show it as a toast
	Toggle(id, label, get, set)                  on/off
	Choice(id, label, options, get, set)         get/set use the option index
	Number(id, label, get, set, min, max, step, unit)
	Text(id, label, get, set, max_length, secret, hint)

A Choice's options can come from a function (options=...), for lists that
change (the camera's ISO values); changes are sent like values.

get() is polled every couple of seconds and only changes are sent, so keep
it cheap: anything slow (the camera) is read in the background and cached
(CameraSettings). set() may return an error string, which is shown on the panel
(the item then snaps back to get()'s value). only_when(item, condition)
hides an item while condition() is false (checked with the values). Tabs
are sorted by `order`; the firmware's own are Motors 30, Test 40,
Display 60, About 90.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Union

from utils import clock, manual_control, photos, pi_system
from utils.config_state import get_section, is_offline_mode, save_section
from utils.sky_view import FOV_STEPS

if TYPE_CHECKING:
	from utils.panel_ui import PanelUI


@dataclass
class Item:
	id: str
	label: str
	type: str = "action"
	get: Optional[Callable[[], Any]] = None
	set: Optional[Callable[[Any], Optional[str]]] = None
	run: Optional[Callable[[], Optional[str]]] = None
	extra: Dict[str, Any] = field(default_factory=dict)
	visible: Optional[Callable[[], bool]] = None
	options: Optional[Callable[[], List[str]]] = None  # a Choice's changing options

	def to_json(self) -> Dict[str, Any]:
		out: Dict[str, Any] = {"id": self.id, "type": self.type, "label": self.label}
		out.update(self.extra)
		if self.options is not None:
			out["options"] = list(self.options()) or ["-"]
		if self.get is not None:
			out["value"] = self.get()
		if self.visible is not None:
			out["visible"] = bool(self.visible())
		return out


def Info(id: str, label: str, get: Callable[[], Any]) -> Item:
	return Item(id, label, "info", get=lambda: str(get()))


def Action(id: str, label: str, run: Callable[[], Optional[str]], confirm: str = "", hint: str = "") -> Item:
	extra = {k: v for k, v in (("confirm", confirm), ("hint", hint)) if v}
	return Item(id, label, "action", run=run, extra=extra)


def Toggle(id: str, label: str, get: Callable[[], bool], set: Callable[[bool], Optional[str]]) -> Item:
	return Item(id, label, "toggle", get=lambda: bool(get()), set=lambda v: set(bool(v)))


def Choice(id: str, label: str, options: Union[Sequence[str], Callable[[], List[str]]], get: Callable[[], int],
		set: Callable[[int], Optional[str]]) -> Item:
	if callable(options):
		return Item(id, label, "choice", get=get, set=lambda v: set(int(v)), options=options)
	return Item(id, label, "choice", get=get, set=lambda v: set(int(v)), extra={"options": list(options)})


def Number(id: str, label: str, get: Callable[[], float], set: Callable[[float], Optional[str]],
		min: float, max: float, step: float = 1, unit: str = "", decimals: int = 0) -> Item:
	return Item(id, label, "number", get=get, set=set,
		extra={"min": min, "max": max, "step": step, "unit": unit, "decimals": decimals})


def Text(id: str, label: str, get: Callable[[], str], set: Callable[[str], Optional[str]],
		max_length: int = 64, secret: bool = False, hint: str = "") -> Item:
	# hint: shown under the keyboard's title (and in the footer if short)
	extra: Dict[str, Any] = {"max_length": max_length, "secret": secret}
	if hint:
		extra["hint"] = hint
	return Item(id, label, "text", get=get, set=lambda v: set(str(v)), extra=extra)


def only_when(item: Item, condition: Callable[[], bool]) -> Item:
	item.visible = condition
	return item


@dataclass
class Panel:
	id: str
	title: str
	order: int
	items: List[Item]

	def to_json(self) -> Dict[str, Any]:
		return {"id": self.id, "title": self.title, "order": self.order, "items": [i.to_json() for i in self.items]}


# --- helpers ------------------------------------------------------------------

def _client_config() -> Dict[str, Any]:
	from utils.config_state import get_client_config

	return get_client_config()


def _bare_server(url: str) -> str:
	"""https://seleno.org/ -> seleno.org (what people type)."""
	return re.sub(r"^https?://", "", url or "").rstrip("/")


def validate_device_name(value: str) -> Optional[str]:
	value = value.strip()
	if not value:
		return "The name can't be empty"
	if not re.fullmatch(r"[A-Za-z0-9 ._-]{1,32}", value):
		return "Use letters, numbers, space . _ -"
	return None


def validate_server(value: str) -> Optional[str]:
	value = _bare_server(value.strip())
	if not value or " " in value or "." not in value.split("/")[0]:
		return "Type a domain like seleno.org"
	return None


def validate_api_key(value: str) -> Optional[str]:
	value = value.strip()
	if len(value) < 8:
		return "That key looks too short"
	if " " in value:
		return "The key can't contain spaces"
	return None


def _server_state() -> str:
	if is_offline_mode():
		return "offline"
	try:
		from core.networking.websocket import server_state
	except Exception:
		return "?"
	return server_state.get("state", "?")


def _tracking_status() -> str:
	try:
		from core.hardware import tracking
	except Exception:
		return "?"
	target = (tracking._target_object or {}).get("name") if tracking._tracking_active else None
	if not target:
		return "Idle"
	return f"{'Tracking' if tracking.is_sidereal_tracking() else 'Slewing to'} {target}"


def _liveview(on: bool) -> Optional[str]:
	try:
		from utils.handlers import startLiveView, stopLiveView

		(startLiveView if on else stopLiveView)()
	except Exception as exc:
		return f"Live view: {exc}"
	return None


def _liveview_on() -> bool:
	try:
		from utils.liveview_state import is_liveview_enabled

		return bool(is_liveview_enabled())
	except Exception:
		return False


def _camera() -> str:
	try:
		from core.camera.service import get_camera_service

		service = get_camera_service()
		return (service.model or "connected") if service.is_available() else "not found"
	except Exception:
		return "?"


def _camera_connected() -> bool:
	try:
		from core.camera.service import get_camera_service

		return get_camera_service().is_available()
	except Exception:
		return False


def _online() -> bool:
	return not is_offline_mode()


def _stop_tracking() -> Optional[str]:
	from core.hardware.tracking import stop_tracking

	stop_tracking()
	return "Tracking stopped"


def _tracking_now() -> bool:
	try:
		from core.hardware import tracking

		return tracking.is_sidereal_tracking()
	except Exception:
		return False


def _solver() -> bool:
	try:
		from core.astrometry import solver

		return solver.available()
	except Exception:
		return False


def _parked() -> bool:
	try:
		from utils.telescope_state import get_parked

		return get_parked()
	except Exception:
		return False


def _run_job(ui: "PanelUI", name: str, fn: Callable[[], Dict[str, Any]], busy: str) -> Optional[str]:
	"""Something slow (a park) on its own thread; the result as a toast."""
	def run() -> None:
		try:
			result = fn() or {}
		except Exception as exc:
			result = {"error": str(exc)}
		ui.toast(str(result.get("error") or result.get("status") or "Done")[:60])
	threading.Thread(target=run, name=f"panel-{name}", daemon=True).start()
	return busy


# --- camera settings, read in the background ----------------------------------------

class CameraSettings:
	"""The camera's ISO, shutter, battery, card and zoom, for the panel:
	read off the panel's thread at most every MAX_AGE_S, so get() never
	waits on the camera (which, with the gphoto2 command, pauses live view)."""

	MAX_AGE_S = 30.0
	SETTINGS = ("iso", "shutterspeed", "batterylevel", "zoom")

	def __init__(self) -> None:
		self._values: Dict[str, Dict[str, Any]] = {}
		self._card: Optional[Dict[str, Any]] = None
		self._at = 0.0
		self._busy = threading.Lock()
		self._model = ""

	def _refresh(self) -> None:
		from core.camera.backends import CameraError
		from core.camera.service import get_camera_service

		service = get_camera_service()
		values: Dict[str, Dict[str, Any]] = {}
		for setting in self.SETTINGS:
			try:
				if service.setting_name(setting) is not None:
					values[setting] = service.get_setting(setting)
			except CameraError:
				continue
		try:
			self._card = service.storage()
		except CameraError:
			self._card = None
		self._values = values
		self._model = service.model
		self._at = time.monotonic()

	def _maybe_refresh(self) -> None:
		if not _camera_connected():
			self._values, self._card = {}, None
			return
		from core.camera.service import get_camera_service

		stale = time.monotonic() - self._at > self.MAX_AGE_S or get_camera_service().model != self._model
		if stale and self._busy.acquire(blocking=False):
			def run() -> None:
				try:
					self._refresh()
				except Exception:
					pass
				finally:
					self._busy.release()
			threading.Thread(target=run, name="panel-camera", daemon=True).start()

	def invalidate(self) -> None:
		self._at = 0.0

	def info(self, setting: str) -> Dict[str, Any]:
		self._maybe_refresh()
		return self._values.get(setting) or {}

	def options(self, setting: str) -> List[str]:
		return [str(c) for c in self.info(setting).get("choices") or []]

	def index(self, setting: str) -> int:
		info = self.info(setting)
		choices = [str(c) for c in info.get("choices") or []]
		value = str(info.get("value"))
		return choices.index(value) if value in choices else 0

	def setter(self, setting: str) -> Callable[[int], Optional[str]]:
		def set_value(index: int) -> Optional[str]:
			choices = self.options(setting)
			if not 0 <= index < len(choices):
				return "Camera not ready"
			try:
				from core.camera.service import get_camera_service

				get_camera_service().set_setting(setting, choices[index])
			except Exception as exc:
				return str(exc)[:60]
			self._values.setdefault(setting, {})["value"] = choices[index]
			return None
		return set_value

	def battery(self) -> str:
		value = self.info("batterylevel").get("value")
		return str(value) if value not in (None, "") else "-"

	def card(self) -> str:
		self._maybe_refresh()
		card = self._card
		if not card:
			return "-"
		shots = f", {card['free_images']} shots" if card.get("free_images") else ""
		return f"{card['free_mb'] / 1024:.1f} GB free{shots}"

	def has(self, setting: str) -> bool:
		return bool(self.info(setting).get("choices"))


camera_settings = CameraSettings()


# --- focus ---------------------------------------------------------------------------

_focus_cache: Dict[str, Any] = {"at": 0.0, "text": "-"}
FOCUS_HOLDER = "panel focus"


def _focus_on() -> bool:
	from core.camera.stream import get_live_stream

	return get_live_stream().focus


def _set_focus(on: bool) -> Optional[str]:
	"""Focus mode: live view runs (for the HFR here, and the crop the
	server gets) while it's on."""
	from core.camera.service import get_camera_service
	from core.camera.stream import get_live_stream

	get_live_stream().set_focus(on)
	if on:
		get_camera_service().acquire_preview(FOCUS_HOLDER)
		return "Focus mode: the HFR shows below"
	get_camera_service().release_preview(FOCUS_HOLDER)
	return None


def _focus_text() -> str:
	"""HFR of the brightest star (smaller is sharper), every 2 s at most."""
	if time.monotonic() - _focus_cache["at"] < 2.0:
		return _focus_cache["text"]
	_focus_cache["at"] = time.monotonic()
	try:
		from core.camera.focus import analyse
		from core.camera.service import get_camera_service

		_seq, frame, at = get_camera_service().frames.latest()
		if frame is None or time.monotonic() - at > 5.0:
			_focus_cache["text"] = "-"
		else:
			result = analyse(frame)
			hfr = result.get("hfr")
			saturated = " sat" if (result.get("peak") or 0) >= 250 else ""
			_focus_cache["text"] = (f"HFR {hfr:.2f}{saturated}" if hfr is not None
				else f"sharp {result.get('sharpness', 0):.0f}")
	except Exception:
		_focus_cache["text"] = "?"
	return _focus_cache["text"]


# --- live view, recording -----------------------------------------------------------

QUALITY_LABELS = ["Original", "High", "Medium", "Low"]


def _quality_index() -> int:
	q = str(get_section("liveview").get("quality", "medium")).lower()
	names = [label.lower() for label in QUALITY_LABELS]
	return names.index(q) if q in names else 2


def _set_quality(index: int) -> Optional[str]:
	from utils.handlers import setLiveViewOptions

	result = setLiveViewOptions(quality=QUALITY_LABELS[max(0, min(3, index))].lower())
	return result.get("error")


def _stream_text() -> str:
	from core.camera.stream import get_live_stream

	s = get_live_stream().stats()
	if s.get("paused"):
		return "paused: no viewers"
	return f"{s['fps']:.0f} fps {s['kbps']:.0f} kbps"


def _recording() -> bool:
	from core.camera.recorder import get_recorder

	return get_recorder().running


def _set_recording(on: bool) -> Optional[str]:
	from core.camera.recorder import get_recorder

	recorder = get_recorder()
	try:
		return recorder.start("video") if on else recorder.stop()
	except (ValueError, RuntimeError) as exc:
		return str(exc)[:60]


def _snapshot() -> Optional[str]:
	from core.camera.recorder import snapshot

	try:
		import os

		return f"Saved {os.path.basename(snapshot())}"[:60]
	except RuntimeError as exc:
		return str(exc)[:60]


ZOOM_LEVELS = ["1", "5", "10"]


def _zoom_index() -> int:
	value = str(camera_settings.info("zoom").get("value"))
	return ZOOM_LEVELS.index(value) if value in ZOOM_LEVELS else 0


def _set_zoom(index: int) -> Optional[str]:
	from utils.handlers import setCameraZoom

	result = setCameraZoom(ZOOM_LEVELS[max(0, min(2, index))])
	camera_settings.invalidate()
	return result.get("error") if isinstance(result, dict) else None


def _upload_offline() -> Optional[str]:
	from utils.handlers import uploadOfflinePhotos

	result = uploadOfflinePhotos()
	return result.get("error") or f"{result.get('queued', 0)} files queued"


# --- photos ------------------------------------------------------------------------

FRAME_LABELS = ["Light", "Dark", "Flat", "Bias"]


def _photo_setting(key: str, default: Any) -> Any:
	return photos._settings().get(key, default)


def _frame_index() -> int:
	kind = str(_photo_setting("kind", "light")).capitalize()
	return FRAME_LABELS.index(kind) if kind in FRAME_LABELS else 0


def _set_frame(index: int) -> Optional[str]:
	photos._save_setting("kind", FRAME_LABELS[max(0, min(3, index))].lower())
	return None


def _exposure() -> float:
	try:
		return float(_photo_setting("exposure_s", 0) or 0)
	except (TypeError, ValueError):
		return 0.0


def _set_exposure(value: float) -> Optional[str]:
	photos._save_setting("exposure_s", max(0.0, min(900.0, float(value))))
	return None


def _dither() -> bool:
	return bool(_photo_setting("dither", False))


def _set_dither(on: bool) -> Optional[str]:
	photos._save_setting("dither", bool(on))
	return None


def photo_plan(count: int) -> Optional[photos.SequencePlan]:
	"""The Camera tab's frame type, exposure and dither as a sequence (None
	for plain photos at the camera's settings)."""
	kind = FRAME_LABELS[_frame_index()].lower()
	exposure = _exposure() or None
	if kind == "light" and exposure is None and not _dither():
		return None
	return photos.SequencePlan(kind=kind, count=count, exposure_s=exposure, dither_every=1 if _dither() else 0)


# --- setup ---------------------------------------------------------------------------

def _utc_text() -> str:
	"""The time now as the panel edits it: month-day hours:minutes, UTC."""
	return clock.now().strftime("%m-%d %H:%M")


def _set_time(text: str) -> Optional[str]:
	if not text.strip():
		clock.clear()
		return "Using the Pi's clock"
	try:
		offset = clock.set_time(clock.parse_utc(text), source="manual")
	except ValueError as exc:
		return str(exc)
	if clock.system_synced():
		return "The Pi's clock is synced: kept that"
	return f"Time set ({offset:+.0f} s)"


def _horizon() -> float:
	return float(get_section("horizon").get("min_alt_deg", 0.0) or 0.0)


def _set_horizon(value: float) -> Optional[str]:
	save_section("horizon", {"min_alt_deg": max(-5.0, min(45.0, float(value)))})
	return None


def _local_page_on() -> bool:
	from core.networking import local_page

	return local_page.running()


def _set_local_page(on: bool) -> Optional[str]:
	from core.networking import local_page

	return local_page.set_enabled(on)


def _local_page_text() -> str:
	from core.networking import local_page

	return f"{local_page.address().replace('http://', '')} code {local_page.code()}"


MOTOR_POLICIES = ["guarded", "off", "unrestricted"]


def _motor_policy_index() -> int:
	value = str(get_section("server").get("raw_motor_commands", "guarded")).lower()
	return MOTOR_POLICIES.index(value) if value in MOTOR_POLICIES else 0


def _set_motor_policy(index: int) -> Optional[str]:
	save_section("server", {"raw_motor_commands": MOTOR_POLICIES[max(0, min(2, index))]})
	return None


HEMISPHERES = ["auto", "north", "south"]


def _hemisphere_index() -> int:
	from utils.config_state import get_mount_config

	value = get_mount_config().get("hemisphere", "auto")
	return HEMISPHERES.index(value) if value in HEMISPHERES else 0


def _set_hemisphere(index: int) -> Optional[str]:
	save_section("mount", {"hemisphere": HEMISPHERES[max(0, min(2, index))]})
	return None


RATE_LABELS = ["Sidereal", "Lunar", "Solar", "King"]


def _rate_index() -> int:
	value = str(get_section("tracking").get("rate", "sidereal")).capitalize()
	return RATE_LABELS.index(value) if value in RATE_LABELS else 0


def _set_rate(index: int) -> Optional[str]:
	from core.hardware.tracking import set_rate

	result = set_rate(RATE_LABELS[max(0, min(3, index))].lower())
	return result.get("error")


def _alignment_points() -> int:
	try:
		from utils.telescope_state import get_alignment

		return len(get_alignment().points)
	except Exception:
		return 0


# --- the panels ---------------------------------------------------------------

def build_panels(ui: "PanelUI") -> List[Panel]:
	sky = ui.sky_options
	fov_labels = [f"{int(f)} deg" for f in FOV_STEPS]

	def sky_toggle(attr: str) -> Callable[[bool], Optional[str]]:
		def set_option(on: bool) -> Optional[str]:
			setattr(ui.sky_options, attr, on)
			ui.sky_options_changed()
			return None
		return set_option

	def set_fov(index: int) -> Optional[str]:
		ui.sky_options.fov_deg = FOV_STEPS[max(0, min(len(FOV_STEPS) - 1, index))]
		ui.sky_options_changed()
		return None

	return [
		Panel("scope", "Scope", 10, [
			Info("status", "Status", _tracking_status),
			Action("sky", "Sky view", ui.show_sky),
			Action("goto", "Go to RA/Dec", lambda: ui.run_flow("goto", manual_control.goto_coordinates)),
			Action("star", "Go to star", lambda: ui.run_flow("goto", manual_control.goto_star)),
			Action("planet", "Go to planet", lambda: ui.run_flow("goto", manual_control.goto_planet)),
			Action("stop", "Stop tracking", _stop_tracking, confirm="Stop tracking and halt the motors?"),
			Action("align", "Align on star", lambda: ui.run_flow("align", manual_control.align_on_star)),
			only_when(Action("solve", "Solve & sync", lambda: ui.run_flow("solve", manual_control.solve_and_sync)),
				_solver),
			only_when(Action("centre", "Centre target", lambda: ui.run_flow("solve", manual_control.centre_target)),
				lambda: _solver() and _tracking_now()),
			only_when(Action("polar", "Polar align", lambda: ui.run_flow("solve", manual_control.polar_align)),
				_solver),
			Info("alignment", "Alignment", manual_control.alignment_text),
			only_when(Info("polar_err", "Polar", manual_control.polar_text), lambda: _alignment_points() >= 3),
			only_when(Action("clear_align", "Clear alignment", lambda: ui.run_flow("align", manual_control.clear_alignment),
				confirm="Forget this session's alignment stars and solves?"), lambda: _alignment_points() > 0),
			Choice("rate", "Rate", RATE_LABELS, _rate_index, _set_rate),
			only_when(Action("park", "Park", lambda: _run_job(ui, "park", _park, "Parking..."),
				confirm="Stop tracking and slew to the park position?"), lambda: not _parked()),
			only_when(Action("unpark", "Unpark", lambda: _unpark()), _parked),
			Action("park_here", "Set park here", _park_here, confirm="Make where it points now the park position?"),
			Text("location", "Site", manual_control.site_text,
				lambda v: manual_control.set_site(v, ui.toast), max_length=40, hint="lat, lon"),
		]),
		Panel("camera", "Camera", 12, [
			Info("camera", "Camera", lambda: ui.photo_run.progress or _camera()),
			Action("shoot", "Take photos", ui.take_photos),
			Number("shots", "Shots", photos.shots, photos.set_shots, 1, photos.MAX_SHOTS),
			Choice("kind", "Frames", FRAME_LABELS, _frame_index, _set_frame),
			Number("exposure", "Exposure", _exposure, _set_exposure, 0, 900, 5, "s"),
			Toggle("dither", "Dither", _dither, _set_dither),
			only_when(Choice("iso", "ISO", lambda: camera_settings.options("iso"),
				lambda: camera_settings.index("iso"), camera_settings.setter("iso")), lambda: camera_settings.has("iso")),
			only_when(Choice("shutter", "Shutter", lambda: camera_settings.options("shutterspeed"),
				lambda: camera_settings.index("shutterspeed"), camera_settings.setter("shutterspeed")),
				lambda: camera_settings.has("shutterspeed")),
			only_when(Info("battery", "Battery", camera_settings.battery), _camera_connected),
			only_when(Info("card", "Card", camera_settings.card), _camera_connected),
			only_when(Toggle("liveview", "Live view", _liveview_on, _liveview), _online),
			only_when(Choice("quality", "Stream", QUALITY_LABELS, _quality_index, _set_quality), _online),
			only_when(Info("stream", "Stream now", _stream_text), _liveview_on),
			Toggle("focus", "Focus mode", _focus_on, _set_focus),
			only_when(Info("hfr", "Focus", _focus_text), _focus_on),
			only_when(Choice("zoom", "Zoom", [f"{z}x" for z in ZOOM_LEVELS], _zoom_index, _set_zoom),
				lambda: camera_settings.has("zoom")),
			Action("snapshot", "Snapshot", _snapshot),
			Toggle("record", "Record video", _recording, _set_recording),
			Info("saved", "On the Pi", lambda: f"{photos.saved_count()} files"),
			Info("disk", "Pi disk", photos.disk_text),
			only_when(Action("upload", "Upload offline", _upload_offline,
				confirm="Upload the photos taken offline to the server?"), lambda: _online() and bool(photos.owner_id())),
			Info("folder", "Path", photos.local_dir),
		]),
		Panel("sky", "Sky", 15, [
			Choice("fov", "Field", fov_labels,
				lambda: min(range(len(FOV_STEPS)), key=lambda i: abs(FOV_STEPS[i] - ui.sky_options.fov_deg)), set_fov),
			Toggle("horizon", "Horizon grid", lambda: sky.horizon_grid, sky_toggle("horizon_grid")),
			Toggle("equatorial", "Equatorial grid", lambda: sky.equatorial_grid, sky_toggle("equatorial_grid")),
			Toggle("ecliptic", "Ecliptic", lambda: sky.ecliptic, sky_toggle("ecliptic")),
			Toggle("constellations", "Constellations", lambda: sky.constellations, sky_toggle("constellations")),
			Toggle("con_names", "Constell. names", lambda: sky.constellation_names,
				sky_toggle("constellation_names")),
			Toggle("star_names", "Star names", lambda: sky.star_names, sky_toggle("star_names")),
			Toggle("planets", "Planets", lambda: sky.planets, sky_toggle("planets")),
			Toggle("below", "Below horizon", lambda: sky.below_horizon, sky_toggle("below_horizon")),
			Info("data", "Sky data", ui.sky_data_status),
			Action("refresh", "Update sky data", ui.refresh_sky_data),
		]),
		Panel("setup", "Setup", 20, [
			Toggle("offline", "Offline mode", is_offline_mode, ui.set_offline),
			Text("client_id", "Device name", lambda: _client_config().get("client_id", ""),
				lambda v: ui.save_setting("client_id", v), max_length=32),
			Text("base_url", "Server", lambda: _bare_server(_client_config().get("base_url", "")),
				lambda v: ui.save_setting("base_url", v), max_length=96),
			Text("api_token", "API key", lambda: _client_config().get("api_token", ""),
				lambda v: ui.save_setting("api_token", v), max_length=128, secret=True),
			Info("server", "Status", _server_state),
			Info("clock", "Clock", clock.short_status),
			Text("set_time", "UTC", _utc_text, _set_time, max_length=24, hint="UTC: 10-16 21:30"),
			Number("horizon", "Horizon", _horizon, _set_horizon, -5, 45, 1, "deg"),
			Choice("hemisphere", "Hemisphere", ["Auto", "North", "South"], _hemisphere_index, _set_hemisphere),
			Toggle("local_page", "Local page", _local_page_on, _set_local_page),
			only_when(Info("local_addr", "Page", _local_page_text), _local_page_on),
			Text("owner", "Photo owner", photos.owner_id, photos.set_owner_id, max_length=12, hint="account id"),
			Choice("motors", "Server motors", ["Guarded", "Off", "Any"], _motor_policy_index, _set_motor_policy),
			Action("wizard", "Run setup again", ui.start_wizard),
		]),
		Panel("pi", "Pi", 70, [
			Info("ip", "IP", pi_system.ip_address),
			Info("wifi", "Wi-Fi", pi_system.wifi_name),
			Info("host", "Hostname", pi_system.hostname),
			Info("server", "Server", _server_state),
			Info("uptime", "Uptime", pi_system.uptime),
			Action("restart", "Restart client", ui.restart_client, confirm="Restart the SELENO client on the Pi?"),
			Action("reboot", "Reboot Pi", pi_system.reboot, confirm="Reboot the Raspberry Pi?"),
			Action("shutdown", "Shut down Pi", lambda: ui.run_flow("shutdown", _shutdown_flow)),
		]),
	]


def _park() -> Dict[str, Any]:
	from core.hardware.tracking import park

	return park()


def _unpark() -> Optional[str]:
	from core.hardware.tracking import unpark

	return unpark().get("status")


def _park_here() -> Optional[str]:
	from core.hardware.tracking import set_park_here

	result = set_park_here()
	return result.get("error") or result.get("status")


def _shutdown_flow(ui: "PanelUI") -> None:
	"""Park first (it's where the next session will start), then shut down."""
	answer = ui.prompt("choice", "Shut down Pi", options=["Park, then shut down", "Shut down now"], selected=0)
	index = int(answer.get("value", -1))
	if index not in (0, 1):
		return
	if index == 0 and not _parked():
		ui.busy("Parking", "Slewing to the park position...")
		result = _park()
		if result.get("error"):
			ui.prompt("message", "Park", f"{result['error']}"[:100] + ". Not shutting down.")
			return
	try:
		from utils.telescope_state import flush

		flush()
	except Exception:
		pass
	try:
		ui.prompt("message", "Shutting down", "Wait for the Pi's light to stop before cutting power.",
			timeout_ms=3000, timeout_s=4)
	except Exception:
		pass  # it closes itself; shut down either way
	pi_system.shutdown()
