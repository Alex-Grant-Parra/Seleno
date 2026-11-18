"""What the telescope answers to the seleno.org app on a phone, directly.

On the offline hotspot the phone has no seleno.org: the app's own pages (the
interface, star map and album, cached on the phone) send the requests they'd
send seleno.org to this telescope instead (core/networking/phone_link.py
serves them over HTTPS, with the user's offline pass). So each route here
answers like the server's route of the same path, with what the server would
have asked the telescope done here directly (utils/handlers.function_map):

    /interface/...              controllers/interface.py (TELESCOPE_ACTIONS mirrored
                                in ACTIONS below), capture plans excepted
    /interface/api/...          controllers/interface_api.py (sky, targets, latest frames)
    /interface/telescope_settings/data   controllers/telescope_settings.py
    /track_star, /stop_tracking, /get_tracking_status,
    /api/telescope_position, /api/telescope_dead_zones   controllers/star_map.py
    /album/...                  the album's library, thumbnails and files, from
                                this Pi's photos (stacks and edits need seleno.org)

The star map itself never comes here: it runs in the phone from the app's
cache, and only what it wants tracked does.

handle() returns a Reply; the claims are the checked offline pass:
{"u": seleno.org user id, "n": username, "r": "owner" | "shared" | "booking"}.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils.log import get_logger

log = get_logger("phone_api")

OFFLINE_ONLY = "Needs seleno.org: this works once the telescope and the phone are online"
THUMB_SIZES = {"sm": 360, "md": 1600}
PLANETS = ("mercury", "venus", "mars", "jupiter", "saturn", "uranus", "neptune")
SITE_DECIMALS = 2
GOOD_MINUTES = 60


@dataclass
class Reply:
	status: int = 200
	data: Any = None                 # JSON
	body: Optional[bytes] = None     # or raw bytes
	file: Optional[str] = None       # or a file (ranges supported)
	content_type: str = "application/json"
	headers: Dict[str, str] = field(default_factory=dict)


def ok(data: Any, status: int = 200) -> Reply:
	return Reply(status, data)


def err(message: str, status: int = 200, **extra: Any) -> Reply:
	return Reply(status, {"status": "error", "message": message, **extra})


# --- calling the telescope's own functions -----------------------------------------------

_lane_locks = {"camera": threading.Lock(), "motion": threading.Lock()}


def call(name: str, *args: Any, **kwargs: Any) -> Any:
	"""A function the server would have called, run here. Its lane (camera,
	motion) is held as on the server link, so a photo doesn't race another
	photo; stops and reads don't wait. Errors come back as {"error": ...},
	as the server would see them."""
	from core.networking.websocket import lane_for
	from utils.handlers import function_map

	func = function_map.get(name)
	if func is None:
		return {"error": f"Function '{name}' not found"}
	while args and args[-1] is None:  # the server drops trailing Nones too
		args = args[:-1]
	lock = _lane_locks.get(lane_for(name))
	try:
		if lock is None:
			return func(*args, **kwargs)
		with lock:
			return func(*args, **kwargs)
	except Exception as exc:
		log.warning("%s failed: %s", name, exc)
		return {"error": str(exc)}


def result_error(result: Any) -> Optional[str]:
	if isinstance(result, dict):
		error = result.get("error")
		if error:
			return error if isinstance(error, str) else json.dumps(error)
	return None


def command_reply(result: Any, success_message: Optional[str] = None, **extra: Any) -> Reply:
	error = result_error(result)
	if error:
		return ok({"status": "error", "message": error, "result": result})
	data = {"status": "success", "result": result, **extra}
	if success_message:
		data["message"] = success_message
	return ok(data)


# --- this telescope, as the server lists it -----------------------------------------------

def telescope_name() -> str:
	from core.networking import hotspot

	return hotspot.name()


def telescope_entry() -> Dict[str, Any]:
	return {"id": 1, "telescope_id": telescope_name(), "ip_address": None, "type": "telescope",
		"last_seen": time.time(), "online": True}


def access(claims: Dict[str, Any]) -> Dict[str, bool]:
	role = claims.get("r")
	return {"control": True, "owner": role == "owner", "shared": role == "shared"}


def user_id(claims: Dict[str, Any]) -> str:
	return str(claims.get("u") or "")


# --- /interface/command ----------------------------------------------------------------------

class BadParam(ValueError):
	pass


def param(params: Dict[str, Any], name: str, kind: type = str, required: bool = False, choices=None,
		minimum=None, maximum=None) -> Any:
	"""As the server's _param: one value checked and converted."""
	value = params.get(name)
	if value is None or value == "":
		if required:
			raise BadParam(f"{name} is required")
		return None
	try:
		if kind is bool:
			value = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
		elif kind is float:
			value = float(value)
			if value != value or value in (float("inf"), float("-inf")):
				raise ValueError
		elif kind is int:
			value = int(value)
		else:
			value = str(value).strip()[:200]
	except (TypeError, ValueError):
		raise BadParam(f"{name} must be a {'number' if kind in (int, float) else kind.__name__}")
	if choices is not None and value not in choices:
		raise BadParam(f"{name} must be one of {', '.join(map(str, choices))}")
	if minimum is not None and value < minimum:
		raise BadParam(f"{name} must be at least {minimum}")
	if maximum is not None and value > maximum:
		raise BadParam(f"{name} must be at most {maximum}")
	return value


def _utc_now_iso() -> str:
	from utils import clock

	return clock.now().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


TRACKING_RATES = ("sidereal", "lunar", "solar", "king", "custom")
LIVEVIEW_CODECS = ("auto", "h264", "jpeg")
RECORDING_KINDS = ("video", "timelapse")
JOB_NAMES = ("park", "dither", "solve", "centre", "polar")
CONTROL, OWNER = "control", "owner"


def _set_liveview_options(p: Dict[str, Any]) -> Any:
	options = {k: v for k, v in {
		"codec": param(p, "codec", choices=LIVEVIEW_CODECS),
		"bitrate_kbps": param(p, "bitrate_kbps", int, minimum=100, maximum=20000),
		"gop_s": param(p, "gop_s", float, minimum=0.5, maximum=10),
		"jpeg_quality": param(p, "jpeg_quality", int, minimum=50, maximum=98),
		"max_fps": param(p, "max_fps", float, minimum=0.2, maximum=30),
	}.items() if v is not None}
	return call("setLiveViewOptions", **options)


def _set_dead_zones(p: Dict[str, Any]) -> Any:
	zones = p.get("zones")
	if not isinstance(zones, list):
		raise BadParam("zones must be a list")
	return call("setDeadZones", zones)


# action -> (access level, plain, run(params, user id)); as the server's TELESCOPE_ACTIONS
ACTIONS: Dict[str, Tuple[str, bool, Callable[[Dict[str, Any], str], Any]]] = {
	# Scope
	"park": (CONTROL, False, lambda p, u: call("park")),
	"unpark": (CONTROL, False, lambda p, u: call("unpark")),
	"set_park_here": (CONTROL, False, lambda p, u: call("setParkHere")),
	"track_object": (CONTROL, False, lambda p, u: call("trackObject", param(p, "name", required=True))),
	"get_planets": (CONTROL, True, lambda p, u: call("getPlanets")),
	"set_tracking_rate": (CONTROL, False, lambda p, u: call("setTrackingRate",
		param(p, "rate", required=True, choices=TRACKING_RATES),
		param(p, "custom_arcsec_per_s", float, minimum=-3600, maximum=3600))),
	"estop": (CONTROL, False, lambda p, u: call("espEstop")),
	"clear_estop": (OWNER, False, lambda p, u: call("espClearEstop")),
	"estop_status": (CONTROL, True, lambda p, u: call("espEstopStatus")),
	"solve_and_sync": (CONTROL, False, lambda p, u: call("solveAndSync", param(p, "source"))),
	"centre_target": (CONTROL, False, lambda p, u: call("centreTarget", param(p, "source"))),
	"polar_align": (CONTROL, False, lambda p, u: call("polarAlign",
		param(p, "step_deg", float, minimum=1, maximum=90), param(p, "source"))),
	"job_status": (CONTROL, True, lambda p, u: call("getJobStatus", param(p, "name", required=True, choices=JOB_NAMES))),
	"get_alignment": (CONTROL, True, lambda p, u: call("getAlignment")),
	"clear_alignment": (CONTROL, False, lambda p, u: call("clearAlignment")),
	"mount_status": (CONTROL, True, lambda p, u: call("getMountStatus")),
	"dither": (CONTROL, False, lambda p, u: call("dither", param(p, "arcmin", float, minimum=0, maximum=60))),
	# Live view
	"set_liveview_quality": (CONTROL, False, lambda p, u: call("setLiveViewQuality", param(p, "quality", required=True))),
	"set_liveview_focus": (CONTROL, False, lambda p, u: call("setLiveViewFocus", param(p, "on", bool, required=True))),
	"set_camera_zoom": (CONTROL, False, lambda p, u: call("setCameraZoom",
		param(p, "level", float, required=True, minimum=1, maximum=20),
		param(p, "x", float, minimum=0, maximum=1), param(p, "y", float, minimum=0, maximum=1))),
	"set_liveview_options": (CONTROL, False, lambda p, u: _set_liveview_options(p)),
	"liveview_snapshot": (CONTROL, False, lambda p, u: call("liveViewSnapshot", u)),
	"start_recording": (CONTROL, False, lambda p, u: call("startRecording",
		param(p, "kind", required=True, choices=RECORDING_KINDS), param(p, "interval_s", float, minimum=0.1, maximum=3600))),
	"stop_recording": (CONTROL, False, lambda p, u: call("stopRecording")),
	"recording_status": (CONTROL, True, lambda p, u: call("getRecordingStatus")),
	"liveview_stats": (CONTROL, True, lambda p, u: call("getLiveViewStats")),
	# Camera
	"camera_status": (CONTROL, True, lambda p, u: call("getCameraStatus")),
	# Uploads (to seleno.org, once online)
	"upload_queue": (CONTROL, True, lambda p, u: call("getUploadQueue")),
	"upload_pending": (CONTROL, False, lambda p, u: call("uploadPending",
		param(p, "kind", choices=("raw", "jpeg", "video", "all")) or "raw")),
	"retry_failed_uploads": (CONTROL, False, lambda p, u: call("retryFailedUploads")),
	"upload_offline_photos": (CONTROL, False, lambda p, u: call("uploadOfflinePhotos", u)),
	# Device: the owner's
	"client_status": (OWNER, True, lambda p, u: call("getClientStatus")),
	"set_clock": (OWNER, False, lambda p, u: call("setClock", param(p, "utc") or _utc_now_iso())),
	"set_horizon": (OWNER, False, lambda p, u: call("setHorizon",
		param(p, "min_alt_deg", float, minimum=-10, maximum=90), p.get("profile"), param(p, "stop_when_set", bool))),
	"get_dead_zones": (CONTROL, True, lambda p, u: call("getDeadZones")),
	"set_dead_zones": (OWNER, False, lambda p, u: _set_dead_zones(p)),
	"esp_info": (OWNER, True, lambda p, u: call("espInfo")),
	"esp_driver_status": (OWNER, True, lambda p, u: call("espDriverStatus")),
	"update_client": (OWNER, False, lambda p, u: {"error": OFFLINE_ONLY}),
	"update_firmware": (OWNER, False, lambda p, u: call("updateFirmware")),
}
JOB_ACTIONS = {"park": "park", "dither": "dither", "solve_and_sync": "solve", "centre_target": "centre",
	"polar_align": "polar"}


def _owner_only(claims: Dict[str, Any]) -> Optional[Reply]:
	if claims.get("r") != "owner":
		return err("Only the telescope's owner can do that.", 403)
	return None


def telescope_command(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	action = body.get("action")
	spec = ACTIONS.get(action)
	if spec is None:
		return err(f"Unknown action: {action}", 400)
	level, plain, run = spec
	if level == OWNER:
		refused = _owner_only(claims)
		if refused:
			return refused
	params = body.get("params") or {}
	if not isinstance(params, dict):
		return err("params must be an object", 400)
	try:
		result = run(params, user_id(claims))
	except BadParam as exc:
		return err(str(exc), 400)
	if plain:
		if isinstance(result, dict) and set(result) == {"error"}:
			return ok({"status": "error", "message": result_error(result), "result": result})
		return ok({"status": "success", "result": result})
	if action in JOB_ACTIONS:
		return command_reply(result, job=JOB_ACTIONS[action])
	return command_reply(result)


MOTOR_COMMANDS = {
	"enable": ("espEnable", [bool]), "set_direction": ("espSetDirection", [bool]), "set_speed": ("espSetSpeed", [float]),
	"start": ("espStart", [float, bool]), "move_steps": ("espMoveSteps", [int, float, bool]), "stop": ("espStop", []),
	"set_microsteps": ("espSetMicrosteps", [int]), "set_current": ("espSetCurrent", [int]),
	"set_mode": ("espSetMode", [str]), "set_accel": ("espSetAccel", [float]), "status": ("espStatus", []),
	"status_all": ("espStatusAll", []),
}


def motor_command(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	command = body.get("command")
	if not command:
		return err("No command specified", 400)
	spec = MOTOR_COMMANDS.get(command)
	if spec is None:
		return err(f"Unknown motor command: {command}", 400)
	name, kinds = spec
	if command == "status_all":
		return command_reply(call(name), f"Motor command '{command}' executed successfully")
	args = body.get("args")
	args = [] if args is None else (list(args) if isinstance(args, list) else [args])
	try:
		args = [kind(value) for kind, value in zip(kinds, args) if value is not None]
	except (TypeError, ValueError):
		return err("Bad motor arguments", 400)
	result = call(name, *args, motor_id=str(body.get("motor_id") or "motor1"))
	return command_reply(result, f"Motor command '{command}' executed successfully")


def get_motors(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	result = call("espStatusAll")
	motors = list(result.keys()) if isinstance(result, dict) and result and "error" not in result else ["motor1"]
	return ok({"status": "success", "motors": motors})


# --- camera and live view ----------------------------------------------------------------------

CAMERA_SETTING_LABELS = {
	"shutterSpeed": "/main/capturesettings/shutterspeed",
	"iso": "/main/imgsettings/iso",
	"aperture": "/main/capturesettings/aperture",
	"whiteBalance": "/main/imgsettings/whitebalance",
	"imageFormat": "/main/imgsettings/imageformat",
	"exposureCompensation": "/main/capturesettings/exposurecompensation",
}


def update_camera(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	if "photoFormat" in body and "imageFormat" not in body:
		body["imageFormat"] = body["photoFormat"]
	changed, errors = [], []
	for key, label in CAMERA_SETTING_LABELS.items():
		value = body.get(key)
		if value in (None, ""):
			continue
		error = result_error(call("setCameraSetting", label, value))
		if error:
			errors.append(f"{key}: {error}")
		else:
			changed.append(key)
	if errors:
		return ok({"status": "error", "message": "; ".join(errors), "changed": changed})
	if not changed:
		return err("No camera setting given", 400)
	return ok({"status": "success", "message": "Settings updated", "changed": changed})


def camera_choices(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	choices = call("getCameraChoices")
	error = result_error(choices)
	return err(error) if error else ok(choices)


def take_photo(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	exposure_s = body.get("exposure_s")
	if exposure_s not in (None, ""):
		try:
			exposure_s = float(exposure_s)
		except (TypeError, ValueError):
			return err("exposure_s must be a number", 400)
		if not 0 < exposure_s <= 900:
			return err("Bulb exposures run from 0 to 900 s", 400)
	else:
		exposure_s = None
	frame_type = body.get("frame_type")
	if frame_type not in (None, "", "test", "light", "snapshot"):
		return err("frame_type must be test, light or snapshot", 400)
	# Under the user's own seleno.org account: kept here, and uploaded to it once online
	result = call("capturePhoto", user_id(claims), exposure_s, frame_type=frame_type or "light")
	return command_reply(result, "Photo taken")


def start_live_view(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	return command_reply(call("startLiveView"), "Live view started")


def stop_live_view(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	return command_reply(call("stopLiveView"), "Live view stopped")


# --- the telescope's status ------------------------------------------------------------------

def telescope_status(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	from core.networking import phone_link
	from utils.client_status import collect

	return ok({
		"status": "success",
		"telescope_id": telescope_name(),
		"age_s": 0.0,
		"telescope_status": collect(),
		"viewers": phone_link.viewer_count(),
		"plan": None,
		"access": access(claims),
		"booking": None,
		"next_booking_start": None,
	})


# --- the sky, targets and search ---------------------------------------------------------------

def _utcnow() -> datetime:
	from utils import clock

	return clock.now().astimezone(timezone.utc).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
	return value.isoformat() + "Z" if value else None


def site() -> Optional[Tuple[float, float]]:
	from core.hardware.tracking import _site

	return _site()


def _no_site() -> Reply:
	return ok({"status": "success", "site": None,
		"message": "This telescope's location isn't known yet: set it on its panel (Scope > Site) or in Telescope Settings."})


def planet_positions(when: datetime) -> Dict[str, Tuple[float, float]]:
	from utils import ephemeris

	out = {}
	for entry in ephemeris.positions(when.replace(tzinfo=timezone.utc), site()):
		out[str(entry["name"]).lower()] = (float(entry["ra"]) % 360.0, float(entry["dec"]))
	return out


def resolve_target(name: str, ra: Optional[float] = None, dec: Optional[float] = None,
		when: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
	"""{name, ra, dec} for a designation, a planet, the Moon or coordinates."""
	from utils import offline_pack

	when = when or _utcnow()
	if ra is not None and dec is not None:
		return {"name": name or "Target", "ra": ra % 360.0, "dec": max(-90.0, min(90.0, dec))}
	if not name:
		return None
	key = name.strip().lower()
	if key == "moon" or key in PLANETS:
		positions = planet_positions(when)
		if key in positions:
			return {"name": key.capitalize(), "ra": positions[key][0], "dec": positions[key][1], "moving": True}
		return None
	found = offline_pack.lookup(name)
	if found is None:
		return None
	return {"name": found["name"], "ra": float(found["ra"]), "dec": float(found["dec"])}


def _float(value: Any) -> Optional[float]:
	try:
		number = float(value)
	except (TypeError, ValueError):
		return None
	return number if number == number else None


def sky_now(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	from utils import sky_tonight

	here = site()
	if here is None:
		return _no_site()
	lat, lon = here
	when = _utcnow()
	target = None
	name = (query.get("target") or "").strip()[:120]
	ra, dec = _float(query.get("ra")), _float(query.get("dec"))
	if name or (ra is not None and dec is not None):
		label = (query.get("label") or "").strip()[:120]
		target = resolve_target(name or label, ra, dec, when)
		if target is None:
			return err(f"Couldn't find {name}", 404)
	min_alt = _float(query.get("min_alt"))
	min_alt = 30.0 if min_alt is None else max(0.0, min(80.0, min_alt))
	summary = sky_tonight.sky_summary(lat, lon, when, target=target, min_alt=min_alt)
	summary["site"] = {"lat": round(lat, SITE_DECIMALS), "lon": round(lon, SITE_DECIMALS), "source": "telescope"}
	return ok({"status": "success", **summary})


def targets_now(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	"""Basic mode's suggestions, from the featured targets seleno.org gave the
	telescope (utils/offline_pack.py), worked out for tonight as it would."""
	from utils import offline_pack, sky_tonight

	here = site()
	when = _utcnow()
	planets: Dict[str, Tuple[float, float]] = {}
	try:
		planets = planet_positions(when)
	except Exception:
		log.exception("Planet positions failed")
	jd = sky_tonight.julian_day(when)
	moon_ra, moon_dec, _unused, _unused2 = sky_tonight.moon_position(jd)
	night = moon = None
	if here is not None:
		lat, lon = here
		night = sky_tonight.night_window(lat, lon, when)
		fraction, _unused, phase = sky_tonight.moon_phase(jd)
		moon = {"alt": round(sky_tonight.moon_altitude(lat, lon, jd), 1), "illumination": round(fraction, 3), "phase": phase}
	out = []
	for target in offline_pack.featured_targets():
		key = str(target.get("designation") or "").lower()
		if key == "moon":
			coords: Optional[Tuple[float, float]] = (moon_ra, moon_dec)
		elif key in planets:
			coords = planets[key]
		elif target.get("ra") is not None and target.get("dec") is not None:
			coords = (float(target["ra"]), float(target["dec"]))
		else:
			coords = None
		if coords is None:
			continue
		entry = {k: target.get(k) for k in ("designation", "name", "track_name", "kind", "preset", "size_arcmin",
			"blurb", "wiki")}
		entry.update(ra=round(coords[0], 4), dec=round(coords[1], 4), moving=key == "moon" or key in planets)
		if here is not None:
			info = sky_tonight.target_tonight(coords[0], coords[1], lat, lon, when, night=night)
			info["moon_separation"] = round(sky_tonight.separation_deg(coords[0], coords[1], moon_ra, moon_dec), 1)
			entry.update({
				"alt": info["alt"], "rising": info["rising"], "best_alt": info["best_alt"],
				"best_time": _iso(info["best_time"]), "transit": _iso(info["transit"]),
				"minutes_above": info["minutes_above"], "moon_separation": info["moon_separation"],
				"never_rises": bool(info["never_rises"]), "good_tonight": bool(info["minutes_above"] >= GOOD_MINUTES),
				"verdict": sky_tonight.verdict(info, {"moon": moon, "sun": {"alt": sky_tonight.sun_altitude(lat, lon, jd)}}),
			})
		out.append(entry)
	if here is not None:
		def score(t: Dict[str, Any]) -> tuple:
			up_now = t["alt"] >= 20
			later = (t["best_alt"] or -90) >= 25
			return (not t["good_tonight"], 0 if up_now else 1 if later else 2, -(t["minutes_above"] or 0), -t["alt"])
		out.sort(key=score)
	return ok({
		"status": "success",
		"site": None if here is None else {"lat": round(here[0], SITE_DECIMALS), "lon": round(here[1], SITE_DECIMALS)},
		"night": None if night is None else {"start": _iso(night[0]), "end": _iso(night[1])},
		"moon": moon,
		"targets": out,
	})


def search_object(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	"""As the server's /interface/search_object, over what the telescope
	knows offline: the planets and the Moon, the stars of its sky view and the
	named objects seleno.org gave it (utils/offline_pack.py)."""
	from utils import offline_pack

	pick = body.get("pick") if isinstance(body.get("pick"), dict) else None
	query = str((pick or {}).get("key") or body.get("searchValue") or "").strip()[:120]
	if not query:
		return err("Object not found")
	key = query.lower()
	if key == "moon" or key in PLANETS or key == "sun":
		if key == "sun":
			return err("The Sun can't be tracked from here")
		now = _utcnow()
		positions = planet_positions(now)
		if key not in positions:
			return err("Object not found")
		ra, dec = positions[key]
		data = {"Name": key.capitalize(), "RA": ra, "DEC": dec, "V-Mag": None, "label": key.capitalize(),
			"kind": "body", "alternatives": []}
		return ok({"status": "success", "data": data})
	matches = offline_pack.search(query, limit=5)
	if not matches:
		return err("Object not found")
	best, rest = matches[0], matches[1:]
	if pick is None and best["match"] == "fuzzy" and rest and rest[0]["match"] == "fuzzy":
		return ok({"status": "choose", "query": query, "matches": [
			{"kind": "catalog", "key": m["name"], "name": m["label"], "matched": m["matched"], "match": m["match"]}
			for m in matches]})
	data = {"Name": best["name"], "RA": best["ra"], "DEC": best["dec"], "V-Mag": best.get("mag"), "kind": "catalog",
		"match": {"query": query, "matched": best["matched"], "match": best["match"]},
		"alternatives": [{"kind": "catalog", "key": m["name"], "name": m["label"], "matched": m["matched"],
			"match": m["match"]} for m in rest]}
	if best["label"] != best["name"]:
		data["friendlyName"] = best["label"]
	return ok({"status": "success", "data": data})


# --- the star map's telescope requests ----------------------------------------------------------

_tracking: Dict[str, Dict[str, Any]] = {}  # user id -> what the star map asked to track


def track_star(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	ra, dec, name, mag = body.get("ra"), body.get("dec"), body.get("name"), body.get("mag")
	if ra is None or dec is None:
		return ok({"error": "Missing RA/DEC"}, 400)
	key = str(name or "").strip().lower()
	if key == "moon" or key in PLANETS:
		result = call("trackObject", str(name).strip())
	else:
		try:
			ra, dec = float(ra), float(dec)
		except (TypeError, ValueError):
			return ok({"error": "RA/DEC must be numbers"}, 400)
		result = call("trackCoordinates", name, ra, dec, mag)
	refusal = result_error(result)
	if refusal:
		return ok({"status": "error", "error": refusal, "message": refusal, "telescope_id": telescope_name(),
			"result": result})
	_tracking[user_id(claims)] = {"name": name, "ra": ra, "dec": dec, "mag": mag}
	return ok({"status": "tracking", "ra": ra, "dec": dec, "telescope_id": telescope_name(), "result": result,
		"redirect": True})


def stop_tracking(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	result = call("stopTracking")
	_tracking.pop(user_id(claims), None)
	refusal = result_error(result)
	if refusal:
		return ok({"status": "error", "error": refusal, "message": refusal, "telescope_id": telescope_name(),
			"result": result})
	return ok({"status": "stopped", "message": "Tracking stopped successfully", "telescope_id": telescope_name(),
		"result": result})


def tracking_status(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	selected = _tracking.get(user_id(claims))
	return ok({"status": "success", "tracking": bool(selected), "object": selected})


def telescope_position(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	coords = call("getCurrentCoordinates")
	try:
		ra = float(coords["current_right_ascension"])
		dec = float(coords["current_declination"])
	except (KeyError, TypeError, ValueError):
		return ok({"status": "error", "error": "No coordinates", "message": result_error(coords) or "No coordinates",
			"telescope_id": telescope_name()}, 500)
	return ok({"status": "success", "ra": ra, "dec": dec, "telescope_id": telescope_name()})


def telescope_dead_zones(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	zones = call("getDeadZones")
	here = site()
	return ok({"status": "success", "telescope_id": telescope_name(), "reported": True,
		"zones": (zones or {}).get("zones", []) if isinstance(zones, dict) else [],
		"site": {"lat": here[0], "lon": here[1]} if here else None, "age_s": 0.0})


# --- settings ------------------------------------------------------------------------------------

def read_settings(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	refused = _owner_only(claims)
	if refused:
		return refused
	return ok({"status": "success", "telescope_id": telescope_name(), "settings": call("getSettings")})


def save_settings(body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	refused = _owner_only(claims)
	if refused:
		return refused
	changes = body.get("changes")
	if not isinstance(changes, dict) or not changes:
		return err("Nothing to change", 400)
	result = call("setSettings", changes)
	error = result_error(result)
	if error:
		return ok({"status": "error", "message": error, "result": result})
	return ok({"status": "success", "result": result})


# --- the album: this Pi's photos ---------------------------------------------------------------

RAW_EXTENSIONS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".orf", ".rw2", ".dng", ".raf", ".pef", ".srw"}
JPEG_EXTENSIONS = {".jpg", ".jpeg"}
VIDEO_EXTENSIONS = {".avi", ".ser", ".mp4"}
IMAGE_EXTENSIONS = {".png", ".tif", ".tiff", ".fits", ".fit"}
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".tif": "image/tiff", ".tiff": "image/tiff",
	".avi": "video/x-msvideo", ".mp4": "video/mp4", ".fits": "application/fits", ".fit": "application/fits"}


def _kind(name: str) -> Optional[str]:
	ext = os.path.splitext(name)[1].lower()
	if ext in JPEG_EXTENSIONS:
		return "jpeg"
	if ext in RAW_EXTENSIONS:
		return "raw"
	if ext in VIDEO_EXTENSIONS:
		return "video"
	if ext in IMAGE_EXTENSIONS:
		return "image"
	return None


def photo_name(relative: str) -> str:
	"""A file's name in the album: its path under photos/, with "~" for "/"
	(the album puts names in URLs and shows them)."""
	return relative.replace(os.sep, "~")


def photo_path(name: str) -> Optional[str]:
	from utils import photos

	base = os.path.realpath(photos.base_dir())
	path = os.path.realpath(os.path.join(base, name.replace("~", os.sep)))
	if not path.startswith(base + os.sep) or not os.path.isfile(path) or "/." in path[len(base):]:
		return None
	return path


def _scan() -> List[Dict[str, Any]]:
	"""Every photo, video and RAW on the Pi with what photos.jsonl says."""
	from utils import photos

	base = photos.base_dir()
	found: List[Dict[str, Any]] = []
	for root, dirs, files in os.walk(base):
		dirs[:] = [d for d in dirs if not d.startswith(".")]
		records: Dict[str, Dict[str, Any]] = {}
		try:
			with open(os.path.join(root, photos.LOG_NAME)) as f:
				for line in f:
					try:
						entry = json.loads(line)
					except ValueError:
						continue
					for file_name in entry.get("files") or []:
						records[file_name] = entry
		except OSError:
			pass
		for file_name in files:
			kind = _kind(file_name)
			if kind is None or file_name.startswith("."):
				continue
			path = os.path.join(root, file_name)
			try:
				stat = os.stat(path)
			except OSError:
				continue
			found.append({"path": path, "rel": os.path.relpath(path, base), "file": file_name, "kind": kind,
				"stat": stat, "record": records.get(file_name, {})})
	return found


def album_library(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	"""The album's frames, as /album/api/library lists them, from this Pi.
	A RAW with a JPEG of the same capture shows as the JPEG's RAW."""
	items = _scan()
	by_stem = {}
	for item in items:
		if item["kind"] == "jpeg":
			by_stem[os.path.splitext(item["path"])[0].lower()] = item
	raws_with_jpeg = set()
	for item in items:
		if item["kind"] == "raw":
			jpeg = by_stem.get(os.path.splitext(item["path"])[0].lower())
			if jpeg is not None:
				jpeg["raw_item"] = item
				raws_with_jpeg.add(item["path"])
	frames = []
	for item in items:
		if item["path"] in raws_with_jpeg:
			continue
		record = item["record"]
		name = photo_name(item["rel"])
		version = int(item["stat"].st_mtime)
		raw = item.get("raw_item")
		captured = record.get("captured") or record.get("time")
		if not captured:
			captured = datetime.fromtimestamp(item["stat"].st_mtime, timezone.utc).replace(tzinfo=None).isoformat() + "Z"
		frames.append({
			"name": name,
			"kind": item["kind"],
			"raw": photo_name(raw["rel"]) if raw else None,
			"url": f"/album/photo/{urllib.parse.quote(name)}",
			"raw_url": f"/album/photo/{urllib.parse.quote(photo_name(raw['rel']))}" if raw else None,
			"thumb": f"/album/thumb/{urllib.parse.quote(name)}?size=sm&v={version}",
			"preview": f"/album/thumb/{urllib.parse.quote(name)}?size=md&v={version}",
			"captured_at": captured,
			"exposure_s": record.get("exposure_s"),
			"iso": record.get("iso"),
			"camera": record.get("camera"),
			"width": None, "height": None,
			"bytes": item["stat"].st_size,
			"target": record.get("target"),
			"notes": None,
			"frame_type": record.get("frame_type") or record.get("kind"),
			"frame_type_set": False,
			"flag": None, "rating": None, "metrics": None, "metrics_source": None,
			"ra": record.get("ra"), "dec": record.get("dec"),
			"telescope": telescope_name(),
			"on_telescope": True,
		})
	frames.sort(key=lambda f: f["captured_at"] or "", reverse=True)
	return ok({"status": "success", "frames": frames, "plans": {}, "stacks": 0, "on_telescope": True})


def album_thumb(name: str, query: Dict[str, str]) -> Reply:
	from io import BytesIO

	path = photo_path(name)
	if path is None:
		return err("Not found", 404)
	size = THUMB_SIZES.get(query.get("size", "sm"), THUMB_SIZES["sm"])
	thumbs = os.path.join(os.path.dirname(path), ".thumbs")
	thumb = os.path.join(thumbs, f"{os.path.basename(path)}.{size}.jpg")
	try:
		if not os.path.exists(thumb) or os.path.getmtime(thumb) < os.path.getmtime(path):
			from PIL import Image, ImageOps

			kind = _kind(path)
			if kind == "raw":
				data = _raw_preview(path)
				if data is None:
					return err("No preview in this RAW", 404)
				image = Image.open(BytesIO(data))
			elif kind in ("jpeg", "image"):
				image = Image.open(path)
			else:
				return err("No thumbnail for videos", 404)
			image.draft("RGB", (size, size))
			image = ImageOps.exif_transpose(image).convert("RGB")
			image.thumbnail((size, size))
			os.makedirs(thumbs, exist_ok=True)
			image.save(thumb, "JPEG", quality=82)
	except Exception as exc:
		log.info("No thumbnail for %s: %s", name, exc)
		return err("No thumbnail", 404)
	return Reply(200, file=thumb, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=2592000"})


def _raw_preview(path: str) -> Optional[bytes]:
	"""The JPEG a camera embeds in its RAW: the biggest one in the file."""
	with open(path, "rb") as f:
		data = f.read()
	best = None
	start = data.find(b"\xff\xd8\xff")
	while start != -1:
		end = data.find(b"\xff\xd9", start)
		if end == -1:
			break
		if best is None or end - start > best[1] - best[0]:
			best = (start, end + 2)
		start = data.find(b"\xff\xd8\xff", end)
	return data[best[0]:best[1]] if best and best[1] - best[0] > 10000 else None


def album_photo(name: str, query: Dict[str, str]) -> Reply:
	path = photo_path(name)
	if path is None:
		return err("Not found", 404)
	ext = os.path.splitext(path)[1].lower()
	return Reply(200, file=path, content_type=MIME.get(ext, "application/octet-stream"),
		headers={"Content-Disposition": f'inline; filename="{os.path.basename(path)}"'})


# --- the routes -----------------------------------------------------------------------------------

JsonHandler = Callable[[Dict[str, Any], Dict[str, Any]], Reply]

GET: Dict[str, Callable[[Dict[str, str], Dict[str, Any]], Reply]] = {
	"/interface/get_telescopes": lambda q, c: ok({"status": "success", "telescopes": [telescope_entry()]}),
	"/interface/get_selected_telescope": lambda q, c: ok({"status": "success", "telescope": telescope_entry(),
		"access": access(c)}),
	"/interface/telescope_status": telescope_status,
	"/interface/get_camera_choices": camera_choices,
	"/interface/api/sky": sky_now,
	"/interface/api/targets": targets_now,
	"/interface/telescope_settings/data": read_settings,
	"/get_tracking_status": tracking_status,
	"/api/telescope_position": telescope_position,
	"/api/telescope_dead_zones": telescope_dead_zones,
	"/album/api/library": album_library,
	"/album/api/stacks": lambda q, c: ok({"status": "success", "stacks": []}),
	"/album/api/projects": lambda q, c: ok({"status": "success", "projects": []}),
	"/album/plans": lambda q, c: ok({"status": "success", "plans": []}),
	"/interface/capture/history": lambda q, c: ok({"status": "success", "plans": []}),
	"/interface/capture/saved": lambda q, c: ok({"status": "success", "plans": []}),
	"/interface/capture/presets": lambda q, c: ok({"status": "success", "presets": []}),
}

POST: Dict[str, JsonHandler] = {
	"/interface/select_telescope": lambda b, c: ok({"status": "success", "message": f"Selected telescope: {telescope_name()}",
		"telescope": telescope_entry(), "access": access(c)}),
	"/interface/command": telescope_command,
	"/interface/motor_command": motor_command,
	"/interface/get_motors": get_motors,
	"/interface/update_camera": update_camera,
	"/interface/take_photo": take_photo,
	"/interface/start_live_view": start_live_view,
	"/interface/stop_live_view": stop_live_view,
	"/interface/search_object": search_object,
	"/interface/telescope_settings/data": save_settings,
	"/track_star": track_star,
	"/stop_tracking": stop_tracking,
	"/user/night_mode": lambda b, c: ok({"status": "success"}),
}


def latest_captures(query: Dict[str, str], claims: Dict[str, Any]) -> Reply:
	"""The newest frames, as /interface/api/latest_captures lists them."""
	library = album_library({}, claims).data["frames"]
	try:
		limit = max(1, min(50, int(query.get("limit", 12))))
	except ValueError:
		limit = 12
	since = query.get("since")
	frames = []
	for f in library:
		if f["kind"] == "raw":
			continue
		if since and (f["captured_at"] or "") <= since:
			continue
		frames.append({"name": f["name"], "kind": f["kind"], "frame_type": f["frame_type"], "label": None,
			"target": f["target"], "captured_at": f["captured_at"], "uploaded_at": f["captured_at"],
			"exposure_s": f["exposure_s"], "iso": f["iso"], "plan_id": None, "has_raw": bool(f["raw"]),
			"url": f["url"], "thumb": f["thumb"].split("&v=")[0], "preview": f["preview"].split("&v=")[0],
			"metrics": None})
		if len(frames) >= limit:
			break
	return ok({"status": "success", "frames": frames, "now": _iso(_utcnow())})


GET["/interface/api/latest_captures"] = latest_captures


def handle(method: str, path: str, query: Dict[str, str], body: Dict[str, Any], claims: Dict[str, Any]) -> Reply:
	"""One request from the app."""
	path = path.rstrip("/") or "/"
	if method in ("GET", "HEAD"):
		if path.startswith("/album/thumb/"):
			return album_thumb(urllib.parse.unquote(path[len("/album/thumb/"):]), query)
		if path.startswith("/album/photo/") and not path.endswith("/metadata"):
			return album_photo(urllib.parse.unquote(path[len("/album/photo/"):]), query)
		handler = GET.get(path)
		if handler is not None:
			return handler(query, claims)
	elif method in ("POST", "PUT", "PATCH", "DELETE"):
		handler = POST.get(path) if method == "POST" else None
		if handler is not None:
			return handler(body, claims)
	if path.startswith(("/interface/capture", "/album/")):
		return err(OFFLINE_ONLY, 503)
	return err("Not on the telescope", 404)


# Paths this answers (for the app's service worker, which sends only these here)
def paths() -> Dict[str, List[str]]:
	exact = sorted(set(GET) | set(POST) | {"/interface/liveview_ticket"})
	return {"exact": exact, "prefixes": ["/album/thumb/", "/album/photo/", "/interface/capture/", "/album/api/"]}

