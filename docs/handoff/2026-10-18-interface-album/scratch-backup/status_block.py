# Status messages pushed by telescopes (type "status"), stored rather than printed
_status_drop_log = {}


def _clean_status(data):
    # Keep known parts that are objects or plain values; a malformed part becomes an error part
    cleaned = {}
    for key, value in data.items():
        if key in STATUS_PARTS:
            if value is None or isinstance(value, (dict, str, int, float, bool)):
                cleaned[key] = value
            else:
                cleaned[key] = {"error": "malformed status part"}
        elif value is None or isinstance(value, (str, int, float, bool)):
            cleaned[key] = value
    return cleaned


def _store_status(client_id, client_name, raw_message, data):
    if len(raw_message) > MAX_STATUS_BYTES:
        now = time.time()
        if now - _status_drop_log.get(client_id, 0) > 60:
            _status_drop_log[client_id] = now
            print(f"[Status] Dropped {len(raw_message)} byte status message from {client_id} (limit {MAX_STATUS_BYTES})")
        return

    status = _clean_status(data)
    received_at = time.time()
    latest_status[client_id] = (received_at, status)
    background_pool.submit(_apply_status, client_name, status, received_at)


def _apply_status(client_name, status, received_at):
    # Database side of a status message: last_seen, and capture plan progress
    try:
        from Server import app
        with app.app_context():
            if received_at - _status_last_seen_write.get(client_name, 0) >= 15:
                _status_last_seen_write[client_name] = received_at
                from models.tables import Telescope
                Telescope.update_last_seen(client_name, received_at)

            from app.capture_plans import plan_status_from_status, record_plan_status
            plan_status = plan_status_from_status(status)
            if plan_status:
                record_plan_status(client_name, plan_status)
    except Exception:
        logger.exception("Failed to apply status message from %s", client_name)


def get_latest_status(client_id):
    # (age in seconds, status message) for a telescope, or (None, None)
    entry = latest_status.get(client_id)
    if not entry:
        return None, None
    received_at, status = entry
    return max(0.0, time.time() - received_at), status


def _catch_up_capture_plan(client_id, client_name):
    # After a reconnect, ask once for the state of a plan that was running
    time.sleep(3)
    try:
        from Server import app
        from app.capture_plans import catch_up_after_reconnect
        with app.app_context():
            catch_up_after_reconnect(client_id, client_name)
    except Exception:
        logger.exception("Capture plan catch-up failed for %s", client_name)

