"""The time the astronomy uses.

Go-tos are only as good as the clock: a minute out is a quarter of a
degree. The Pi keeps time from the network (NTP); offline it relies on the
Pi 5's real-time clock, which only keeps time with its backup battery
fitted. Without either, the clock after a boot is wherever it was at the
last shutdown.

So everything astronomical reads now() here, which is the system clock
plus an offset. The offset is set from GPS (utils/gps.py) or typed on the
front panel, and only applies while the system clock isn't synchronised;
once NTP takes over it's dropped. It needs no root (the system clock
isn't changed) and survives a client restart in the same boot.

status() says how far to trust it, for the panel's warning.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from utils.log import get_logger

log = get_logger("clock")

_TIME_ERROR = 5          # adjtimex(2): the clock isn't synchronised
_SYNC_CHECK_S = 10.0

_lock = threading.Lock()
_offset_s = 0.0
_source = "system"       # "system", "gps" or "manual"
_set_at = 0.0            # time.monotonic() when the offset was set
_synced_cache = (None, -1e9)   # (value, monotonic time checked)
_loaded = False


def _libc_adjtimex() -> Optional[int]:
	try:
		libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
		buf = ctypes.create_string_buffer(512)  # struct timex, modes = 0: read only
		return int(libc.adjtimex(buf))
	except Exception:
		return None


def system_synced() -> Optional[bool]:
	"""Whether the kernel clock is synchronised (NTP, or a PPS); None if
	it can't be told."""
	global _synced_cache
	value, checked = _synced_cache
	if time.monotonic() - checked < _SYNC_CHECK_S:
		return value
	state = _libc_adjtimex()
	value = None if state is None or state < 0 else state != _TIME_ERROR
	_synced_cache = (value, time.monotonic())
	return value


def _boot_id() -> str:
	try:
		with open("/proc/sys/kernel/random/boot_id") as f:
			return f.read().strip()
	except OSError:
		return ""


def _load() -> None:
	# An offset set earlier in this boot (the client was restarted).
	global _loaded, _offset_s, _source, _set_at
	if _loaded:
		return
	_loaded = True
	try:
		from utils.config_state import runtime_store

		saved = runtime_store().get("clock") or {}
		if saved.get("boot_id") and saved.get("boot_id") == _boot_id():
			_offset_s = float(saved.get("offset_s", 0.0))
			_source = str(saved.get("source", "manual"))
			_set_at = time.monotonic()
	except Exception as exc:
		log.debug("No saved clock offset: %s", exc)


def offset_s() -> float:
	"""Seconds added to the system clock (0 once it's synchronised)."""
	_load()
	if _source == "system" or system_synced():
		return 0.0
	return _offset_s


def now() -> datetime:
	"""UTC now, as the astronomy should see it."""
	return datetime.now(timezone.utc) + timedelta(seconds=offset_s())


def set_time(when: datetime, source: str = "manual") -> float:
	"""Take `when` (timezone-aware, or UTC) as the time now. Returns the
	offset from the system clock, seconds."""
	global _offset_s, _source, _set_at
	_load()
	if when.tzinfo is None:
		when = when.replace(tzinfo=timezone.utc)
	offset = (when - datetime.now(timezone.utc)).total_seconds()
	with _lock:
		_offset_s, _source, _set_at = offset, source, time.monotonic()
	try:
		from utils.config_state import runtime_store

		runtime_store().update({"clock": {"boot_id": _boot_id(), "offset_s": offset, "source": source}}, when="now")
	except Exception as exc:
		log.warning("Couldn't save the clock offset: %s", exc)
	log.info("Clock set from %s: %+.1f s from the system clock%s", source, offset,
		" (the system clock is synchronised, so it's ignored)" if system_synced() else "")
	return offset


def clear() -> None:
	global _offset_s, _source
	_load()
	with _lock:
		_offset_s, _source = 0.0, "system"
	try:
		from utils.config_state import runtime_store

		runtime_store().update({"clock": {}}, when="now")
	except Exception:
		pass


def trusted() -> bool:
	"""Synchronised, or set from GPS or by hand in this boot."""
	_load()
	return bool(system_synced()) or _source in ("gps", "manual")


def status() -> Dict[str, Any]:
	_load()
	synced = system_synced()
	if synced:
		state = "synced"
	elif _source in ("gps", "manual"):
		state = f"set from {_source}"
	elif synced is None:
		state = "unknown"
	else:
		state = "not synced"
	return {"state": state, "trusted": trusted(), "source": "system" if synced else _source,
		"offset_s": round(offset_s(), 2), "utc": now().isoformat(timespec="seconds")}


def short_status() -> str:
	"""For the panel: 'synced', 'GPS', 'set by hand' or 'NOT SET'."""
	s = status()
	if s["state"] == "synced":
		return "synced"
	if s["source"] == "gps":
		return "GPS"
	if s["source"] == "manual":
		return "set by hand"
	return "unknown" if s["state"] == "unknown" else "NOT SET"


def parse_utc(text: str) -> datetime:
	"""'2026-10-16 21:30', '2026-10-16 21:30:05', '10-16 21:30' (this year)
	or '21:30' (today), UTC."""
	text = text.strip().replace("T", " ").rstrip("Zz").strip()
	formats = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%m-%d %H:%M", "%H:%M:%S", "%H:%M")
	for fmt in formats:
		try:
			parsed = datetime.strptime(text, fmt)
		except ValueError:
			continue
		today = now().date()
		if fmt.startswith("%H"):
			parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
		elif fmt.startswith("%m"):
			parsed = parsed.replace(year=today.year)
		return parsed.replace(tzinfo=timezone.utc)
	raise ValueError("Type UTC as 10-16 21:30")
