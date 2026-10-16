# Main client entry point

from asyncio import run
import sys
import time
from utils import log as logging_setup
from core.networking.websocket import websocketClient, shutdown
from utils.location import update_location_config
from utils.esp32_state import esp32_state
from utils.config_state import (
    get_client_config,
    ensure_state_files,
    get_missing_required_client_fields,
    is_offline_mode,
)
from utils.log import get_logger

log = get_logger("startup")


def _wait_for_required_client_config() -> None:
    # The front panel asks for the device name, server and API key on first
    # run (utils/panel_ui.py); config/client_profile.json or
    # 'python Client.py setup' work too. Keep trying to reach the ESP32 while
    # waiting, so the panel can take part even if it was late. Offline mode
    # (chosen on the panel) needs none of them.
    if not get_missing_required_client_fields() or is_offline_mode():
        return
    log.warning("Missing required client configuration: client_id, base_url, api_token")
    log.info("Set them on the front panel (or choose offline mode there), in config/client_profile.json, or with 'python Client.py setup'")
    while get_missing_required_client_fields() and not is_offline_mode():
        if esp32_state.get_connection() is None:
            esp32_state.ensure_connection()
        time.sleep(2.0)
    log.info("Offline mode" if is_offline_mode() else "Configuration complete")


def load_local_config():
    try:
        ensure_state_files()
    except Exception as e:
        log.error(f"Failed to read configuration files: {e}")
        raise SystemExit(1)
    _wait_for_required_client_config()
    return get_client_config()


def _connect_esp32() -> None:
    """Try the ESP32 once at startup; the scanner keeps trying after."""
    try:
        log.info("Attempting to connect to ESP32...")
        conn = esp32_state.ensure_connection()
        if conn:
            log.info(f"ESP32 connected on {conn.cfg.port}")
        else:
            log.warning("ESP32 not available - will scan continuously in background")
    except Exception as e:
        log.warning(f"ESP32 connection failed: {e}")
        esp32_state.set_available(False)


if __name__ == "__main__":
    logging_setup.setup()
    try:
        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            from core.networking.websocket import setup_client_config
            setup_client_config()
            raise SystemExit(0)

        ensure_state_files()
        # The camera service finds the camera itself (and owns it); GPS is
        # read in the background. Both start before anything waits.
        from core.camera.service import get_camera_service
        from utils import gps

        get_camera_service().start()
        gps.start()

        # Location: a GPS fix if there's one yet, the site typed on the panel,
        # or (online) an IP lookup.
        update_location_config(use_network=not is_offline_mode())

        _connect_esp32()

        cfg = load_local_config()

        # Run the server links and the background tasks.
        run(websocketClient(cfg))
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received, exiting and releasing camera...")
        shutdown()
