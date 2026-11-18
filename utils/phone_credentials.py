"""What the phone link needs from seleno.org, fetched while online.

core/networking/phone_link.py serves the seleno.org app on the hotspot over
HTTPS at the telescope's own name. seleno.org issues, for this telescope:
* the name: <slug>.tel.<domain>, pointing at the hotspot's address;
* its certificate and key (Let's Encrypt), renewed there;
* the secret it signs users' offline passes with, which this checks them with;
* the app's origins (for CORS), and an offline pack for search and basic
  mode's suggestions (utils/offline_pack.py).

GET <server>/api/telescope/offline_credentials with the telescope's token,
when online: at start, then every REFRESH_S, and sooner while anything is
missing. Kept in config/phone_link/ (owner-only files), so they're there
offline.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, Optional

from utils.log import get_logger

log = get_logger("phone_credentials")

FOLDER = os.path.join("config", "phone_link")
CREDENTIALS = "credentials.json"
CERT = "cert.pem"
KEY = "key.pem"
PATH = "api/telescope/offline_credentials"
REFRESH_S = 12 * 3600
RETRY_S = 10 * 60

state: Dict[str, Any] = {"at": 0.0, "error": "", "certificate": ""}
_thread: Optional[threading.Thread] = None


def _write(path: str, text: str, private: bool = False) -> None:
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = f"{path}.tmp"
	fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
	with os.fdopen(fd, "w") as f:
		f.write(text)
	os.replace(tmp, path)


def load() -> Optional[Dict[str, Any]]:
	"""The stored credentials, with the paths of the certificate and key
	(None when there's no certificate yet)."""
	try:
		with open(os.path.join(FOLDER, CREDENTIALS)) as f:
			creds = json.load(f)
	except (OSError, ValueError):
		return None
	if not isinstance(creds, dict):
		return None
	cert, key = os.path.join(FOLDER, CERT), os.path.join(FOLDER, KEY)
	if os.path.exists(cert) and os.path.exists(key):
		creds.update(cert=cert, key=key)
	return creds


def save(data: Dict[str, Any]) -> None:
	from utils import offline_pack

	creds = {k: data.get(k) for k in ("telescope_id", "host", "origins", "secret", "expires", "address")}
	if data.get("cert") and data.get("key"):
		_write(os.path.join(FOLDER, KEY), data["key"], private=True)
		_write(os.path.join(FOLDER, CERT), data["cert"])
	_write(os.path.join(FOLDER, CREDENTIALS), json.dumps(creds, indent=2), private=True)
	if isinstance(data.get("pack"), dict):
		offline_pack.save(data["pack"])


def refresh() -> Optional[str]:
	"""Fetch them now; returns an error message, or None."""
	import requests

	from utils import server_link
	from utils.config_state import is_offline_mode

	link = server_link.online_link()
	if is_offline_mode() or link is None:
		return "offline"
	url = link["http_url"].rstrip("/") + "/" + PATH
	try:
		response = requests.get(url, headers={"Authorization": f"Bearer {link['api_token']}"}, timeout=(10, 60))
	except requests.RequestException as exc:
		return str(exc)[:80]
	if response.status_code != 200:
		return f"{response.status_code}: {response.text[:80]}"
	try:
		data = response.json()
	except ValueError:
		return "not JSON"
	save(data)
	state.update(at=time.time(), error="", certificate=data.get("certificate") or ("ready" if data.get("cert") else "pending"))
	log.info("Phone link credentials: %s, certificate %s", data.get("host"), state["certificate"])
	try:
		from core.networking import phone_link

		phone_link.apply()
	except Exception:
		pass
	return None


def _complete() -> bool:
	creds = load()
	return bool(creds and creds.get("secret") and creds.get("cert"))


def _loop() -> None:
	while True:
		error = refresh()
		if error and error != "offline":
			state["error"] = error
			log.info("Phone link credentials not fetched: %s", error)
		time.sleep(REFRESH_S if not error and _complete() else RETRY_S)


def start() -> None:
	global _thread
	if _thread is None or not _thread.is_alive():
		_thread = threading.Thread(target=_loop, name="phone-credentials", daemon=True)
		_thread.start()


def status_text() -> str:
	creds = load()
	if not creds:
		return "none yet (online once)"
	if not creds.get("cert"):
		return "certificate pending"
	return str(creds.get("host") or "ready")[:40]
