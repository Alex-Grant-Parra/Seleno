"""Live view on or off for the server (core/camera/stream.py).

Held in memory: it was read from config/runtime_state.json hundreds of
times a second while streaming. Every client start begins with it off.
"""
from core.camera.stream import get_live_stream


def is_liveview_enabled() -> bool:
    return get_live_stream().enabled


def save_liveview_state(enabled: bool) -> None:
    stream = get_live_stream()
    if bool(enabled) == stream.enabled:
        return
    if enabled:
        stream.start()
    else:
        stream.stop()


load_liveview_state = is_liveview_enabled
