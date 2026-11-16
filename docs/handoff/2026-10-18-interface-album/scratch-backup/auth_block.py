# Requests from this process's own routes: loopback, no proxy headers, and the per-process key
def _is_internal_request():
    try:
        host = (request.remote_addr or '').strip()
        if host not in ('127.0.0.1', '::1', 'localhost'):
            return False
        # The Cloudflare tunnel also connects from localhost, but adds these headers
        if any(request.headers.get(name) for name in _PROXY_HEADERS):
            return False
        presented = request.headers.get(INTERNAL_COMMAND_HEADER) or ''
        return hmac.compare_digest(presented, INTERNAL_COMMAND_KEY)
    except Exception:
        return False


def _token_may_command(auth_result, client_id):
    # A telescope's token drives only that telescope; admin and developer tokens drive any
    if (auth_result.get('client_type') or '').strip().lower() in CROSS_TELESCOPE_TOKEN_TYPES:
        return True
    return bool(client_id) and auth_result.get('name') == client_id


# Auth handler
def _authorize_send_command(client_id):
    try:
        if current_user and getattr(current_user, 'is_authenticated', False) and getattr(current_user, 'is_admin', False):
            return True, 'admin'
    except Exception:
        pass

    if _is_internal_request():
        return True, 'internal'

    token = _extract_api_token_from_request()
    if token:
        client_ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        auth_result = authenticate_token_with_policy(
            token,
            client_id=client_id,
            client_ip=(client_ip.split(',')[0].strip() if client_ip else 'unknown'),
        )
        if auth_result.get('ok'):
            if _token_may_command(auth_result, client_id):
                return True, auth_result.get('source', 'token')
            return False, 'token_scope'

    return False, 'forbidden'

# Start WebSocket Server in Background
def start_ws_server():
    global ws_event_loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    ws_event_loop = loop  # Store reference for thread-safe operations
    async def run_server():
        try:
            async with websockets.serve(
                handle_client,
                WS_IP,
                WS_PORT,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT
            ):
                await asyncio.Future()
        except Exception as e:
            # Single write so concurrent thread output doesn't interleave on one line
            print(f"[CommandWS] WebSocket server failed to start: {e}\n", end="", flush=True)
    try:
        loop.run_until_complete(run_server())
        loop.run_forever()
    except Exception as e:
        print(f"[CommandWS] Event loop error: {e}")

# Start both websocket servers in daemon threads
def start_websocket_servers():
    
    from socket import gethostname
    from app.liveview import start_liveview_ws_server
    
    # Start rate limit cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_rate_limit_tracking, daemon=True)
    cleanup_thread.start()
    print("[Rate Limiting] Started cleanup thread for tracking data")
    
    threading.Thread(target=start_ws_server, daemon=True).start()
    print(f"Starting websocket command server on {gethostname()} at port: {commandPort}")

    threading.Thread(target=start_liveview_ws_server, daemon=True).start()
    print(f"Starting liveView server on {gethostname()} at port: {LiveViewPort}")


def _request_timeout(value):
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return DEFAULT_COMMAND_TIMEOUT
    return min(max(timeout, 1.0), MAX_COMMAND_TIMEOUT)

# Send command
def send_command_handler():
    data = request.get_json(silent=True) or {}
    client_id = data.get('client_id')
    command = data.get('command')
    args = data.get('args', [])
    kwargs = data.get('kwargs', {})  # Extract kwargs from request
    timeout = _request_timeout(data.get('timeout'))

    if not client_id or not command:
        return jsonify({"status": "error", "error": "client_id and command are required"}), 400

    allowed, auth_source = _authorize_send_command(client_id)
    if not allowed:
        _security_log(
            'send_command_denied',
            client_id=client_id,
            command=command,
            reason=auth_source,
            ip=request.headers.get('X-Forwarded-For', request.remote_addr),
        )
        return jsonify({"status": "error", "error": "Unauthorized"}), 403

    _security_log(
        'send_command_allowed',
        auth_source=auth_source,
        client_id=client_id,
        command=command,
        ip=request.headers.get('X-Forwarded-For', request.remote_addr),
    )

    try:
        result = client_manager.command(client_id, command, args, kwargs, timeout=timeout)
        return jsonify({"status": "success", "result": result})
    except ClientError as e:
        return jsonify({"status": "error", "error": str(e)}), 502
    except Exception:
        logger.exception("send_command failed for client %s", client_id)
        return jsonify({"status": "error", "error": "Command failed"}), 500

# Client disconnect handler for admin interface
def admin_disconnect_ws_client_handler(client_id):
    
    try:
        if not current_user or not getattr(current_user, 'is_authenticated', False) or not getattr(current_user, 'is_admin', False):
            return jsonify({'status': 'error', 'error': 'Admin access required'}), 403
    except Exception:
        return jsonify({'status': 'error', 'error': 'Admin access required'}), 403

    existing = client_manager.get_client(client_id)
    if not existing:
        return jsonify({'status': 'error', 'error': 'Client not connected'}), 404

    global ws_event_loop
    if ws_event_loop is None:
        return jsonify({'status': 'error', 'error': 'WebSocket event loop not initialized'}), 500

    try:
        fut = asyncio.run_coroutine_threadsafe(
            existing.ws.close(code=4010, reason='Admin override disconnect'),
            ws_event_loop,
        )
        fut.result(timeout=5)
        client_manager.remove_client(client_id)
        _security_log('ws_command_admin_disconnect', client_id=client_id, by=getattr(current_user, 'id', None))
        return jsonify({'status': 'success', 'message': f"Disconnected '{client_id}'"})
    except Exception:
        logger.exception("Admin disconnect failed for client %s", client_id)
        return jsonify({'status': 'error', 'error': 'Failed to disconnect client'}), 500


def disconnect_client_by_id(client_id, reason='admin_disconnect'):
    """
    Programmatic helper to disconnect a connected websocket client by client_id.
    `<client_id>_liveview` closes that telescope's live-view socket and its viewers.
    Returns True if a client was disconnected, False otherwise.
    """
    if str(client_id).endswith('_liveview'):
        try:
            from app.liveview import disconnect_liveview
            return disconnect_liveview(str(client_id)[:-len('_liveview')], reason=reason)
        except Exception:
            return False

    try:
        existing = client_manager.get_client(client_id)
        if not existing:
            return False

        global ws_event_loop
        if ws_event_loop is None:
            return False

        fut = asyncio.run_coroutine_threadsafe(
            existing.ws.close(code=4011, reason=reason),
            ws_event_loop,
        )
        fut.result(timeout=5)
        client_manager.remove_client(client_id)
        _security_log('ws_command_programmatic_disconnect', client_id=client_id, reason=reason)
        return True
    except Exception:
        return False
    
