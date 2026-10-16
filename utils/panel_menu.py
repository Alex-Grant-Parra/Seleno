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

get() is polled every couple of seconds and only changes are sent, so keep
it cheap. set() may return an error string, which is shown on the panel
(the item then snaps back to get()'s value). only_when(item, condition)
hides an item while condition() is false (checked with the values). Tabs
are sorted by `order`; the firmware's own are Motors 30, Test 40,
Display 60, About 90.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from utils import manual_control, photos, pi_system
from utils.config_state import is_offline_mode
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

	def to_json(self) -> Dict[str, Any]:
		out: Dict[str, Any] = {"id": self.id, "type": self.type, "label": self.label}
		out.update(self.extra)
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


def Choice(id: str, label: str, options: Sequence[str], get: Callable[[], int],
		set: Callable[[int], Optional[str]]) -> Item:
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
		from utils.camera_state import camera_state

		return "connected" if camera_state.is_available() else "not found"
	except Exception:
		return "?"


def _online() -> bool:
	return not is_offline_mode()


def _stop_tracking() -> Optional[str]:
	from core.hardware.tracking import stop_tracking

	stop_tracking()
	return "Tracking stopped"


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
			Action("stop", "Stop tracking", _stop_tracking, confirm="Stop tracking and halt the motors?"),
			Action("align", "Align on star", lambda: ui.run_flow("align", manual_control.align_on_star)),
			Text("location", "Site", manual_control.site_text,
				lambda v: manual_control.set_site(v, ui.toast), max_length=40, hint="lat, lon"),
		]),
		Panel("camera", "Camera", 12, [
			Info("camera", "Camera", lambda: ui.photo_run.progress or _camera()),
			Action("shoot", "Take photos", ui.take_photos),
			Number("shots", "Shots", photos.shots, photos.set_shots, 1, photos.MAX_SHOTS),
			Info("saved", "On the Pi", lambda: f"{photos.saved_count()} files"),
			Info("folder", "Path", photos.local_dir),
			only_when(Toggle("liveview", "Live view", _liveview_on, _liveview), _online),
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
			Action("shutdown", "Shut down Pi", pi_system.shutdown,
				confirm="Shut the Pi down? Wait for its light to stop before cutting power."),
		]),
	]
