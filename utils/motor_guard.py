"""Guards on the server's raw motor commands (espStart, espMoveSteps,
espTurnDegrees: the web page's motor panel).

They move one motor by steps, with no idea where the telescope points, so
they bypassed the route planner and the Sun guard. config
server.raw_motor_commands says what happens:

* "guarded" (the default): a move that would end past an axis limit, or
  take the telescope inside the Sun's danger zone (utils/sun_guard.py), is
  refused; moving away from the Sun is fine. A continuous move is watched
  and stopped at a limit or the Sun. Refused while tracking (stop it
  first) so the two don't fight.
* "off": refused.
* "unrestricted": as before (bench testing, nothing on the mount).

When the position or site isn't known there's nothing to check against;
the move goes ahead and the log says so.
"""
from __future__ import annotations

import threading
from typing import Optional, Tuple

from utils.log import get_logger

log = get_logger("motor_guard")
_watchers: dict = {}


def _policy() -> str:
	try:
		from utils.config_state import get_section

		value = str(get_section("server").get("raw_motor_commands", "guarded")).lower()
	except Exception:
		value = "guarded"
	return value if value in ("guarded", "off", "unrestricted") else "guarded"


def _context():
	"""(config, axes, sun (ha, dec)) or None if there's nothing to check."""
	from core.hardware import tracking
	from utils import sun_guard  # noqa: F401 (the danger radius)
	from utils.telescope_state import get_mount_axes

	tracking._update_current_coords_from_motors()
	axes = get_mount_axes()
	lon = tracking._longitude()
	if axes is None or lon is None:
		return None
	return tracking.mount_config(), axes, tracking._sun_hour_angle(lon)


def _end_axes(config, axes: Tuple[float, float], motor_id: str, steps: float) -> Tuple[float, float]:
	d_ra, d_dec = config.axis_degrees(steps if motor_id == "motor1" else 0.0, steps if motor_id == "motor2" else 0.0)
	return axes[0] + d_ra, axes[1] + d_dec


def _problem(config, start, end, sun) -> Optional[str]:
	from core.hardware import mount
	from utils.sun_guard import DANGER_DEG

	if not config.within(end):
		return "That would take an axis past its limit"
	start_sun = mount.separation(mount.sky_from_axes(*start, config), sun)
	closest = mount.closest_to([start, end], sun, config=config)
	if closest < DANGER_DEG and closest < start_sun - 0.5:
		return f"That would point {closest:.0f} deg from the Sun: use the front panel"
	return None


def check_raw_move(motor_id: str, steps: Optional[float] = None, motor_degrees: Optional[float] = None,
		continuous: bool = False, forward: bool = True) -> Optional[str]:
	"""None if the move may go ahead, else why not."""
	policy = _policy()
	if policy == "unrestricted":
		return None
	if policy == "off":
		return "Raw motor commands are off (config server.raw_motor_commands)"
	if motor_id not in ("motor1", "motor2"):
		return None
	try:
		from core.hardware import tracking

		if tracking._tracking_active:
			return "Stop tracking before moving a motor by hand"
		context = _context()
	except Exception as exc:
		log.warning("Couldn't check the move: %s", exc)
		return None
	if context is None:
		log.info("Raw %s move not checked: the position or site isn't known", motor_id)
		return None
	config, axes, sun = context
	if motor_degrees is not None:
		steps = motor_degrees / 360.0 * config.steps_per_rev
	if continuous:
		# One degree of axis travel that way: the watcher handles the rest.
		per_deg = config.motor_steps(1.0, 0.0)[0] if motor_id == "motor1" else config.motor_steps(0.0, 1.0)[1]
		steps = per_deg * (1 if forward else -1)
	if not steps:
		return None
	return _problem(config, axes, _end_axes(config, axes, motor_id, float(steps)), sun)


def watch_continuous(motor_id: str, forward: bool, period_s: float = 0.25) -> None:
	"""Stop a continuous raw move at an axis limit or the Sun."""
	if _policy() != "guarded" or motor_id not in ("motor1", "motor2"):
		return
	old = _watchers.get(motor_id)
	if old is not None:
		old.set()
	done = threading.Event()
	_watchers[motor_id] = done

	def watch() -> None:
		from utils.esp32_state import esp32_state

		while not done.wait(period_s):
			try:
				conn = esp32_state.get_connection()
				if conn is None:
					return
				status = conn.send({"cmd": "status", "motor": motor_id})
				if not status.get("moving"):
					return
				context = _context()
				if context is None:
					continue
				config, axes, sun = context
				ahead = _end_axes(config, axes, motor_id, (1 if forward else -1)
					* (config.motor_steps(0.5, 0.0)[0] if motor_id == "motor1" else config.motor_steps(0.0, 0.5)[1]))
				problem = _problem(config, axes, ahead, sun)
				if problem:
					conn.send({"cmd": "stop", "motor": motor_id})
					log.warning("Stopped %s: %s", motor_id, problem)
					try:
						from utils.panel_ui import get_panel_ui

						get_panel_ui().toast(problem[:60], 5000)
					except Exception:
						pass
					return
			except Exception as exc:
				log.debug("Watching %s: %s", motor_id, exc)
		_watchers.pop(motor_id, None)

	threading.Thread(target=watch, name=f"guard-{motor_id}", daemon=True).start()
