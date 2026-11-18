"""The front panel's settings, for a phone or browser to change.

The server's Telescope Settings page (controllers/telescope_settings.py
there) asks getSettings() for this description and sends changes to
setSettings({key: value}). On the hotspot that's the phone at the telescope;
online it's the telescope's owner. Each setting is the same one the panel's
item changes, through the same code.

describe() -> {"sections": [{"id", "title", "note", "items": [item...]}]},
an item being {"key", "label", "type", "value", "help", ...}: type is text,
password, number (min, max, step, unit), choice (options: [{value, label}]),
toggle, info (read-only) or clock (the Pi's time; send an ISO time to set it).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from utils.config_state import get_client_config, get_section, is_offline_mode, save_section
from utils.log import get_logger

log = get_logger("settings")


@dataclass
class Setting:
	key: str
	label: str
	type: str
	get: Callable[[], Any]
	set: Optional[Callable[[Any], Optional[str]]] = None
	help: str = ""
	extra: Dict[str, Any] = field(default_factory=dict)
	# A notice once it's changed (the hotspot restarting, say)
	notice: Optional[Callable[[], str]] = None

	def describe(self) -> Dict[str, Any]:
		out = {"key": self.key, "label": self.label, "type": self.type, "value": self.get()}
		if self.help:
			out["help"] = self.help
		out.update(self.extra)
		return out


def _choices(pairs: List[tuple]) -> Dict[str, Any]:
	return {"options": [{"value": v, "label": label} for v, label in pairs]}


def _one_of(section: str, key: str, allowed: List[str]) -> Callable[[Any], Optional[str]]:
	def set_value(value: Any) -> Optional[str]:
		value = str(value).lower()
		if value not in allowed:
			return f"One of {', '.join(allowed)}"
		save_section(section, {key: value})
		return None
	return set_value


def _flag(section: str, key: str) -> Callable[[Any], Optional[str]]:
	def set_value(value: Any) -> Optional[str]:
		save_section(section, {key: bool(value) and str(value).lower() not in ("false", "0", "off")})
		return None
	return set_value


def _number(section: str, key: str, low: float, high: float) -> Callable[[Any], Optional[str]]:
	def set_value(value: Any) -> Optional[str]:
		try:
			number = float(value)
		except (TypeError, ValueError):
			return "A number"
		if not low <= number <= high:
			return f"Between {low:g} and {high:g}"
		save_section(section, {key: number})
		return None
	return set_value


# --- the settings that need their own code -------------------------------------------------

def _site() -> str:
	from utils import manual_control

	return manual_control.site_text()


def _set_site(value: Any) -> Optional[str]:
	from utils import location, manual_control

	try:
		lat, lon = manual_control.parse_site(str(value))
	except ValueError as exc:
		return str(exc)
	location.set_manual_location(lat, lon)
	return None


def _clock() -> str:
	from utils import clock

	return clock.now().astimezone(timezone.utc).isoformat(timespec="seconds")


def _set_clock(value: Any) -> Optional[str]:
	from utils import clock

	try:
		when = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
	except ValueError:
		return "An ISO time, like 2026-11-18T21:30:00Z"
	if when.tzinfo is None:
		when = when.replace(tzinfo=timezone.utc)
	clock.set_time(when.astimezone(timezone.utc), source="manual")
	return None


def _clock_help() -> str:
	from utils import clock

	return f"{clock.short_status()}. The Pi has no clock battery: offline, it can wake at the wrong time."


def _rate() -> str:
	return str(get_section("tracking").get("rate", "sidereal")).lower()


def _set_rate(value: Any) -> Optional[str]:
	from core.hardware.tracking import set_rate

	return set_rate(str(value).lower()).get("error")


def _set_quality(value: Any) -> Optional[str]:
	from utils.handlers import setLiveViewOptions

	return setLiveViewOptions(quality=str(value).lower()).get("error")


def _owner() -> str:
	from utils import photos

	return photos.owner_id()


def _set_owner(value: Any) -> Optional[str]:
	from utils import photos

	return photos.set_owner_id(str(value))


def _hotspot_name() -> str:
	from core.networking import hotspot

	return hotspot.name()


def _hotspot_password() -> str:
	from core.networking import hotspot

	return hotspot.password()


def _set_hotspot_name(value: Any) -> Optional[str]:
	from core.networking import hotspot

	return hotspot.set_name(str(value), delay_s=hotspot.APPLY_DELAY_S)


def _set_hotspot_password(value: Any) -> Optional[str]:
	from core.networking import hotspot

	return hotspot.set_password(str(value), delay_s=hotspot.APPLY_DELAY_S)


def _hotspot_notice() -> str:
	from core.networking import hotspot

	if not hotspot.wanted():
		return ""
	return (f"The telescope's Wi-Fi restarts in a few seconds as {hotspot.name()}: "
		"join it again with the new details, then reload this page.")


def _hotspot_status() -> str:
	from core.networking import hotspot, phone_link

	return f"Wi-Fi {hotspot.status_text()}; app link {phone_link.status_text()}"


# --- the list -----------------------------------------------------------------------------------

def sections() -> List[Dict[str, Any]]:
	hotspot = {
		"id": "hotspot", "title": "Offline Wi-Fi hotspot",
		"note": ("In offline mode the telescope makes its own Wi-Fi, with no internet: join it and open the "
			"Seleno app to use the telescope from a phone. Changing these restarts it."),
		"items": [
			Setting("hotspot.name", "Telescope name", "text", _hotspot_name, _set_hotspot_name,
				"The Wi-Fi's name, and the telescope's name in the app offline.",
				{"max_length": 32}, notice=_hotspot_notice),
			Setting("hotspot.password", "Wi-Fi password", "password", _hotspot_password, _set_hotspot_password,
				"8 to 63 characters. It's also shown on the telescope's screen.",
				{"max_length": 63}, notice=_hotspot_notice),
			Setting("hotspot.status", "Now", "info", _hotspot_status),
		],
	}
	site = {
		"id": "site", "title": "Site and time",
		"items": [
			Setting("site", "Site", "text", _site, _set_site, "Latitude, longitude: 51.48, -0.01",
				{"max_length": 40, "placeholder": "51.48, -0.01"}),
			Setting("clock", "Clock", "clock", _clock, _set_clock, _clock_help()),
			Setting("horizon.min_alt_deg", "Lowest altitude", "number",
				lambda: float(get_section("horizon").get("min_alt_deg", 0.0) or 0.0),
				_number("horizon", "min_alt_deg", -5, 45),
				"Go-tos below this are refused, and tracking stops when the target sets below it.",
				{"min": -5, "max": 45, "step": 1, "unit": "°"}),
			Setting("mount.hemisphere", "Hemisphere", "choice",
				lambda: str(get_section("mount").get("hemisphere", "auto")).lower(),
				_one_of("mount", "hemisphere", ["auto", "north", "south"]),
				"Auto picks it from the site's latitude.",
				_choices([("auto", "Auto"), ("north", "North"), ("south", "South")])),
		],
	}
	tracking = {
		"id": "tracking", "title": "Tracking",
		"items": [
			Setting("tracking.rate", "Rate", "choice", _rate, _set_rate, "",
				_choices([("sidereal", "Sidereal (stars)"), ("lunar", "Lunar"), ("solar", "Solar"), ("king", "King")])),
			Setting("tracking.auto_rate", "Follow the Moon and planets", "toggle",
				lambda: bool(get_section("tracking").get("auto_rate", True)), _flag("tracking", "auto_rate"),
				"Their own motion, on both axes."),
			Setting("tracking.refraction", "Allow for refraction", "toggle",
				lambda: bool(get_section("tracking").get("refraction", True)), _flag("tracking", "refraction"),
				"Aim where things appear: the air lifts them near the horizon."),
		],
	}
	camera = {
		"id": "camera", "title": "Camera and photos",
		"items": [
			Setting("liveview.quality", "Live view quality", "choice",
				lambda: str(get_section("liveview").get("quality", "medium")).lower(), _set_quality, "",
				_choices([("original", "Original"), ("high", "High"), ("medium", "Medium"), ("low", "Low")])),
			Setting("camera.focal_mm", "Focal length", "number",
				lambda: float(get_section("camera").get("focal_mm", 0) or 0), _number("camera", "focal_mm", 0, 10000),
				"The telescope's, for stacking. 0: unknown.", {"min": 0, "max": 10000, "step": 1, "unit": "mm"}),
			Setting("upload.raw", "RAW files", "choice",
				lambda: str(get_section("upload").get("raw", "after_jpeg")).lower(),
				_one_of("upload", "raw", ["after_jpeg", "on_request", "never"]),
				"When RAWs go to the album: after the JPEGs, only when asked for, or never.",
				_choices([("after_jpeg", "After the JPEGs"), ("on_request", "When asked for"), ("never", "Never")])),
			Setting("photos.owner_id", "Photo owner", "text", _owner, _set_owner,
				"Your seleno.org account id: photos taken offline upload to it with \"Upload offline\".",
				{"max_length": 12, "placeholder": "account id"}),
		],
	}
	safety = {
		"id": "safety", "title": "Motors",
		"items": [
			Setting("server.raw_motor_commands", "Manual motor control", "choice",
				lambda: str(get_section("server").get("raw_motor_commands", "guarded")).lower(),
				_one_of("server", "raw_motor_commands", ["guarded", "off", "unrestricted"]),
				"Guarded refuses moves past the axis limits, towards the Sun or into a dead zone.",
				_choices([("guarded", "Guarded"), ("off", "Off"), ("unrestricted", "Unrestricted (bench testing)")])),
		],
	}
	device = {
		"id": "device", "title": "This telescope",
		"items": [
			Setting("device.name", "Device name (online)", "info", lambda: get_client_config().get("client_id") or "-"),
			Setting("device.mode", "Mode", "info", lambda: "Offline" if is_offline_mode() else "Online"),
		],
	}
	return [hotspot, site, tracking, camera, safety, device]


def _all() -> Dict[str, Setting]:
	return {item.key: item for section in sections() for item in section["items"]}


def describe() -> Dict[str, Any]:
	return {"sections": [{"id": s["id"], "title": s["title"], "note": s.get("note", ""),
		"items": [item.describe() for item in s["items"]]} for s in sections()]}


def apply(changes: Any) -> Dict[str, Any]:
	if not isinstance(changes, dict) or not changes:
		return {"error": "Send the settings to change, as {key: value}"}
	known = _all()
	applied: List[str] = []
	errors: Dict[str, str] = {}
	notices: List[str] = []
	# The hotspot last: it may restart the Wi-Fi this reply goes out on
	for key in sorted(changes, key=lambda k: k.startswith("hotspot.")):
		setting = known.get(key)
		if setting is None or setting.set is None:
			errors[key] = "Not a setting that can be changed"
			continue
		try:
			error = setting.set(changes[key])
		except Exception as exc:
			log.warning("Setting %s: %s", key, exc)
			error = str(exc)[:80]
		if error:
			errors[key] = error
			continue
		applied.append(key)
		if setting.notice is not None:
			notice = setting.notice()
			if notice and notice not in notices:
				notices.append(notice)
	if applied:
		log.info("Settings changed remotely: %s", ", ".join(applied))
		_refresh_panel()
	return {"applied": applied, "errors": errors, "notice": " ".join(notices), "settings": describe()}


def _refresh_panel() -> None:
	try:
		from utils.panel_ui import get_panel_ui

		get_panel_ui().refresh_values()
	except Exception:
		pass
