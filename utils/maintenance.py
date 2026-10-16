"""Updating the client and the ESP32 firmware when the server asks.

Both are off unless config server.allow_remote_update /
allow_firmware_update say otherwise: an update changes what runs on the
telescope, so whoever owns it decides whether the server may do that.

* update_client: `git pull --ff-only` from the repo's own remote (nothing
  the server sends is run), then the client restarts.
* update_firmware: builds esp32-firmware with PlatformIO and flashes it
  (the dev kit over USB, the v0.8 board through esp32/seleno_flash.py),
  then the client restarts to reconnect.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict

from utils.log import get_logger

log = get_logger("maintenance")
ROOT = Path(__file__).resolve().parents[1]
_busy = threading.Lock()


def _allowed(key: str) -> bool:
	from utils.config_state import get_section

	return bool(get_section("server").get(key, False))


def _restart_soon() -> None:
	from utils import pi_system

	pi_system.restart_client(delay_s=2.0)


def update_client() -> Dict[str, Any]:
	if not _allowed("allow_remote_update"):
		return {"error": "Remote updates are off (config server.allow_remote_update)"}
	if not _busy.acquire(blocking=False):
		return {"error": "An update is already running"}
	try:
		result = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only"], capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.TimeoutExpired) as exc:
		_busy.release()
		return {"error": f"git pull failed: {exc}"}
	output = (result.stdout + result.stderr).strip()
	if result.returncode != 0:
		_busy.release()
		return {"error": output[-300:] or "git pull failed"}
	if "Already up to date" in output:
		_busy.release()
		return {"status": "Already up to date"}
	log.info("Client updated: %s", output[-200:])
	_restart_soon()
	return {"status": "Updated; restarting", "output": output[-300:]}


def _pio() -> str:
	for candidate in (os.path.expanduser("~/.platformio/penv/bin/pio"), shutil.which("pio") or ""):
		if candidate and os.path.exists(candidate):
			return candidate
	return ""


def update_firmware() -> Dict[str, Any]:
	if not _allowed("allow_firmware_update"):
		return {"error": "Firmware updates are off (config server.allow_firmware_update)"}
	pio = _pio()
	if not pio:
		return {"error": "PlatformIO isn't installed"}
	if not _busy.acquire(blocking=False):
		return {"error": "An update is already running"}

	def run() -> None:
		from utils.esp32_state import esp32_state

		try:
			conn = esp32_state.get_connection()
			transport = getattr(conn, "transport", "usb") if conn else "usb"
			port = conn.cfg.port if conn else ""
			env = "devkit" if transport == "usb" else "seleno_v08"
			esp32_state.clear_connection()  # the port has to be free
			build = subprocess.run([pio, "run", "-d", str(ROOT / "esp32-firmware"), "-e", env],
				capture_output=True, text=True, timeout=900)
			if build.returncode != 0:
				log.error("Firmware build failed: %s", build.stdout[-400:])
				return
			if env == "devkit":
				flash = [pio, "run", "-d", str(ROOT / "esp32-firmware"), "-e", env, "-t", "upload"]
				if port:
					flash += ["--upload-port", port]
			else:
				flash = [sys.executable, "-m", "esp32.seleno_flash", "write", "--build-dir",
					str(ROOT / "esp32-firmware" / ".pio" / "build" / env)]
			result = subprocess.run(flash, capture_output=True, text=True, timeout=600, cwd=str(ROOT))
			if result.returncode != 0:
				log.error("Firmware flash failed: %s", (result.stdout + result.stderr)[-400:])
			else:
				log.info("Firmware flashed (%s)", env)
		except Exception as exc:
			log.exception("Firmware update failed: %s", exc)
		finally:
			_busy.release()
			_restart_soon()

	threading.Thread(target=run, name="firmware-update", daemon=True).start()
	return {"status": "Building and flashing the firmware; the client restarts afterwards"}
