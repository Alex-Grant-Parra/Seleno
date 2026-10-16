"""GPS, read in the background from gpsd (localhost:2947), when there is one.

* The site: a fix that's moved more than SITE_MOVE_KM from the saved site
  replaces it (unless the site was typed on the panel).
* The time: while the Pi's clock isn't synchronised (no network, no RTC
  battery), GPS time sets the astronomy clock's offset (utils/clock.py).

Nothing blocks: startup used to wait on gpsd for up to ten reports. With
no gpsd it tries again every minute, quietly. config gps.enabled turns it
off.
"""
from __future__ import annotations

import json
import math
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from utils.log import get_logger

log = get_logger("gps")

GPSD = ("127.0.0.1", 2947)
SITE_MOVE_KM = 0.5
RETRY_S = 60.0
CLOCK_EVERY_S = 600.0

_fix: Dict[str, Any] = {}
_thread: Optional[threading.Thread] = None
_last_clock_set = 0.0


def last_fix() -> Dict[str, Any]:
	"""The latest fix: latitude, longitude, altitude, time, mode, at
	(time.monotonic()); empty without one."""
	return dict(_fix)


def has_fix(max_age_s: float = 30.0) -> bool:
	return bool(_fix) and time.monotonic() - _fix.get("at", 0.0) < max_age_s


def _distance_km(a, b) -> float:
	lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
	h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
	return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, h)))


def handle_report(report: Dict[str, Any]) -> None:
	"""One gpsd JSON report (only TPV with a 2D or 3D fix matters)."""
	global _last_clock_set
	if report.get("class") != "TPV" or int(report.get("mode", 0) or 0) < 2:
		return
	lat, lon = report.get("lat"), report.get("lon")
	if lat is None or lon is None:
		return
	_fix.update(latitude=float(lat), longitude=float(lon), altitude=report.get("alt"),
		time=report.get("time"), mode=int(report["mode"]), at=time.monotonic())
	_maybe_move_site(float(lat), float(lon), report.get("alt"))
	stamp = report.get("time")
	if stamp and time.monotonic() - _last_clock_set > CLOCK_EVERY_S:
		from utils import clock

		if not clock.system_synced():
			try:
				when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
				clock.set_time(when.astimezone(timezone.utc), source="gps")
				_last_clock_set = time.monotonic()
			except ValueError:
				pass


def _maybe_move_site(lat: float, lon: float, alt) -> None:
	from utils.config_state import get_location, save_location

	saved = get_location() or {}
	if saved.get("source") == "manual":
		return
	if saved.get("latitude") is not None and saved.get("longitude") is not None:
		if _distance_km((lat, lon), (float(saved["latitude"]), float(saved["longitude"]))) < SITE_MOVE_KM:
			return
	save_location({"latitude": lat, "longitude": lon, "altitude": alt, "source": "hardware_gps",
		"last_updated": datetime.now(timezone.utc).isoformat()})
	log.info("Site from GPS: %.4f, %.4f", lat, lon)


def _session() -> None:
	with socket.create_connection(GPSD, timeout=5.0) as sock:
		sock.sendall(b'?WATCH={"enable":true,"json":true}\n')
		sock.settimeout(30.0)
		buf = b""
		log.info("Reading GPS from gpsd")
		while True:
			chunk = sock.recv(4096)
			if not chunk:
				return
			buf += chunk
			while b"\n" in buf:
				line, buf = buf.split(b"\n", 1)
				try:
					handle_report(json.loads(line))
				except ValueError:
					continue


def _enabled() -> bool:
	try:
		from utils.config_state import get_section

		return bool(get_section("gps").get("enabled", True))
	except Exception:
		return True


def _run() -> None:
	told = False
	while True:
		if _enabled():
			try:
				_session()
			except OSError as exc:
				if not told:
					log.info("No gpsd (%s); trying again every minute", exc)
					told = True
			except Exception as exc:
				log.warning("GPS reader error: %s", exc)
		time.sleep(RETRY_S)


def start() -> None:
	global _thread
	if _thread is None or not _thread.is_alive():
		_thread = threading.Thread(target=_run, name="gps", daemon=True)
		_thread.start()
