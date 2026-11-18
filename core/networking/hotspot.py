"""The Pi's own Wi-Fi, for phones, in offline mode.

In offline mode (with hotspot "enabled"), the Pi stops joining Wi-Fi and
broadcasts its own: its "name", which is also the telescope's name on the
phone, with the WPA2 "password". All three are in config/hotspot.json, which
can be changed by hand (the client notices within a few seconds), on the
front panel (Setup) or from the phone (Telescope Settings), and the name and
password are shown on the panel.
Phones that join get an address from the Pi, and the seleno.org app
installed on them talks to the telescope directly, over HTTPS at its own name
(core/networking/phone_link.py). There's no internet on it: the Pi offers no
gateway, so phones keep their mobile data for that.

Switching the Wi-Fi needs root: deploy/seleno-hotspot does it through sudo,
once deploy/hotspot-setup.sh has installed it (run that once, with sudo).
Without it the panel says so and the Wi-Fi is left alone.

The Pi has one Wi-Fi radio: with the hotspot on it isn't on any other Wi-Fi,
so SSH over Wi-Fi goes too. Join the hotspot to reach it (ssh <user>@10.42.0.1),
or use ethernet.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from utils.config_state import get_client_config, get_section, hotspot_file, is_offline_mode, load_hotspot_file, save_section
from utils.log import get_logger

log = get_logger("hotspot")

HELPER = "/usr/local/sbin/seleno-hotspot"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,31}")
PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # nothing to confuse on a small screen
APPLY_DELAY_S = 2.0  # a change from the phone: its reply goes out first
WATCH_S = 3.0  # how often config/hotspot.json is checked for changes by hand

state: Dict[str, str] = {"state": "off", "detail": ""}
_lock = threading.Lock()
_timer: Optional[threading.Timer] = None
_active_config: Optional[Dict[str, Any]] = None  # what the hotspot is running with
_seen_file: Optional[Dict[str, Any]] = None  # config/hotspot.json, as last saved or read
_watcher: Optional[threading.Thread] = None


def settings() -> Dict[str, Any]:
	return get_section("hotspot")


# --- name, password -------------------------------------------------------------

def validate_name(value: str) -> Optional[str]:
	value = str(value).strip()
	if not NAME_RE.fullmatch(value):
		return "1-32 letters, numbers, space . _ -"
	return None


def validate_password(value: str) -> Optional[str]:
	value = str(value)
	if not 8 <= len(value) <= 63:
		return "8 to 63 characters"
	if any(not 32 <= ord(c) <= 126 for c in value):
		return "Plain letters, numbers and symbols only"
	return None


def _default_name() -> str:
	device = str(get_client_config().get("client_id") or "").strip()
	if device and validate_name(device) is None:
		return device
	try:
		with open("/etc/machine-id") as f:
			suffix = str(int(f.read().strip()[-6:], 16) % 10000)
	except (OSError, ValueError):
		suffix = str(secrets.randbelow(10000))
	return f"SELENO-{suffix.zfill(4)}"


def name() -> str:
	"""The hotspot's name, and the telescope's on the phone: the one set, or
	the device name until then (ensure_credentials() keeps it)."""
	value = str(settings().get("name") or "").strip()
	if value and validate_name(value) is None:
		return value
	return _default_name()


def password() -> str:
	"""The one set, or "" until ensure_credentials() makes one up."""
	value = str(settings().get("password") or "")
	return value if validate_password(value) is None else ""


def ensure_credentials() -> None:
	"""Keep the name and make up a password, at startup and whenever one is
	missing or wrong (an edit by hand), so they stay the same from then on and
	config/hotspot.json always shows them. (Not when they're only read:
	reading has no side effects.)"""
	changes: Dict[str, Any] = {}
	saved = settings()
	if validate_name(str(saved.get("name") or "").strip()) is not None:
		changes["name"] = _default_name()
		if saved.get("name"):
			log.warning("Hotspot name in %s isn't usable (%s); using %s", hotspot_file(),
				validate_name(saved["name"]), changes["name"])
	if validate_password(str(saved.get("password") or "")) is not None:
		changes["password"] = "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(10))
		if saved.get("password"):
			log.warning("Hotspot password in %s isn't usable (%s); made a new one", hotspot_file(),
				validate_password(saved["password"]))
	if changes or not os.path.exists(hotspot_file()):
		_save(changes)


def _save(values: Dict[str, Any]) -> None:
	"""Save to config/hotspot.json, and remember it (not taken for a change by
	hand)."""
	global _seen_file
	save_section("hotspot", values)
	_seen_file = load_hotspot_file()


def set_name(value: str, delay_s: float = 0.0) -> Optional[str]:
	value = str(value).strip()
	error = validate_name(value)
	if error:
		return error
	if value == name():
		return None
	_save({"name": value})
	apply(delay_s=delay_s)
	return None


def set_password(value: str, delay_s: float = 0.0) -> Optional[str]:
	value = str(value)
	error = validate_password(value)
	if error:
		return error
	if value == password():
		return None
	_save({"password": value})
	apply(delay_s=delay_s)
	return None


def enabled() -> bool:
	value = settings().get("enabled", True)
	if isinstance(value, str):  # an edit by hand: "off", "false", "no"...
		return value.strip().lower() not in ("", "0", "off", "false", "no")
	return bool(value)


def set_enabled(on: bool) -> Optional[str]:
	_save({"enabled": bool(on)})
	apply()
	if not on:
		return "Hotspot off"
	return "Hotspot on" if is_offline_mode() else "On in offline mode"


def wanted() -> bool:
	"""Offline mode with the hotspot on."""
	return is_offline_mode() and enabled()


# --- what the panel shows --------------------------------------------------------------

def address() -> str:
	return str(settings().get("address") or "10.42.0.1")


def wifi_text() -> str:
	return f"{name()} / {password() or 'made when it starts'}"


def status_text() -> str:
	detail = state.get("detail")
	return f"{state['state']}: {detail}"[:40] if detail and state["state"] != "on" else state["state"]


def helper_installed() -> bool:
	# Never while tests run (SELENO_TESTS, set by tests/helpers.py and the
	# panel tests): a test turning offline mode on would switch the real Wi-Fi
	if os.environ.get("SELENO_TESTS"):
		return False
	return os.path.exists(HELPER)


# --- switching ----------------------------------------------------------------------------

def _config() -> Dict[str, Any]:
	s = settings()
	try:
		channel = max(1, min(13, int(s.get("channel", 6))))
	except (TypeError, ValueError):
		channel = 6
	return {"ssid": name(), "password": password(), "country": str(s.get("country") or "GB").upper()[:2],
		"channel": channel, "interface": str(s.get("interface") or "wlan0"), "address": address(),
		"dns_names": dns_names()}


def dns_names() -> List[str]:
	"""The names the hotspot answers itself (no internet, so no other DNS):
	tel.<domain>, which holds the telescope's own name from seleno.org."""
	from utils import phone_credentials

	creds = phone_credentials.load() or {}
	host = str(creds.get("host") or "")
	if "." in host:
		return [host.split(".", 1)[1].lower()]
	base = str(get_client_config().get("base_url") or "")
	domain = re.sub(r"^https?://", "", base).split("/")[0].split(":")[0].lower()
	return [f"tel.{domain}"] if "." in domain else []


def _run(action: str, payload: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> Optional[str]:
	"""Run the root helper; returns an error message, or None. The password
	goes on stdin, not the command line (where ps would show it)."""
	try:
		result = subprocess.run(["sudo", "-n", HELPER, action], input=json.dumps(payload or {}),
			capture_output=True, text=True, timeout=timeout)
	except subprocess.TimeoutExpired:
		return "the Wi-Fi didn't switch in time"
	except OSError as exc:
		return str(exc)[:60]
	if result.returncode != 0:
		text = (result.stderr or result.stdout or "").strip().splitlines()
		message = text[-1] if text else f"exit {result.returncode}"
		if "password is required" in message or "not allowed" in message:
			return "run deploy/hotspot-setup.sh with sudo"
		return message[:80]
	return None


def helper_status() -> Dict[str, Any]:
	if not helper_installed():
		return {"active": False}
	try:
		result = subprocess.run(["sudo", "-n", HELPER, "status"], input="{}", capture_output=True, text=True,
			timeout=10)
		return json.loads(result.stdout or "{}")
	except (OSError, ValueError, subprocess.TimeoutExpired):
		return {"active": False}


def _up() -> None:
	global _active_config
	config = _config()
	if state["state"] == "on" and config == _active_config:
		return
	if not helper_installed():
		if state["state"] != "not set up":
			log.warning("Hotspot wanted, but %s isn't installed (run deploy/hotspot-setup.sh with sudo)", HELPER)
		state.update(state="not set up", detail="run deploy/hotspot-setup.sh")
		return
	ensure_credentials()
	config = _config()
	state.update(state="starting", detail="")
	log.info("Hotspot: starting %s on %s", config["ssid"], config["interface"])
	error = _run("up", config)
	if error:
		state.update(state="failed", detail=error)
		_active_config = None
		log.warning("Hotspot didn't start: %s", error)
		return
	_active_config = config
	state.update(state="on", detail="")
	log.info("Hotspot on: %s at %s", config["ssid"], config["address"])


def _down(force: bool = False) -> None:
	global _active_config
	if not force and state["state"] in ("off", "not set up"):
		return
	if helper_installed():
		error = _run("down", {"interface": str(settings().get("interface") or "wlan0")})
		if error:
			log.warning("Hotspot didn't stop cleanly: %s", error)
	_active_config = None
	state.update(state="off", detail="")
	log.info("Hotspot off: back on the ordinary Wi-Fi")


def _apply_now() -> None:
	from core.networking import phone_link

	with _lock:
		try:
			if wanted():
				_up()
			else:
				_down()
		except Exception as exc:
			log.exception("Hotspot: %s", exc)
			state.update(state="failed", detail=str(exc)[:60])
		phone_link.apply()


def apply(delay_s: float = 0.0) -> None:
	"""Bring the hotspot and the offline server in line with the settings, in
	the background (switching the Wi-Fi takes a few seconds)."""
	global _timer
	if _timer is not None:
		_timer.cancel()
	_timer = threading.Timer(max(0.0, delay_s), _apply_now)
	_timer.daemon = True
	_timer.name = "hotspot"
	_timer.start()


def start() -> None:
	"""At startup: config/hotspot.json gets the name and password (made up the
	first time), and is watched for changes by hand. A hotspot left on by an
	earlier run counts as on (it's switched off if offline mode has been
	turned off since)."""
	global _watcher, _seen_file
	try:
		ensure_credentials()
	except OSError as exc:
		log.warning("Couldn't save %s: %s", hotspot_file(), exc)
	_seen_file = load_hotspot_file()
	if helper_installed() and helper_status().get("active"):
		state.update(state="on", detail="")
		if not wanted():
			with _lock:
				_down(force=True)
	apply()
	if _watcher is None or not _watcher.is_alive():
		_watcher = threading.Thread(target=_watch, name="hotspot-file", daemon=True)
		_watcher.start()


def check_file() -> bool:
	"""Apply config/hotspot.json if it was changed by hand since; True if so."""
	global _seen_file
	current = load_hotspot_file()
	if _seen_file is None:
		_seen_file = current
		return False
	if current == _seen_file:
		return False
	changed = sorted(k for k in set(current) | set(_seen_file) if current.get(k) != _seen_file.get(k))
	_seen_file = current
	log.info("Hotspot: %s changed in %s", ", ".join(changed), hotspot_file())
	ensure_credentials()  # a name or password that won't do is replaced, and saved
	apply()
	return True


def _watch() -> None:
	while True:
		time.sleep(WATCH_S)
		try:
			check_file()
		except Exception as exc:
			log.warning("Hotspot: couldn't check %s: %s", hotspot_file(), exc)


def wait_until_settled(timeout: float = 90.0) -> bool:
	"""For tests and the shutdown path."""
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		timer = _timer
		if (timer is None or not timer.is_alive()) and not _lock.locked():
			return True
		time.sleep(0.1)
	return False
