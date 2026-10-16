"""Sun protection for every go-to (core/hardware/tracking.trackCoordinates,
so the panel's and the server's alike).

What counts is the closest the telescope gets to the Sun on the planned
route (core/hardware/mount.py): at the target, or on the way. Routes curve
round the Sun's keep-out (DANGER_DEG plus a margin) when the straight way
would pass it, so in practice it's the target that decides:

* a target closer than WARN_DEG (30): a warning on the front panel about
  eyes and equipment; Cancel is selected first;
* closer than DANGER_DEG (20), at the target or on the way: a danger
  notice, then a code of four random arrows that the ESP32 picks and
  shows, to be pressed in order.

Moving away from the Sun (from a target near it) isn't getting close. With
no panel attached a warning is only logged and anything inside 20 deg is
refused: someone has to be at the telescope.

A yes is a SunApproval: the panel flow that asked passes it to
trackCoordinates (keyword only; a server request can't make one), for that
target, once, within APPROVAL_S. The tracking loop then holds the session
to it: never closer to the Sun than the route or the target allowed
(tracking._sun_and_limits_problem).

The Sun's position is computed here (sky_view.sun_radec), so this works
offline. It applies whether or not the Sun is up.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional, Tuple

from utils import clock
from utils.sky_view import angular_distance, sun_radec
from utils.log import get_logger

log = get_logger("sun")

WARN_DEG = 30.0
DANGER_DEG = 20.0
ROUTE_MARGIN_DEG = 2.0   # routes keep this much further out than DANGER_DEG
PROMPT_TIMEOUT_S = 90.0
APPROVAL_S = 120.0
CANCELLED = "Cancelled"

RaDec = Tuple[float, float]


def keep_out_deg(start_deg: float, target_deg: float) -> float:
	"""How far from the Sun a route keeps: outside the danger zone, but
	never asked to be further out than its own start or target are."""
	return max(0.0, min(DANGER_DEG + ROUTE_MARGIN_DEG, start_deg - 0.5, target_deg - 0.5))


@dataclass
class SunCheck:
	name: str
	target_deg: float  # target to the Sun
	path_deg: float    # closest on the route, target included
	start_deg: float = 180.0  # where it starts

	@property
	def on_the_way(self) -> bool:
		# The route goes closer than both its ends.
		return self.path_deg < self.target_deg - 1.0 and self.path_deg < self.start_deg - 0.5

	@property
	def closest(self) -> float:
		return self.path_deg if self.on_the_way else self.target_deg

	@property
	def level(self) -> str:
		# The code for getting inside DANGER_DEG at all, on the way or at the
		# target; the warning is about where it'll point (a slew curving
		# round the keep-out passes inside WARN_DEG without stopping there).
		if self.closest < DANGER_DEG:
			return "danger"
		return "warn" if self.target_deg < WARN_DEG else "clear"

	def where(self) -> str:
		if self.on_the_way:
			return f"The slew to {self.name} passes {self.closest:.0f} deg from the Sun."
		return f"{self.name}: {self.target_deg:.0f} deg from the Sun."


@dataclass(frozen=True)
class SunApproval:
	"""A yes from the panel for one target."""
	ra: float
	dec: float
	closest_deg: float
	expires: float  # time.monotonic()

	def covers(self, ra: float, dec: float, closest_deg: float) -> bool:
		return (time.monotonic() < self.expires
			and angular_distance(ra, dec, self.ra, self.dec) < 0.05
			and closest_deg >= self.closest_deg - 1.0)


def check(name: str, ra: float, dec: float, start: Optional[RaDec] = None) -> SunCheck:
	"""How close a go-to to (ra, dec) comes to the Sun on its planned route.
	start=(ra, dec) means it's already pointing there (nothing slews)."""
	sun_ra, sun_dec = sun_radec(clock.now())
	target_deg = angular_distance(ra, dec, sun_ra, sun_dec)
	if start is not None:
		start_deg = angular_distance(start[0], start[1], sun_ra, sun_dec)
		return SunCheck(name, target_deg, min(target_deg, start_deg), start_deg)
	try:
		from core.hardware.tracking import plan_goto
		from utils.telescope_state import get_mount_axes

		axes = get_mount_axes()
		route = plan_goto(ra, dec, start_axes=axes)
	except Exception as exc:
		log.warning(f"Couldn't plan the route: {exc}")
		route, axes = None, None
	if route is None or axes is None:
		return SunCheck(name, target_deg, target_deg)
	from utils.telescope_state import get_telescope_coords

	now = get_telescope_coords()
	start_deg = angular_distance(now["right_ascension"], now["declination"], sun_ra, sun_dec) if now else 180.0
	return SunCheck(name, target_deg, route.sun_deg, start_deg)


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
		approval: Optional[SunApproval] = None) -> Tuple[bool, str, Optional[SunApproval]]:
	"""Whether a go-to may go ahead, asking on the panel if it comes near the
	Sun. start: see check(). approval: a yes the caller already has for
	this target. Returns (allowed, reason, the approval to pass on)."""
	c = check(name, ra, dec, start)
	if c.level == "clear":
		return True, "", None
	if isinstance(approval, SunApproval) and approval.covers(ra, dec, c.closest):
		log.info(f"{c.where()} Already confirmed on the panel.")
		return True, "", approval
	ui = _panel()
	if ui is None:
		if c.level == "warn":
			log.warning(f"Warning: {c.where()} (no front panel to show it on)")
			return True, "", None
		log.warning(f"Refused: {c.where()} Within {DANGER_DEG:.0f} deg needs confirming on the front panel.")
		return False, f"Within {DANGER_DEG:.0f} deg of the Sun: confirm on the telescope's front panel", None
	if _ask(ui, c):
		log.info(f"{c.where()} Confirmed on the panel ({c.level}).")
		return True, "", SunApproval(ra % 360.0, dec, c.closest, time.monotonic() + APPROVAL_S)
	log.info(f"{c.where()} Not confirmed: not going.")
	return False, CANCELLED, None
