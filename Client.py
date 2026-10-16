# Main client entry point

from asyncio import run
import sys
import time
from core.networking.websocket import websocketClient, cleanup_camera
from core.camera.controller import Camera
from utils.location import update_location_config
from utils.camera_state import camera_state
from utils.esp32_state import esp32_state
from utils.config_state import (
    get_client_config,
    ensure_state_files,
    get_missing_required_client_fields,
    is_offline_mode,
)
from utils.LEDmanager import get_led_manager


def _wait_for_required_client_config() -> None:
    # The front panel asks for the device name, server and API key on first
    # run (utils/panel_ui.py); config/client_profile.json or
    # 'python Client.py setup' work too. Keep trying to reach the ESP32 while
    # waiting, so the panel can take part even if it was late. Offline mode
    # (chosen on the panel) needs none of them.
    if not get_missing_required_client_fields() or is_offline_mode():
        return
    print("\n[config] Missing required client configuration: client_id, base_url, api_token")
    print("[config] Set them on the front panel (or choose offline mode there), in config/client_profile.json, or with 'python Client.py setup'")
    while get_missing_required_client_fields() and not is_offline_mode():
        if esp32_state.get_connection() is None:
            esp32_state.ensure_connection()
        time.sleep(2.0)
    print("[config] Offline mode" if is_offline_mode() else "[config] Configuration complete")


def load_local_config():
    try:
        ensure_state_files()
    except Exception as e:
        print(f"Failed to read configuration files: {e}")
        raise SystemExit(1)
    _wait_for_required_client_config()
    return get_client_config()


def _connect_and_sync_esp32() -> None:
    """Attempt initial ESP32 connection and asset sync on startup.
    
    This runs before the websocket client starts, ensuring ESP32 is
    available and assets are synced during the startup flow.
    """
    try:
        print("[startup] Attempting to connect to ESP32...")
        
        # Get or establish connection using centralized singleton
        try:
            conn = esp32_state.ensure_connection()
            if conn:
                print(f"[startup] ESP32 connected successfully on {conn.cfg.port}")
            
            # Now sync assets using the established connection (runs in background)
            try:
                from graphics.assets_player import sync_assets_on_connect
                print("[startup] Starting asset sync to ESP32...")
                sync_assets_on_connect(conn)
            except Exception as e:
                print(f"[startup] Asset sync error: {e}")
        except Exception as conn_error:
            print(f"[startup] ESP32 connection failed: {conn_error}")
            print("[startup] ESP32 not available - will scan continuously in background")
            esp32_state.set_available(False)
    except Exception as e:
        print(f"[startup] Unexpected error during ESP32 connection: {e}")
        esp32_state.set_available(False)


if __name__ == "__main__":
    try:
        # Clean up any lingering processes from previous runs
        print("[startup] Cleaning up previous camera processes...")
        cleanup_camera()
        time.sleep(1)
        
        # Quick non-blocking camera check
        print("[startup] Checking for camera...")
        if Camera.ensureConnection(retryCount=1, delaySeconds=0):
            print("[startup] Camera detected and ready")
            camera_state.set_available(True)
            # Release viewfinder to ensure camera is in clean state
            try:
                Camera.releaseViewfinder()
            except:
                pass
        else:
            print("[startup] Camera not detected - will scan continuously in background")
            print("[startup] Connect camera via USB and it will be detected automatically")
            camera_state.set_available(False)
        
        # Update GPS location on startup (no IP lookup in offline mode)
        update_location_config(use_network=not is_offline_mode())
        
        # Attempt initial ESP32 connection and asset sync
        _connect_and_sync_esp32()
        
        if len(sys.argv) > 1 and sys.argv[1] == "setup":
            from core.networking.websocket import setup_client_config
            setup_client_config()
            raise SystemExit(0)

        cfg = load_local_config()
        
        # Run the main websocket client (camera scanner and ESP32 scanner run as background tasks)
        run(websocketClient(cfg))
    except KeyboardInterrupt:
        print("[global] KeyboardInterrupt received, exiting and releasing camera...")
        try:
            get_led_manager().shutdown()
        except Exception:
            pass
        cleanup_camera()
