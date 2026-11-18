"""Which server the client talks to, and how.

Online: the server set on the panel (client_profile.json), as the device
name with its API token. Offline: none (phones on the hotspot talk to the
telescope directly: core/networking/phone_link.py).

Everything that reaches a server (the command and live view sockets in
core/networking/websocket.py, photo uploads in utils/uploads.py) asks here,
so switching between them needs no restart.
"""
from __future__ import annotations

from typing import Dict, Optional

from utils.config_state import build_service_urls, get_client_config, is_offline_mode


def online_link() -> Optional[Dict[str, str]]:
	"""The server on the panel, when it's set up (even in offline mode)."""
	cfg = get_client_config()
	client_id = str(cfg.get("client_id") or "").strip()
	token = str(cfg.get("api_token") or "").strip()
	base = str(cfg.get("base_url") or "").strip()
	if not (client_id and token and base):
		return None
	try:
		urls = build_service_urls(base)
	except ValueError:
		return None
	return {"kind": "server", "client_id": client_id, "api_token": token, "http_url": urls["http_url"],
		"server_uri": urls["server_uri"], "liveview_uri": urls["liveview_uri"]}


def current() -> Optional[Dict[str, str]]:
	if is_offline_mode():
		return None
	return online_link()


def kind() -> Optional[str]:
	""""server" or None."""
	link = current()
	return link["kind"] if link else None
