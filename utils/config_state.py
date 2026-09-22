import json
import os
import threading
from copy import deepcopy
from typing import Any, Dict

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
    raw_url = (raw_url or "").strip()
    if not raw_url:
        return "https://telescopes.dev/"
    if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
        raw_url = f"https://{raw_url}"
    return raw_url.rstrip("/") + "/"


def build_service_urls(base_url: str) -> Dict[str, str]:
    normalized = _normalize_base_url(base_url)
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
        "esp32": {
            "port": "/dev/ttyUSB0",
            "baudrate": 115200,
            "timeout": 0.5,
        },
    }


def _default_client_profile() -> Dict[str, Any]:
    return {
        "client_config": {
            "client_id": "",
            "base_url": "",
            "api_token": "",
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


def _save_json(path: str, payload: Dict[str, Any]) -> None:
    # Write to a temp file and rename over the target, so a concurrent reader never
    # sees a truncated file (which would load as defaults and be saved back as zeros).
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)


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


def load_runtime_state() -> Dict[str, Any]:
    ensure_state_files()
    return _load_json(RUNTIME_STATE_FILE) or _default_runtime_state()


def save_runtime_state(state: Dict[str, Any]) -> None:
    global _STATE_READY
    with runtime_state_lock:
        existing = _load_json(RUNTIME_STATE_FILE) or {}
        normalized = _merge_defaults(_default_runtime_state(), existing)
        normalized = _merge_defaults(normalized, state)
        _save_json(RUNTIME_STATE_FILE, normalized)
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


def save_slew_config(slew_config: Dict[str, Any]) -> None:
    state = load_static_state()
    current = state.get("slew_config", {})
    state["slew_config"] = _merge_defaults(current if isinstance(current, dict) else {}, slew_config)
    save_static_state(state)
