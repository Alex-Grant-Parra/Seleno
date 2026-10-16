"""Pi facts and power actions for the front panel's Pi tab."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from typing import Optional

_cache: dict = {}


def _cached(key: str, max_age_s: float, fn):
	now = time.monotonic()
	hit = _cache.get(key)
	if hit is not None and now - hit[0] < max_age_s:
		return hit[1]
	try:
		value = fn()
	except Exception:
		value = ""
	_cache[key] = (now, value)
	return value


def ip_address() -> str:
	def find() -> str:
		# No packet is sent: connecting a UDP socket only picks the route.
		s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		try:
			s.connect(("10.255.255.255", 1))
			return s.getsockname()[0]
		finally:
			s.close()
	return _cached("ip", 10.0, find) or "no network"


def hostname() -> str:
	return socket.gethostname()


def wifi_name() -> str:
	def find() -> str:
		for cmd in (["iwgetid", "-r"], ["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"]):
			try:
				out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
			except Exception:
				continue
			if cmd[0] == "nmcli":
				for line in out.splitlines():
					if line.startswith("yes:"):
						return line[4:]
				continue
			if out.strip():
				return out.strip()
		return ""
	return _cached("wifi", 30.0, find) or "not on Wi-Fi"


def uptime() -> str:
	try:
		with open("/proc/uptime") as f:
			seconds = int(float(f.read().split()[0]))
	except Exception:
		return "?"
	days, rest = divmod(seconds, 86400)
	hours, rest = divmod(rest, 3600)
	return f"{days}d {hours}h" if days else f"{hours}h {rest // 60}m"


def _systemctl(action: str) -> Optional[str]:
	"""Reboot or power off. Needs passwordless sudo for systemctl (Raspberry
	Pi OS's first user has it). Returns an error message, or None."""
	try:
		result = subprocess.run(["sudo", "-n", "systemctl", action], capture_output=True, text=True, timeout=10)
	except Exception as exc:
		return str(exc)
	if result.returncode != 0:
		return "Needs passwordless sudo for systemctl"
	return None


def reboot() -> Optional[str]:
	return _systemctl("reboot")


def shutdown() -> Optional[str]:
	return _systemctl("poweroff")


def restart_client(delay_s: float = 1.0) -> None:
	"""Start Client.py again in this process (and so in the same screen
	session client.service runs it in), after the panel has had its reply."""
	def again() -> None:
		time.sleep(delay_s)
		os.execv(sys.executable, [sys.executable] + sys.argv)
	threading.Thread(target=again, name="restart-client", daemon=True).start()
