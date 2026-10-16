"""Sun protection for every go-to (core/hardware/tracking.trackCoordinates,
so the panel's and the server's alike).

What counts is the closest the telescope gets to the Sun: at the target,
or on the way there (the slew moves RA and Dec together, so a path between
two safe points can cross the Sun).

* Closer than WARN_DEG (30): a warning on the front panel about eyes and
  equipment; Cancel is selected first.
* Closer than DANGER_DEG (20): a danger notice, then a code of four random
  arrows that the ESP32 picks and shows, to be pressed in order on the panel.

With no panel attached a warning is only logged, and anything inside 20 deg
is refused: someone has to be at the telescope to confirm it. An answer
holds for that target for APPROVAL_S, so a panel flow that asks first
isn't asked again when it calls trackCoordinates.

The Sun's position is computed here (sky_view.sun_radec), so this works
offline. It applies whether or not the Sun is up.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from utils.sky_view import angular_distance, sun_radec

WARN_DEG = 30.0
DANGER_DEG = 20.0
PROMPT_TIMEOUT_S = 90.0
APPROVAL_S = 120.0
CANCELLED = "Cancelled"

RaDec = Tuple[float, float]


@dataclass
class SunCheck:
	name: str
	target_deg: float  # target to the Sun
	path_deg: float    # closest approach on the slew, target included

	@property
	def closest(self) -> float:
		return min(self.target_deg, self.path_deg)

	@property
	def on_the_way(self) -> bool:
		return self.path_deg < self.target_deg - 1.0

	@property
	def level(self) -> str:
		if self.closest < DANGER_DEG:
			return "danger"
		return "warn" if self.closest < WARN_DEG else "clear"

	def where(self) -> str:
		if self.on_the_way:
			return f"The slew to {self.name} passes {self.closest:.0f} deg from the Sun."
		return f"{self.name}: {self.target_deg:.0f} deg from the Sun."


def _wrap180(deg: float) -> float:
	return (deg + 180.0) % 360.0 - 180.0


def closest_on_slew(start: RaDec, end: RaDec, sun: RaDec, ra_gear: float, dec_gear: float,
		samples: int = 240) -> float:
	"""Closest approach to the Sun along a slew from start to end, as
	tracking._move_motors drives it: both axes at once at the same step rate,
	so in sky degrees Dec moves ra_gear/dec_gear times as fast as RA, and RA
	goes the short way round."""
	d_ra = _wrap180(end[0] - start[0])
	d_dec = end[1] - start[1]
	# Motor degrees for each axis; the longer one sets the slew's length.
	length = max(abs(d_ra) * ra_gear, abs(d_dec) * dec_gear)
	closest = angular_distance(end[0], end[1], sun[0], sun[1])
	if length <= 0:
		return closest
	for i in range(samples + 1):
		s = length * i / samples
		ra = start[0] + (min(abs(d_ra), s / ra_gear) if d_ra >= 0 else -min(abs(d_ra), s / ra_gear))
		dec = start[1] + (min(abs(d_dec), s / dec_gear) if d_dec >= 0 else -min(abs(d_dec), s / dec_gear))
		closest = min(closest, angular_distance(ra % 360.0, max(-90.0, min(90.0, dec)), sun[0], sun[1]))
	return closest


def check(name: str, ra: float, dec: float, start: Optional[RaDec] = None,
		when: Optional[datetime] = None) -> SunCheck:
	"""How close a go-to from start (default: where the telescope is now)
	to (ra, dec) comes to the Sun."""
	sun = sun_radec(when or datetime.now(timezone.utc))
	target_deg = angular_distance(ra, dec, sun[0], sun[1])
	if start is None:
		start = _current_position()
	path_deg = target_deg
	if start is not None:
		from utils.config_state import get_slew_config

		config = get_slew_config()
		path_deg = closest_on_slew(start, (ra, dec), sun, config["ra_gear_ratio"], config["dec_gear_ratio"])
	return SunCheck(name, target_deg, path_deg)


def _current_position() -> Optional[RaDec]:
	try:
		from utils.telescope_state import get_telescope_coords

		coords = get_telescope_coords()
	except Exception:
		return None
	if not coords:
		return None
	ra, dec = float(coords["right_ascension"]), float(coords["declination"])
	if not (0.0 <= ra <= 360.0 and -90.0 <= dec <= 90.0):
		return None
	return ra, dec


# --- answers ------------------------------------------------------------------

_approved: Dict[Tuple[int, int], float] = {}
_lock = threading.Lock()


def _key(ra: float, dec: float) -> Tuple[int, int]:
	return int(round((ra % 360.0) * 10)), int(round(dec * 10))


def _take_approval(ra: float, dec: float) -> bool:
	with _lock:
		expires = _approved.pop(_key(ra, dec), 0.0)
	return expires > time.monotonic()


def _approve(ra: float, dec: float) -> None:
	with _lock:
		_approved[_key(ra, dec)] = time.monotonic() + APPROVAL_S


def _panel():
	try:
		from utils.panel_ui import get_panel_ui

		ui = get_panel_ui()
	except Exception:
		return None
	return ui if ui.has_panel() else None


def _ask(ui, c: SunCheck) -> bool:
	from utils.panel_ui import PromptCancelled

	try:
		if c.level == "warn":
			return bool(ui.prompt("confirm", "Sun warning",
				f"{c.where()} Never look at the Sun through a telescope or finder: it blinds instantly. "
				"Sunlight also burns cameras.",
				yes="Go on", no="Cancel", style="warn", timeout_s=PROMPT_TIMEOUT_S).get("value"))
		# Each text has to fit its screen without scrolling.
		if not ui.prompt("confirm", "DANGER: SUN",
				f"{c.where()} Focused sunlight blinds instantly and burns cameras and eyepieces.",
				yes="Go on", no="Cancel", style="danger", timeout_s=PROMPT_TIMEOUT_S).get("value"):
			return False
		return bool(ui.prompt("code", "Enter the code",
			"Only with solar filters on the telescope and finder. Then press these keys in order:",
			timeout_ms=int(PROMPT_TIMEOUT_S * 1000), timeout_s=PROMPT_TIMEOUT_S + 5).get("value"))
	except (PromptCancelled, TimeoutError):
		return False


def confirm_goto(name: str, ra: float, dec: float, start: Optional[RaDec] = None,
		remember: bool = True) -> Tuple[bool, str]:
	"""Whether a go-to may go ahead, asking on the panel if it comes near the
	Sun. start: where the slew begins (default: the telescope now; pass the
	target itself when nothing will move). remember: keep a yes for the
	go-to that follows (trackCoordinates, which asks last, doesn't).
	Returns (allowed, reason)."""
	c = check(name, ra, dec, start)
	if c.level == "clear":
		return True, ""
	if _take_approval(ra, dec):
		print(f"[sun] {c.where()} Already confirmed on the panel.")
		return True, ""
	ui = _panel()
	if ui is None:
		if c.level == "warn":
			print(f"[sun] Warning: {c.where()} (no front panel to show it on)")
			return True, ""
		print(f"[sun] Refused: {c.where()} Within {DANGER_DEG:.0f} deg needs confirming on the front panel.")
		return False, f"Within {DANGER_DEG:.0f} deg of the Sun: confirm on the telescope's front panel"
	if _ask(ui, c):
		print(f"[sun] {c.where()} Confirmed on the panel ({c.level}).")
		if remember:
			_approve(ra, dec)
		return True, ""
	print(f"[sun] {c.where()} Not confirmed: not going.")
	return False, CANCELLED
