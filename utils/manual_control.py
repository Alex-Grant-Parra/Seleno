"""Manual control from the front panel, with or without the server.

* Go to RA/Dec: type coordinates on the panel's keyboard; the telescope
  slews there and tracks it (core/hardware/tracking.trackCoordinates).
* Go to star: pick one of the brightest named stars that are up now, from
  the sky view's cached catalogue (utils/sky_data.py), so it works offline
  once the catalogue has been downloaded.
* Align on star: centre a bright star by hand, then tell SELENO which it
  is; that becomes the telescope's position, and it tracks the star.
* Site: latitude and longitude typed in, for when there's no GPS or
  internet to find it.

Each flow runs on its own thread (PanelUI.run_flow) and asks through
PanelUI.prompt; backing out of any prompt (RETURN) ends it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

from utils.sky_view import lst_degrees, radec_to_altaz

if TYPE_CHECKING:
	from utils.panel_ui import PanelUI

STAR_MIN_ALT_DEG = 10.0
STAR_CHOICES = 30

# The last coordinates typed, offered again next time.
_last_typed = {"ra": "", "dec": ""}


# --- coordinates ----------------------------------------------------------------

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def _sexagesimal(text: str, units: str) -> List[float]:
	"""'5h35m17s', '5:35:17', '5 35 17' -> [5, 35, 17]."""
	cleaned = re.sub(f"[{units}:'\"°,]", " ", text)
	parts = cleaned.split()
	if not parts or len(parts) > 3 or not all(_NUMBER.fullmatch(p) for p in parts):
		raise ValueError("Couldn't read that")
	values = [float(p) for p in parts]
	if any(v < 0 for v in values[1:]) or any(v >= 60 for v in values[1:]):
		raise ValueError("Minutes and seconds go 0 to 59")
	return values


def parse_ra(text: str) -> float:
	"""Right ascension in hours ('5 35 17', '5:35:17', '5h35m17s', '5.588')
	or degrees with a d ('83.82d'). Returns degrees."""
	t = text.strip().lower()
	if not t:
		raise ValueError("Type the RA")
	degrees = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:d|deg|°)", t)
	if degrees:
		value = float(degrees.group(1))
		if not 0 <= value < 360:
			raise ValueError("Degrees go 0 to 360")
		return value
	parts = _sexagesimal(t, "hms")
	if parts[0] < 0:
		raise ValueError("RA can't be negative")
	hours = parts[0] + sum(v / 60 ** i for i, v in enumerate(parts[1:], 1))
	if hours >= 24:
		raise ValueError("Hours go 0 to 24. End with d for degrees.")
	return hours * 15.0


def parse_dec(text: str) -> float:
	"""Declination in degrees ('-5 23 28', '-5:23:28', '-5d23m28s', '-5.39')."""
	t = text.strip().lower()
	if not t:
		raise ValueError("Type the Dec")
	negative = t.startswith("-")
	parts = _sexagesimal(t.lstrip("+-").strip(), "dms")
	if parts[0] < 0:
		raise ValueError("Put the minus sign first")
	degrees = parts[0] + sum(v / 60 ** i for i, v in enumerate(parts[1:], 1))
	if degrees > 90:
		raise ValueError("Dec goes -90 to +90")
	return -degrees if negative else degrees


def _split(value: float) -> Tuple[int, int, int]:
	total = int(round(value * 3600))
	return total // 3600, (total // 60) % 60, total % 60


def _ra_hms(ra_deg: float) -> Tuple[int, int, int]:
	h, m, s = _split((ra_deg % 360.0) / 15.0)
	return h % 24, m, s  # 23h59m59.9s rounds to 0h00m00s


def format_ra(ra_deg: float) -> str:
	h, m, s = _ra_hms(ra_deg)
	return f"{h}h{m:02d}m{s:02d}s"


def format_dec(dec_deg: float) -> str:
	d, m, s = _split(abs(dec_deg))
	return f"{'-' if dec_deg < 0 else '+'}{d}d{m:02d}m{s:02d}s"


def target_name(ra_deg: float, dec_deg: float) -> str:
	# Short enough for the sky view's top strip beside the distance and ETA:
	# "5h35 -5". The details page has the exact coordinates.
	h, m, _s = _ra_hms(ra_deg)
	return f"{h}h{m:02d} {dec_deg:+.0f}"


# --- the site ---------------------------------------------------------------------

def parse_site(text: str) -> Tuple[float, float]:
	"""'51.48, -0.01' or '51.48N 0.01W' -> (latitude, longitude)."""
	found = re.findall(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([nsew])?", text.strip().lower())
	if len(found) != 2:
		raise ValueError("Type latitude, longitude")
	(lat_s, lat_h), (lon_s, lon_h) = found
	if lat_h in ("e", "w") or lon_h in ("n", "s"):
		raise ValueError("Latitude first, then longitude")
	lat = float(lat_s) * (-1 if lat_h == "s" else 1)
	lon = float(lon_s) * (-1 if lon_h == "w" else 1)
	if not -90 <= lat <= 90:
		raise ValueError("Latitude goes -90 to 90")
	if not -180 <= lon <= 180:
		raise ValueError("Longitude goes -180 to 180")
	return lat, lon


def site() -> Optional[Tuple[float, float]]:
	from utils.location import get_current_location

	loc = get_current_location() or {}
	if loc.get("latitude") is None or loc.get("longitude") is None:
		return None
	return float(loc["latitude"]), float(loc["longitude"])


def site_text() -> str:
	here = site()
	return f"{here[0]:.2f}, {here[1]:.2f}" if here else ""


def set_site(text: str, notify: Callable[[str], None]) -> Optional[str]:
	"""The Site item's setter. Blank (or 'auto') goes back to GPS or IP lookup."""
	import threading

	from utils import location
	from utils.config_state import is_offline_mode

	if text.strip().lower() in ("", "auto"):
		location.forget_manual_location()

		def look_up() -> None:
			location.update_location_config(use_network=not is_offline_mode())
			notify(f"Site: {site_text() or 'unknown'}")

		threading.Thread(target=look_up, name="panel-site", daemon=True).start()
		return "Finding the site..."
	try:
		lat, lon = parse_site(text)
	except ValueError as exc:
		return str(exc)
	location.set_manual_location(lat, lon)
	return "Site saved"


# --- stars ------------------------------------------------------------------------

@dataclass
class Target:
	name: str
	ra: float
	dec: float
	mag: Optional[float] = None
	alt: float = 0.0


def display_name(name: str) -> str:
	# The panel's fonts are ASCII only.
	return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").strip()


def stars_up(stars, names, lat: float, lon: float, when: datetime,
		min_alt: float = STAR_MIN_ALT_DEG, limit: int = STAR_CHOICES) -> List[Target]:
	"""The brightest named stars at least min_alt above the horizon."""
	lst = lst_degrees(when, lon)
	found: List[Target] = []
	seen = set()
	for ra, dec, mag, star_id in stars:  # brightest first
		name = display_name(names.get(star_id, ""))
		if not name or name in seen:
			continue
		alt, _az = radec_to_altaz(ra, dec, lst, lat)
		if alt < min_alt:
			continue
		seen.add(name)
		found.append(Target(name, float(ra), float(dec), float(mag), alt))
		if len(found) >= limit:
			break
	return found


# --- the flows --------------------------------------------------------------------

def _need_site(ui: "PanelUI") -> Optional[Tuple[float, float]]:
	here = site()
	if here is None:
		ui.prompt("message", "Site needed", "Set the site in the Scope tab first: tracking needs latitude and longitude.")
	return here


def _ask_parsed(ui: "PanelUI", title: str, value: str, subtitle: str, parse: Callable[[str], float]) -> Tuple[float, str]:
	while True:
		text = str(ui.prompt("text", title, subtitle=subtitle, value=value, max_length=24).get("value", "")).strip()
		try:
			return parse(text), text
		except ValueError as exc:
			ui.prompt("message", title, str(exc))
			value = text


def _pick_star(ui: "PanelUI", title: str, here: Tuple[float, float]) -> Optional[Target]:
	from utils.sky_data import get_sky_data

	data = get_sky_data()
	data.refresh()
	if not data.ready():
		ui.prompt("message", "No star list", "The star list hasn't been downloaded yet. Connect to the server once.")
		return None
	stars, names, _figures, _planets = data.snapshot()
	choices = stars_up(stars, names, here[0], here[1], datetime.now(timezone.utc))
	if not choices:
		ui.prompt("message", title, "No bright named stars are above the horizon.")
		return None
	answer = ui.prompt("choice", title, options=[t.name for t in choices], selected=0)
	index = int(answer.get("value", -1))
	return choices[index] if 0 <= index < len(choices) else None


def _release_estop(ui: "PanelUI") -> bool:
	from utils.esp32_state import esp32_state

	conn = esp32_state.get_connection()
	if conn is None:
		ui.prompt("message", "No motor board", "The ESP32 isn't connected.")
		return False
	try:
		if not conn.estop_status().get("active"):
			return True
	except Exception:
		return True  # let the move report what's wrong
	if not ui.prompt("confirm", "Emergency stop", "The emergency stop is on. Release it and go?",
			yes="Release", no="Cancel").get("value"):
		return False
	conn.clear_estop()
	return True


def _sun_ok(ui: "PanelUI", name: str, ra: float, dec: float, start: Optional[Tuple[float, float]] = None) -> bool:
	# Asked first, before anything else is changed (utils/sun_guard.py);
	# trackCoordinates then finds the answer and doesn't ask again.
	from utils import sun_guard

	allowed, reason = sun_guard.confirm_goto(name, ra, dec, start=start)
	if not allowed and reason != sun_guard.CANCELLED:
		ui.prompt("message", "Not going", reason)
	return allowed


def _safe_to_go(ui: "PanelUI", name: str, ra: float, dec: float, here: Tuple[float, float]) -> bool:
	if not _sun_ok(ui, name, ra, dec):
		return False
	now = datetime.now(timezone.utc)
	alt, _az = radec_to_altaz(ra, dec, lst_degrees(now, here[1]), here[0])
	if alt < 0:
		if not ui.prompt("confirm", "Below the horizon",
				f"{name} is {-alt:.0f} deg below the horizon. Go anyway?", yes="Go", no="Cancel").get("value"):
			return False
	return _release_estop(ui)


def _start(ui: "PanelUI", name: str, ra: float, dec: float, mag: Optional[float]) -> None:
	# The panel switches to the sky view itself once the slew starts.
	from core.hardware.tracking import trackCoordinates

	result = trackCoordinates(name, ra, dec, mag)
	if isinstance(result, dict) and result.get("error"):
		ui.toast(str(result["error"])[:60])


def goto_coordinates(ui: "PanelUI") -> None:
	here = _need_site(ui)
	if here is None:
		return
	ra, ra_text = _ask_parsed(ui, "RA", _last_typed["ra"], "hours: 5 35 17", parse_ra)
	_last_typed["ra"] = ra_text
	dec, dec_text = _ask_parsed(ui, "Dec", _last_typed["dec"], "degrees: -5 23 28", parse_dec)
	_last_typed["dec"] = dec_text
	alt, _az = radec_to_altaz(ra, dec, lst_degrees(datetime.now(timezone.utc), here[1]), here[0])
	if not ui.prompt("confirm", "Go there?", f"RA {format_ra(ra)}\nDec {format_dec(dec)}\nAltitude {alt:.0f} deg",
			yes="Go", no="Cancel", default="yes").get("value"):
		return
	name = target_name(ra, dec)
	if _safe_to_go(ui, name, ra, dec, here):
		_start(ui, name, ra, dec, None)


def goto_star(ui: "PanelUI") -> None:
	here = _need_site(ui)
	if here is None:
		return
	star = _pick_star(ui, "Go to star", here)
	if star is not None and _safe_to_go(ui, star.name, star.ra, star.dec, here):
		_start(ui, star.name, star.ra, star.dec, star.mag)


def align_on_star(ui: "PanelUI") -> None:
	here = _need_site(ui)
	if here is None:
		return
	star = _pick_star(ui, "Pointing at?", here)
	if star is None:
		return
	if not ui.prompt("confirm", "Align",
			f"Is {star.name} in the middle of the view now? SELENO will take that as its position and track it.",
			yes="Yes", no="Cancel", default="yes").get("value"):
		return
	# Nothing slews (it's already there), but tracking it near the Sun
	# still needs confirming.
	if not _sun_ok(ui, star.name, star.ra, star.dec, start=(star.ra, star.dec)):
		return
	if not _release_estop(ui):
		return
	from core.hardware.tracking import sync_position

	sync_position(star.ra, star.dec)
	ui.toast(f"Aligned on {star.name}")
	_start(ui, star.name, star.ra, star.dec, star.mag)
