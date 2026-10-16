import atexit
import glob
import json
import os
import threading
import time
from copy import deepcopy
from typing import Any, Dict, Optional
from utils.log import get_logger

log = get_logger("config")

CLIENT_PROFILE_FILE = os.path.join("config", "client_profile.json")
STATIC_STATE_FILE = os.path.join("config", "client_config.json")
RUNTIME_STATE_FILE = os.path.join("config", "runtime_state.json")

LEGACY_LOCATION_FILE = os.path.join("config", "location.json")
LEGACY_TELESCOPE_STATE_FILE = os.path.join("config", "telescope_state.json")
LEGACY_LIVEVIEW_FILE = os.path.join("config", "liveview_state.json")

_STATE_READY = False

# Held around every read-modify-write of the runtime state file. Several threads
# (tracking loop, hour angle updater, liveview) update it concurrently.
runtime_state_lock = threading.RLock()


def _normalize_base_url(raw_url: str) -> str:
    # "" stays "": no server set (never a built-in default).
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return ""
    if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
        raw_url = f"https://{raw_url}"
    return raw_url.rstrip("/") + "/"


def build_service_urls(base_url: str) -> Dict[str, str]:
    normalized = _normalize_base_url(base_url)
    if not normalized:
        raise ValueError("No server set")
    host = normalized.split("://", 1)[1].rstrip("/")
    ws_scheme = "wss" if normalized.startswith("https://") else "ws"
    return {
        "base_url": normalized,
        "http_url": normalized,
        "server_uri": f"{ws_scheme}://ws.{host}/",
        "liveview_uri": f"{ws_scheme}://liveview.{host}/",
    }


def _default_static_state() -> Dict[str, Any]:
    return {
        "slew_config": {
            "slew_speed_sps": 1200.0,
            "refine_speed_sps": 150.0,
            "slew_threshold_degrees": 1.0,
            "center_threshold_degrees": 0.1,
            "centered_threshold_degrees": 0.01,
            "ra_gear_ratio": 360.0,
            "dec_gear_ratio": 144.0,
        },
        "mount": {
            # How far each axis may turn, degrees (core/hardware/mount.py).
            # RA: from the meridian; Dec: from the equator, where past +-90
            # the telescope is on the other side of the mount (over a pole).
            # Slews take the quickest way, over a pole if that's quicker,
            # unless pole_crossing is off.
            "ra_axis_limits": [-270.0, 270.0],
            "dec_axis_limits": [-180.0, 180.0],
            "pole_crossing": True,
            # "auto" (from the site's latitude), "north" or "south"
            "hemisphere": "auto",
            # Park/home: axis angles. (0, 90) points at the pole with the
            # counterweight down, the usual place to start a session.
            "park_axes": [0.0, 90.0],
            # Extra motor steps taken when an axis reverses, to take up the
            # gears' slack (0: none).
            "backlash_steps": [0, 0],
        },
        "horizon": {
            # Go-tos from the server below this altitude are refused (the
            # panel asks), and tracking stops when the target sets below it.
            "min_alt_deg": 0.0,
            # Optional [[azimuth, altitude], ...] (trees, a house): the
            # higher of this and min_alt_deg applies.
            "profile": [],
            "stop_when_set": True,
        },
        "tracking": {
            # "sidereal", "lunar", "solar" or "custom" (custom_arcsec_per_s
            # of RA). The Moon and planets are followed with their own
            # rates automatically when auto_rate is on.
            "rate": "sidereal",
            "custom_arcsec_per_s": 15.041,
            "auto_rate": True,
            # Aim where things appear (the air lifts them near the horizon).
            "refraction": True,
        },
        "liveview": {
            # "low", "medium", "high" or "original" (as the camera sends it)
            "quality": "medium",
            "max_fps": 10.0,      # while slewing, focusing or changing
            "still_fps": 2.0,     # while the view isn't changing
            "keyframe_s": 5.0,    # an unchanged view is still sent this often
            "adaptive": True,     # lower the quality when the link is slow
            "idle_stop_min": 0,   # stop after this long with no viewers (0: never)
            "viewer_grace_s": 60, # pause this long after the last viewer leaves
        },
        "camera": {
            # "auto" (libgphoto2 if installed, else the gphoto2 command),
            # "libgphoto2" or "cli"
            "backend": "auto",
            # Turn the camera's auto power-off off while live view runs.
            "keep_awake": True,
        },
        "upload": {
            # RAW files: "after_jpeg" (queued behind the JPEGs),
            # "on_request" (kept on the Pi until asked) or "never".
            "raw": "after_jpeg",
            "retry_max_s": 600,
        },
        "local_page": {
            # A page on the local network with the live view, photos and
            # status; it asks for the code shown on the front panel.
            "enabled": False,
            "port": 8080,
        },
        "server": {
            # Raw motor commands from the server (espStart, espMoveSteps...):
            # "guarded" (refused near the Sun or past the axis limits),
            # "off" or "unrestricted" (bench testing only).
            "raw_motor_commands": "guarded",
            # Send the client's status that often (seconds, 15 at least; 0:
            # off). The ASTRA server only logs such messages for now.
            "status_push_s": 0,
            # Let the server update the client (git pull and restart).
            "allow_remote_update": False,
            "allow_firmware_update": False,
        },
        "solver": {
            # ASTAP (astap_cli). "" finds it in ~/astap or on the PATH.
            "path": "",
            "fov_deg": 0.0,           # image height in degrees; 0: work it out
            "search_radius_deg": 15.0,
            "timeout_s": 60.0,
            "centre_tolerance_arcmin": 2.0,
            "drift_check_min": 0,     # re-solve and correct while tracking (0: off)
        },
        "gps": {
            "enabled": True,
            "poll_s": 60.0,
        },
        "esp32": {
            # "auto": USB serial devices (dev kit) first, then the Pi's GPIO
            # UART (SELENO v0.8 board on J7). A found port is saved here and
            # tried first next time; the search still runs if it's gone.
            "port": "auto",
            "baudrate": 921600,
            "timeout": 0.5,
            # "auto", "usb" or "gpio_uart"
            "transport": "auto",
        },
    }


def _default_client_profile() -> Dict[str, Any]:
    return {
        "client_config": {
            "client_id": "",
            "base_url": "",
            "api_token": "",
            # Offline mode: no server connection; go-to and photos from the
            # front panel only (utils/panel_menu.py).
            "offline": False,
        },
        "location": {},
    }


def _default_runtime_state() -> Dict[str, Any]:
    return {
        "current_right_ascension": 0.0,
        "current_declination": 0.0,
        "target_right_ascension": 0.0,
        "target_declination": 0.0,
        "source": "manual",
        "updated_at": "",
        "current_hour_angle": 0.0,
        "liveview_enabled": False,
    }


def _load_json(path: str) -> Dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _has_valid_state_file(path: str) -> bool:
    data = _load_json(path)
    return isinstance(data, dict)


def _save_json(path: str, payload: Dict[str, Any], sync: bool = True) -> None:
    # Write to a temp file and rename over the target, so a concurrent reader never
    # sees a truncated file (which would load as defaults and be saved back as zeros).
    # sync: flush it to the SD card before the rename, so a power cut (the usual way
    # a telescope in a field stops) leaves the old file or the new one, not nothing.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
        if sync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp_path, path)
    if sync:
        try:
            fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            pass


def remove_stale_temp_files(max_age_s: float = 60.0) -> int:
    # Temp files left by writes that were cut off (the process was killed
    # between the write and the rename). Returns how many were removed.
    removed = 0
    folders = {os.path.dirname(p) or "." for p in (CLIENT_PROFILE_FILE, STATIC_STATE_FILE, RUNTIME_STATE_FILE)}
    for folder in folders:
        for tmp in glob.glob(os.path.join(folder, "*.json.*.tmp")):
            try:
                if time.time() - os.path.getmtime(tmp) > max_age_s:
                    os.remove(tmp)
                    removed += 1
            except OSError:
                pass
    return removed


def _merge_defaults(defaults: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key].update(value)
        else:
            merged[key] = value
    return merged


def _migrate_static_state() -> Dict[str, Any]:
    defaults = _default_static_state()
    static_existing = _load_json(STATIC_STATE_FILE)
    telescope_legacy = _load_json(LEGACY_TELESCOPE_STATE_FILE) or {}

    state = deepcopy(defaults)

    if static_existing:
        if "slew_config" in static_existing and isinstance(static_existing["slew_config"], dict):
            state["slew_config"] = _merge_defaults(defaults["slew_config"], static_existing["slew_config"])
        if "esp32" in static_existing and isinstance(static_existing["esp32"], dict):
            state["esp32"] = _merge_defaults(defaults["esp32"], static_existing["esp32"])
        if "mount" in static_existing and isinstance(static_existing["mount"], dict):
            state["mount"] = _merge_defaults(defaults["mount"], static_existing["mount"])

    if "slew_config" in telescope_legacy and isinstance(telescope_legacy["slew_config"], dict):
        state["slew_config"] = _merge_defaults(state["slew_config"], telescope_legacy["slew_config"])

    return state


def _migrate_client_profile() -> Dict[str, Any]:
    defaults = _default_client_profile()
    profile_existing = _load_json(CLIENT_PROFILE_FILE)
    static_existing = _load_json(STATIC_STATE_FILE)
    location_legacy = _load_json(LEGACY_LOCATION_FILE) or {}

    profile = deepcopy(defaults)

    if profile_existing:
        profile = _merge_defaults(defaults, profile_existing)

    if static_existing:
        # Migration from previously combined config/client_config.json
        nested_cfg = static_existing.get("client_config") if isinstance(static_existing.get("client_config"), dict) else {}
        migrated_client_cfg = {
            "client_id": nested_cfg.get("client_id")
            or static_existing.get("client_id")
            or profile["client_config"].get("client_id", ""),
            "base_url": nested_cfg.get("base_url")
            or static_existing.get("base_url")
            or static_existing.get("server_url")
            or static_existing.get("http_url")
            or profile["client_config"].get("base_url", ""),
            "api_token": nested_cfg.get("api_token")
            or static_existing.get("api_token")
            or profile["client_config"].get("api_token", ""),
        }
        profile["client_config"] = _merge_defaults(profile["client_config"], migrated_client_cfg)
        if "location" in static_existing and isinstance(static_existing["location"], dict):
            profile["location"] = _merge_defaults(profile["location"], static_existing["location"])

    if location_legacy:
        profile["location"] = _merge_defaults(profile["location"], location_legacy)

    if profile["client_config"].get("base_url"):
        profile["client_config"]["base_url"] = _normalize_base_url(profile["client_config"]["base_url"])

    return profile


def _migrate_runtime_state() -> Dict[str, Any]:
    defaults = _default_runtime_state()
    runtime_existing = _load_json(RUNTIME_STATE_FILE)
    telescope_legacy = _load_json(LEGACY_TELESCOPE_STATE_FILE) or {}
    liveview_legacy = _load_json(LEGACY_LIVEVIEW_FILE) or {}

    state = deepcopy(defaults)
    if runtime_existing:
        state = _merge_defaults(defaults, runtime_existing)

    # Bring over latest positional fields from legacy state files.
    for key in (
        "current_right_ascension",
        "current_declination",
        "target_right_ascension",
        "target_declination",
        "source",
        "updated_at",
        "current_hour_angle",
    ):
        if key in telescope_legacy:
            state[key] = telescope_legacy[key]

    # Legacy compatibility for old coordinate keys.
    if "current_right_ascension" not in telescope_legacy and "right_ascension" in telescope_legacy:
        state["current_right_ascension"] = telescope_legacy["right_ascension"]
    if "current_declination" not in telescope_legacy and "declination" in telescope_legacy:
        state["current_declination"] = telescope_legacy["declination"]

    if "enabled" in liveview_legacy:
        state["liveview_enabled"] = bool(liveview_legacy["enabled"])

    return state


def ensure_state_files() -> None:
    global _STATE_READY
    if _STATE_READY:
        return
    remove_stale_temp_files()

    if (
        _has_valid_state_file(CLIENT_PROFILE_FILE)
        and _has_valid_state_file(STATIC_STATE_FILE)
        and _has_valid_state_file(RUNTIME_STATE_FILE)
    ):
        _STATE_READY = True
        return

    client_profile = _migrate_client_profile()
    static_state = _migrate_static_state()
    runtime_state = _migrate_runtime_state()
    _save_json(CLIENT_PROFILE_FILE, client_profile)
    _save_json(STATIC_STATE_FILE, static_state)
    _save_json(RUNTIME_STATE_FILE, runtime_state)
    _STATE_READY = True


def load_client_profile() -> Dict[str, Any]:
    ensure_state_files()
    return _load_json(CLIENT_PROFILE_FILE) or _default_client_profile()


def save_client_profile(profile: Dict[str, Any]) -> None:
    global _STATE_READY
    normalized = _merge_defaults(_default_client_profile(), profile)
    base_url = normalized["client_config"].get("base_url", "")
    if base_url:
        normalized["client_config"]["base_url"] = _normalize_base_url(base_url)
    _save_json(CLIENT_PROFILE_FILE, normalized)
    _STATE_READY = True


def load_static_state() -> Dict[str, Any]:
    ensure_state_files()
    return _load_json(STATIC_STATE_FILE) or _default_static_state()


def save_static_state(state: Dict[str, Any]) -> None:
    global _STATE_READY
    normalized = _merge_defaults(_default_static_state(), state)
    _save_json(STATIC_STATE_FILE, normalized)
    _STATE_READY = True


class RuntimeStore:
    """config/runtime_state.json, held in memory.

    Reads come from memory. Changes are written out in the background, at
    most every FLUSH_DELAY_S (when="soon": the axes while tracking), at once
    (when="now": a stop, a sync, a park) or not at all (when="never": values
    worked out from others). Before this, the file was rewritten every
    second whether anything had changed or not."""

    FLUSH_DELAY_S = 5.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._path: Optional[str] = None
        self._data: Dict[str, Any] = {}
        self._dirty = False
        self._timer: Optional[threading.Timer] = None

    def _ensure_loaded(self) -> None:
        # Reload when the path changes (the tests point it at copies).
        if self._path == RUNTIME_STATE_FILE:
            return
        ensure_state_files()
        loaded = _load_json(RUNTIME_STATE_FILE)
        self._data = _merge_defaults(_default_runtime_state(), loaded or {})
        self._path = RUNTIME_STATE_FILE
        self._dirty = False

    def reload(self) -> None:
        with self._lock:
            self._path = None
            self._ensure_loaded()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._ensure_loaded()
            return deepcopy(self._data.get(key, default))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            return deepcopy(self._data)

    def update(self, changes: Dict[str, Any], when: str = "soon") -> None:
        with self._lock:
            self._ensure_loaded()
            self._data.update(deepcopy(changes))
            if when == "never":
                return
            self._dirty = True
            if when == "now":
                self._flush_locked()
            elif self._timer is None:
                self._timer = threading.Timer(self.FLUSH_DELAY_S, self.flush)
                self._timer.daemon = True
                self._timer.start()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._dirty or self._path is None:
            return
        try:
            _save_json(self._path, self._data)
            self._dirty = False
        except OSError as exc:
            log.warning(f"Couldn't save the runtime state: {exc}")


_RUNTIME_STORE = RuntimeStore()
atexit.register(_RUNTIME_STORE.flush)


def runtime_store() -> RuntimeStore:
    return _RUNTIME_STORE


def load_runtime_state() -> Dict[str, Any]:
    return _RUNTIME_STORE.snapshot()


def save_runtime_state(state: Dict[str, Any]) -> None:
    global _STATE_READY
    _RUNTIME_STORE.update(state, when="now")
    _STATE_READY = True


def get_client_config() -> Dict[str, Any]:
    profile = load_client_profile()
    client_config = profile.get("client_config", {})
    merged = _merge_defaults(_default_client_profile()["client_config"], client_config)
    if merged.get("base_url"):
        merged["base_url"] = _normalize_base_url(merged.get("base_url", ""))
    return merged


def save_client_config(client_config: Dict[str, Any]) -> None:
    profile = load_client_profile()
    profile["client_config"] = _merge_defaults(profile.get("client_config", {}), client_config)
    save_client_profile(profile)


def get_location() -> Dict[str, Any]:
    profile = load_client_profile()
    location = profile.get("location", {})
    return location if isinstance(location, dict) else {}


def save_location(location: Dict[str, Any]) -> None:
    profile = load_client_profile()
    profile["location"] = location
    save_client_profile(profile)


def get_missing_required_client_fields() -> list[str]:
    cfg = get_client_config()
    missing: list[str] = []
    for field in ("client_id", "base_url", "api_token"):
        value = cfg.get(field)
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
    return missing


def is_offline_mode() -> bool:
    try:
        return bool(get_client_config().get("offline", False))
    except Exception:
        return False


def set_offline_mode(on: bool) -> None:
    save_client_config({"offline": bool(on)})


def get_slew_config() -> Dict[str, float]:
    state = load_static_state()
    defaults = _default_static_state()["slew_config"]
    config = state.get("slew_config", {})
    merged = _merge_defaults(defaults, config if isinstance(config, dict) else {})
    return {
        "slew_speed_sps": float(merged.get("slew_speed_sps", defaults["slew_speed_sps"])),
        "refine_speed_sps": float(merged.get("refine_speed_sps", defaults["refine_speed_sps"])),
        "slew_threshold_degrees": float(merged.get("slew_threshold_degrees", defaults["slew_threshold_degrees"])),
        "center_threshold_degrees": float(merged.get("center_threshold_degrees", defaults["center_threshold_degrees"])),
        "centered_threshold_degrees": float(merged.get("centered_threshold_degrees", defaults["centered_threshold_degrees"])),
        "ra_gear_ratio": float(merged.get("ra_gear_ratio", defaults["ra_gear_ratio"])),
        "dec_gear_ratio": float(merged.get("dec_gear_ratio", defaults["dec_gear_ratio"])),
    }


def get_section(name: str) -> Dict[str, Any]:
    """One section of config/client_config.json with its defaults filled in."""
    defaults = _default_static_state().get(name, {})
    saved = load_static_state().get(name, {})
    return _merge_defaults(defaults, saved if isinstance(saved, dict) else {})


def save_section(name: str, values: Dict[str, Any]) -> None:
    """Change some settings in one section of config/client_config.json."""
    state = load_static_state()
    current = state.get(name) if isinstance(state.get(name), dict) else {}
    current = dict(current)
    current.update(values)
    state[name] = current
    save_static_state(state)


def get_mount_config() -> Dict[str, Any]:
    defaults = _default_static_state()["mount"]
    saved = load_static_state().get("mount", {})
    merged = _merge_defaults(defaults, saved if isinstance(saved, dict) else {})
    try:
        ra = [float(v) for v in merged["ra_axis_limits"]][:2]
        dec = [float(v) for v in merged["dec_axis_limits"]][:2]
        if len(ra) != 2 or len(dec) != 2 or ra[0] >= ra[1] or dec[0] >= dec[1]:
            raise ValueError
    except (TypeError, ValueError, KeyError):
        log.info("Bad mount limits in config/client_config.json; using the defaults")
        ra, dec = defaults["ra_axis_limits"], defaults["dec_axis_limits"]
    hemisphere = str(merged.get("hemisphere", "auto")).lower()
    if hemisphere not in ("auto", "north", "south"):
        hemisphere = "auto"
    try:
        park = [float(v) for v in merged.get("park_axes", defaults["park_axes"])][:2]
        backlash = [max(0, int(v)) for v in merged.get("backlash_steps", [0, 0])][:2]
        if len(park) != 2 or len(backlash) != 2:
            raise ValueError
    except (TypeError, ValueError):
        park, backlash = list(defaults["park_axes"]), [0, 0]
    return {"ra_axis_limits": ra, "dec_axis_limits": dec, "pole_crossing": bool(merged.get("pole_crossing", True)),
        "hemisphere": hemisphere, "park_axes": park, "backlash_steps": backlash}


def save_slew_config(slew_config: Dict[str, Any]) -> None:
    state = load_static_state()
    current = state.get("slew_config", {})
    state["slew_config"] = _merge_defaults(current if isinstance(current, dict) else {}, slew_config)
    save_static_state(state)
