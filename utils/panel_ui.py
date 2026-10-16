"""The Pi's side of the ESP32 front-panel UI (esp32-firmware/src/ui.h).

* hello on every new connection and after an ESP32 reboot: the panel leaves
  its loading screen and gets the Pi's tabs (utils/panel_menu.py);
* a status line every 2 s (clock, IP, server state) and any changed item
  values; it doubles as the link heartbeat the panel watches;
* the panel's events: item changes and actions, prompt answers, the
  double-RETURN stop (stop tracking, then acknowledge so the Pi may move the
  motors again) and sky-view zoom;
* prompts (keyboard, confirm, message, choice) for the first-run setup of the
  device name, server and API key;
* the sky view: while the panel shows it, a projected sky scene
  (utils/sky_view.py) about once a second while slewing, every few seconds
  otherwise.

Callbacks run on worker threads, never on the serial event thread.
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from utils.panel_menu import Panel, build_panels, validate_api_key, validate_device_name, validate_server
from utils.sky_data import get_sky_data
from utils.sky_view import FOV_STEPS, SkyOptions, build_scene

HEARTBEAT_S = 2.0
SKY_FAST_S = 1.0    # while the scope moves
SKY_SLOW_S = 5.0    # while it tracks or sits still


class PromptCancelled(Exception):
	pass


class PanelUI:
	def __init__(self) -> None:
		self._conn = None
		self._lock = threading.RLock()
		self._jobs: "queue.Queue[tuple]" = queue.Queue()
		self._threads_started = False
		self._panels: List[Panel] = []
		self._sent_values: Dict[str, Any] = {}
		self._prompt_ids = itertools.count(1)
		self._prompt_waiters: Dict[str, "queue.Queue[dict]"] = {}
		self._wizard_running = False
		self._status: Dict[str, Any] = {}
		self._tracking: Dict[str, Any] = {"state": "idle"}
		self._sky_dirty = True
		self._sky_sent_at = 0.0
		self._stop_queued = threading.Event()
		self.sky_options = self._load_sky_options()
		self.attached = threading.Event()

	# --- connection ---------------------------------------------------------------

	def attach(self, conn) -> None:
		"""Called for every new ESP32Connection (utils/esp32_state.py)."""
		with self._lock:
			self._conn = conn
		conn.add_event_listener(self._on_ui_event, "ui")
		conn.add_event_listener(lambda ev: self._post("hello", self._hello), "boot")
		self._start_threads()
		self._post("hello", self._hello)

	def _start_threads(self) -> None:
		if self._threads_started:
			return
		self._threads_started = True
		threading.Thread(target=self._work_loop, name="panel-ui", daemon=True).start()
		threading.Thread(target=self._heartbeat_loop, name="panel-heartbeat", daemon=True).start()
		threading.Thread(target=self._sky_loop, name="panel-sky", daemon=True).start()

	def _send(self, action: str, **fields: Any) -> Optional[Dict[str, Any]]:
		conn = self._conn
		if conn is None:
			return None
		try:
			return conn.ui(action, timeout=2.0, **fields)
		except Exception as exc:
			print(f"[panel] ui {action} failed: {exc}")
			return None

	def _post(self, name: str, fn: Callable[[], None]) -> None:
		self._jobs.put((name, fn))

	def _work_loop(self) -> None:
		while True:
			name, fn = self._jobs.get()
			try:
				fn()
			except Exception as exc:
				print(f"[panel] {name} failed: {exc}")

	# --- hello, status, menus -----------------------------------------------------

	def _client_config(self) -> Dict[str, Any]:
		from utils.config_state import get_client_config

		return get_client_config()

	def _hello(self) -> None:
		self._panels = build_panels(self)
		reply = self._send(
			"hello",
			device=self._client_config().get("client_id", ""),
			status=self._status_fields(),
			panels=[p.to_json() for p in self._panels],
		)
		if reply is None:
			return
		self._sent_values = {f"{p.id}.{i.id}": i.to_json().get("value") for p in self._panels for i in p.items}
		self._note_reply(reply)
		self.attached.set()
		self._send("tracking", **self._tracking)
		print(f"[panel] Front panel attached ({reply.get('width')}x{reply.get('height')})")
		if reply.get("panel_stop_pending"):
			self._queue_panel_stop()
		if self.missing_settings():
			self.start_wizard()

	def _status_fields(self) -> Dict[str, Any]:
		from utils import pi_system

		now = datetime.now()
		return {
			"clock_s": now.hour * 3600 + now.minute * 60 + now.second,
			"ip": pi_system.ip_address(),
			"server": self._server_state(),
		}

	@staticmethod
	def _server_state() -> str:
		try:
			from core.networking.websocket import server_state

			return server_state.get("state", "?")
		except Exception:
			return "?"

	def _note_reply(self, reply: Dict[str, Any]) -> None:
		with self._lock:
			self._status.update(reply)

	def _heartbeat_loop(self) -> None:
		while True:
			time.sleep(HEARTBEAT_S)
			if self._conn is None or not self.attached.is_set():
				continue
			reply = self._send("status", **self._status_fields())
			if reply is None:
				continue
			self._note_reply(reply)
			if reply.get("panel_stop_pending"):
				self._queue_panel_stop()
			self._push_values()

	def _push_values(self) -> None:
		changed: Dict[str, Any] = {}
		for panel in self._panels:
			for item in panel.items:
				if item.get is None:
					continue
				try:
					value = item.to_json().get("value")
				except Exception:
					continue
				key = f"{panel.id}.{item.id}"
				if self._sent_values.get(key) != value:
					changed[key] = value
		if changed and self._send("set", values=changed) is not None:
			self._sent_values.update(changed)

	def refresh_values(self) -> None:
		"""Send current values now (after a change from elsewhere)."""
		self._post("values", self._push_values)

	# --- events from the panel ----------------------------------------------------

	def _on_ui_event(self, event: Dict[str, Any]) -> None:
		kind = event.get("type")
		if kind == "prompt":
			waiter = self._prompt_waiters.get(str(event.get("prompt")))
			if waiter is not None:
				waiter.put(event)
		elif kind in ("change", "action"):
			self._post(f"ui-{kind}", lambda: self._handle_item(event))
		elif kind == "stop":
			self._queue_panel_stop()
		elif kind == "sky":
			self._post("sky-zoom", lambda: self._zoom(int(event.get("zoom", 0))))

	def _find_item(self, panel_id: str, item_id: str):
		for panel in self._panels:
			if panel.id == panel_id:
				for item in panel.items:
					if item.id == item_id:
						return item
		return None

	def _handle_item(self, event: Dict[str, Any]) -> None:
		panel_id, item_id = str(event.get("panel")), str(event.get("item"))
		item = self._find_item(panel_id, item_id)
		if item is None:
			return
		message: Optional[str] = None
		try:
			if event.get("type") == "action" and item.run is not None:
				message = item.run()
			elif item.set is not None:
				message = item.set(event.get("value"))
		except Exception as exc:
			message = str(exc)
		if message:
			self.toast(message)
		key = f"{panel_id}.{item_id}"
		if item.get is not None:
			# Confirm the value (or snap it back if the setter refused it).
			value = item.to_json().get("value")
			if self._send("set", values={key: value}) is not None:
				self._sent_values[key] = value

	def _queue_panel_stop(self) -> None:
		# The event, hello and the heartbeat can all report the same stop.
		if not self._stop_queued.is_set():
			self._stop_queued.set()
			self._post("panel-stop", self._handle_panel_stop)

	def _handle_panel_stop(self) -> None:
		"""RETURN was pressed twice on the panel: the ESP32 has stopped the
		motors and holds host moves until we acknowledge."""
		print("[panel] Stopped from the front panel: stopping tracking")
		try:
			from core.hardware.tracking import stop_tracking

			stop_tracking()
		except Exception as exc:
			print(f"[panel] stop_tracking failed: {exc}")
		self._send("ack_stop")
		self._stop_queued.clear()

	# --- prompts -----------------------------------------------------------------

	def toast(self, text: str, ms: int = 2500) -> None:
		self._send("toast", text=text, ms=ms)

	def prompt(self, kind: str, title: str, text: str = "", timeout_s: Optional[float] = None, **fields: Any) -> Dict[str, Any]:
		"""Show a prompt and wait for the answer (worker threads only).
		kind: text, confirm, message, busy or choice. Raises PromptCancelled
		when the user backs out, TimeoutError after timeout_s."""
		prompt_id = f"p{next(self._prompt_ids)}"
		if kind == "busy":
			# Nothing to wait for: the next prompt replaces it.
			self._send("prompt", prompt=prompt_id, kind=kind, title=title, text=text, **fields)
			return {}
		waiter: "queue.Queue[dict]" = queue.Queue()
		self._prompt_waiters[prompt_id] = waiter
		try:
			if self._send("prompt", prompt=prompt_id, kind=kind, title=title, text=text, **fields) is None:
				raise PromptCancelled("panel unavailable")
			answer = waiter.get(timeout=timeout_s)
		except queue.Empty:
			self._send("close_prompt", prompt=prompt_id)
			raise TimeoutError(title)
		finally:
			self._prompt_waiters.pop(prompt_id, None)
		if answer.get("cancelled"):
			raise PromptCancelled(title)
		return answer

	def busy(self, title: str, text: str) -> None:
		self.prompt("busy", title, text)

	def close_prompts(self) -> None:
		self._send("close_prompt")

	# --- setup ---------------------------------------------------------------------

	def missing_settings(self) -> List[str]:
		from utils.config_state import get_missing_required_client_fields

		return get_missing_required_client_fields()

	def save_setting(self, key: str, value: str) -> Optional[str]:
		"""Validate and save one client setting, then reconnect with it."""
		value = value.strip()
		check = {"client_id": validate_device_name, "base_url": validate_server, "api_token": validate_api_key}[key]
		error = check(value)
		if error:
			return error
		from utils.config_state import get_client_config, save_client_config

		save_client_config({key: value})
		cfg = get_client_config()
		if not self.missing_settings():
			try:
				from core.networking.websocket import apply_client_config

				apply_client_config(cfg)
			except Exception as exc:
				print(f"[panel] Could not apply the new settings: {exc}")
		if key == "base_url":
			self._post("sky-data", lambda: get_sky_data().refresh(force=True))
		if key == "client_id":
			self._send("status", device=value)
		return "Saved"

	def start_wizard(self) -> Optional[str]:
		if self._wizard_running:
			return None
		self._wizard_running = True
		threading.Thread(target=self._wizard, name="panel-setup", daemon=True).start()
		return None

	def _wizard(self) -> None:
		try:
			while True:
				try:
					self._wizard_steps()
					return
				except PromptCancelled:
					try:
						leave = self.prompt("confirm", "Leave setup?",
							"You can finish it later in the Setup tab.", yes="Leave", no="Back").get("value")
					except PromptCancelled:
						leave = True
					if leave:
						return
		except TimeoutError:
			pass
		finally:
			self._wizard_running = False

	def _wizard_steps(self) -> None:
		from utils.config_state import get_client_config, save_client_config
		from utils.panel_menu import _bare_server
		from utils import pi_system

		self.prompt("message", "Welcome to SELENO",
			"Let's connect this telescope to your Seleno account. You'll need the server "
			"address and an API key from the website.")
		cfg = get_client_config()
		name = cfg.get("client_id") or pi_system.hostname()
		server = _bare_server(cfg.get("base_url", ""))
		key = cfg.get("api_token", "")
		while True:
			name = self._ask("Device name", name, 32, validate_device_name, "1/3 any name you like")
			server = self._ask("Server", server, 96, validate_server, "2/3 e.g. seleno.org")
			key = self._ask("API key", key, 128, validate_api_key, "3/3 from your account")
			summary = f"Name: {name}\nServer: {server}\nKey: ...{key[-4:]}"
			if self.prompt("confirm", "Save these?", summary, yes="Save", no="Change", default="yes").get("value"):
				break
		save_client_config({"client_id": name, "base_url": server, "api_token": key})
		self.busy("Connecting", f"Saved. Connecting to {server}...")
		try:
			from core.networking.websocket import apply_client_config

			apply_client_config(get_client_config())
		except Exception:
			pass
		self._send("status", device=name)
		self._post("sky-data", lambda: get_sky_data().refresh(force=True))
		state = self._wait_for_server(20.0)
		if state == "connected":
			self.prompt("message", "All set", f"{name} is online at {server}.")
		elif state in ("starting", "connecting"):
			self.prompt("message", "Saved",
				"Saved. Still connecting to the server; the Pi tab shows how it's going.")
		else:
			self.prompt("message", "Saved",
				f"Saved, but the server says: {state}. Check the settings in the Setup tab.")
		self._post("menu", self._hello)

	def _ask(self, title: str, value: str, max_length: int, check, subtitle: str) -> str:
		while True:
			answer = str(self.prompt("text", title, subtitle=subtitle, value=value,
				max_length=max_length).get("value", "")).strip()
			error = check(answer)
			if not error:
				return answer
			value = answer
			self.prompt("message", title, error)

	def _wait_for_server(self, seconds: float) -> str:
		deadline = time.monotonic() + seconds
		state = self._server_state()
		while time.monotonic() < deadline:
			state = self._server_state()
			if state in ("connected", "auth failed"):
				return state
			time.sleep(0.5)
		return state

	def wait_for_config(self, poll_s: float = 2.0) -> None:
		"""Block until client_id, base_url and api_token are all set, from the
		panel's setup or from config/client_profile.json (Client.py)."""
		told = False
		while self.missing_settings():
			if not told:
				print("[config] Waiting for setup: use the front panel, or edit config/client_profile.json "
					"(or run 'python Client.py setup')")
				told = True
			time.sleep(poll_s)

	def restart_client(self) -> Optional[str]:
		from utils import pi_system

		self._send("mode", mode="loading")
		self._send("loading", text="Restarting the client")
		pi_system.restart_client()
		return None

	# --- tracking ------------------------------------------------------------------

	def tracking_update(self, **fields: Any) -> None:
		"""From core/hardware/tracking.py: state (idle, slewing, centering,
		tracking, stopped), target, ra, dec, mag."""
		with self._lock:
			if fields.get("state") in ("idle", "stopped"):
				self._tracking = {"state": fields["state"]}
			else:
				self._tracking.update({k: v for k, v in fields.items() if v is not None})
			payload = dict(self._tracking)
		self._sky_dirty = True
		self._post("tracking", lambda: self._send("tracking", **payload))

	def show_sky(self) -> Optional[str]:
		self._sky_dirty = True
		self._send("mode", mode="sky")
		return None

	# --- the sky view ----------------------------------------------------------------

	@staticmethod
	def _load_sky_options() -> SkyOptions:
		from utils.config_state import load_static_state

		saved = load_static_state().get("sky_view", {})
		options = SkyOptions()
		for key, value in (saved.items() if isinstance(saved, dict) else []):
			if hasattr(options, key):
				setattr(options, key, type(getattr(options, key))(value))
		return options

	def sky_options_changed(self) -> None:
		from utils.config_state import load_static_state, save_static_state

		state = load_static_state()
		state["sky_view"] = asdict(self.sky_options)
		save_static_state(state)
		self._sky_dirty = True

	def _zoom(self, direction: int) -> None:
		fov = self.sky_options.fov_deg
		steps = FOV_STEPS
		index = min(range(len(steps)), key=lambda i: abs(steps[i] - fov))
		index = max(0, min(len(steps) - 1, index - direction))  # zoom in = narrower
		if steps[index] != fov:
			self.sky_options.fov_deg = steps[index]
			self.sky_options_changed()
			self.refresh_values()

	def sky_data_status(self) -> str:
		data = get_sky_data()
		if data.last_error and not data.ready():
			return "unavailable"
		return f"{len(data.stars)} stars" if data.ready() else "not loaded"

	def refresh_sky_data(self) -> Optional[str]:
		def run() -> None:
			ok = get_sky_data().refresh(force=True)
			self.toast("Sky data updated" if ok and not get_sky_data().last_error else
				f"Update failed: {get_sky_data().last_error or 'no data'}")
		self._post("sky-data", run)
		return "Updating sky data..."

	def _sky_loop(self) -> None:
		data = get_sky_data()
		while True:
			time.sleep(0.25)
			status = self._status
			if self._conn is None or not status.get("sky_shown"):
				continue
			moving = self._tracking.get("state") in ("slewing", "centering")
			interval = SKY_FAST_S if moving else SKY_SLOW_S
			if not self._sky_dirty and time.monotonic() - self._sky_sent_at < interval:
				continue
			self._sky_dirty = False
			self._sky_sent_at = time.monotonic()
			try:
				data.refresh()
				scene = self._build_sky(data, status)
				if scene is not None:
					self._conn.ui_sky(scene)
			except Exception as exc:
				print(f"[panel] Sky view update failed: {exc}")

	def _build_sky(self, data, status: Dict[str, Any]) -> Optional[bytes]:
		from utils.location import get_current_location
		from utils.telescope_state import get_target_coords, get_telescope_coords

		if not data.ready():
			return None
		loc = get_current_location() or {}
		if loc.get("latitude") is None or loc.get("longitude") is None:
			return None
		scope = get_telescope_coords() or {"right_ascension": 0.0, "declination": 0.0}
		target = None
		if self._tracking.get("state") not in ("idle", "stopped"):
			t = get_target_coords()
			if t:
				target = (float(t["right_ascension"]), float(t["declination"]))
		width = int(status.get("sky_w") or 128)
		height = int(status.get("sky_h") or 136)
		keepouts = [(width - 26, height - 11, 26, 11)]  # the field-width label
		if status.get("sky_radar", True):
			keepouts.append((0, height - 41, 41, 41))  # the radar inset
		stars, names, figures, planets = data.snapshot()
		scene = build_scene(
			width, height, stars, names, figures, planets,
			(float(scope["right_ascension"]), float(scope["declination"])), target,
			float(loc["latitude"]), float(loc["longitude"]), datetime.now(timezone.utc),
			self.sky_options, keepouts,
		)
		return scene.encode()


_instance: Optional[PanelUI] = None
_instance_lock = threading.Lock()


def get_panel_ui() -> PanelUI:
	global _instance
	with _instance_lock:
		if _instance is None:
			_instance = PanelUI()
		return _instance


def tracking_update(**fields: Any) -> None:
	"""Safe to call before any panel exists (it just remembers the state)."""
	try:
		get_panel_ui().tracking_update(**fields)
	except Exception as exc:
		print(f"[panel] tracking update failed: {exc}")
