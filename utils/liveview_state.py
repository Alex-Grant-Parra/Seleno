from utils.config_state import load_runtime_state, save_runtime_state, runtime_state_lock

def load_liveview_state() -> bool:
    # Load live view state from runtime state file.
    state = load_runtime_state()
    return bool(state.get("liveview_enabled", False))


def save_liveview_state(enabled: bool) -> None:
    # Persist live view state to runtime state file.
    with runtime_state_lock:
        state = load_runtime_state()
        state["liveview_enabled"] = bool(enabled)
        save_runtime_state(state)


def is_liveview_enabled() -> bool:
    # Check current live view state by reading the state file.
    return load_liveview_state()
