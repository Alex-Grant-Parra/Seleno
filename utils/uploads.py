"""Uploading photos to the server, from a queue that survives restarts.

Photos used to be uploaded straight after capture, all files in one
request, with no second try: a dropped connection lost the upload (the
files stayed on the Pi, unsent). Now:

* each file is queued (photos/upload_queue.json) and sent on its own by a
  background thread, while the client is online;
* JPEGs go first. RAW files (the big ones) follow the policy in
  config upload.raw: "after_jpeg" (sent once no JPEG is waiting),
  "on_request" (held until uploadPending("raw")) or "never";
* a failed send is tried again later, waiting longer each time (up to
  upload.retry_max_s); a refusal (bad name or type) gives up after three
  tries;
* the server files a photo under the account id that starts its name
  ("<id>_..."), so each entry carries its owner id;
* a JSON description of where the telescope pointed is sent with each
  file (form field "meta"; the server ignores fields it doesn't know).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from utils.log import get_logger

log = get_logger("uploads")

QUEUE_NAME = "upload_queue.json"
RAW_EXTENSIONS = {"cr2", "cr3", "nef", "arw", "orf", "rw2", "dng", "raf", "pef", "srw", "nrw"}
JPEG_EXTENSIONS = {"jpg", "jpeg"}
GIVE_UP_AFTER_REFUSALS = 3


def kind_of(path: str) -> str:
	ext = os.path.splitext(path)[1].lower().lstrip(".")
	if ext in JPEG_EXTENSIONS:
		return "jpeg"
	if ext in RAW_EXTENSIONS:
		return "raw"
	return "other"


def _mime(path: str) -> str:
	ext = os.path.splitext(path)[1].lower().lstrip(".")
	return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "tif": "image/tiff",
		"tiff": "image/tiff", "fits": "application/fits"}.get(ext, "application/octet-stream")


def upload_name(path: str, owner: str) -> str:
	base = os.path.basename(path)
	return base if base.startswith(f"{owner}_") else f"{owner}_{base}"


def _settings() -> Dict[str, Any]:
	try:
		from utils.config_state import get_section

		return get_section("upload")
	except Exception:
		return {"raw": "after_jpeg", "retry_max_s": 600}


def _default_sender(path: str, name: str, meta: Dict[str, Any]) -> None:
	"""POST one file to <server>/upload. Raises UploadRefused for a 4xx
	(not 429), anything else for a failure worth retrying."""
	import requests

	from core.networking.csrf import SESSION, get_csrf_token
	from utils.config_state import build_service_urls, get_client_config

	base = get_client_config().get("base_url")
	if not base:
		raise RuntimeError("No server set")
	url = build_service_urls(base)["http_url"].rstrip("/")
	headers: Dict[str, str] = {}
	data: Dict[str, str] = {"meta": json.dumps(meta)}
	token = get_csrf_token(url)
	if token:
		headers["X-CSRF-Token"] = token
		headers["X-CSRFToken"] = token
		data["csrf_token"] = token
	with open(path, "rb") as f:
		response = SESSION.post(f"{url}/upload", files={"file0": (name, f, _mime(path))},
			headers=headers, data=data, timeout=(10, 300))
	if 400 <= response.status_code < 500 and response.status_code != 429:
		raise UploadRefused(f"{response.status_code}: {response.text[:120]}")
	try:
		response.raise_for_status()
	except requests.RequestException as exc:
		raise RuntimeError(str(exc)[:120]) from exc


class UploadRefused(RuntimeError):
	pass


class UploadQueue:
	def __init__(self, folder: Optional[str] = None, sender: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
			online: Optional[Callable[[], bool]] = None, autostart: bool = True) -> None:
		self._folder = folder
		self._sender = sender or _default_sender
		self._online = online or _server_online
		self._autostart = autostart
		self._lock = threading.RLock()
		self._sending = threading.Lock()  # one upload at a time
		self._entries: Optional[List[Dict[str, Any]]] = None
		self._wake = threading.Event()
		self._thread: Optional[threading.Thread] = None
		self.sent = 0
		self.last_error = ""
		self.current = ""

	@property
	def path(self) -> str:
		if self._folder is None:
			from utils import photos

			return os.path.join(photos.base_dir(), QUEUE_NAME)
		return os.path.join(self._folder, QUEUE_NAME)

	def _load(self) -> List[Dict[str, Any]]:
		if self._entries is None:
			try:
				with open(self.path) as f:
					data = json.load(f)
				self._entries = [e for e in data.get("entries", []) if isinstance(e, dict) and e.get("path")]
			except (OSError, ValueError):
				self._entries = []
		return self._entries

	def _save(self) -> None:
		from utils.config_state import _save_json

		try:
			_save_json(self.path, {"entries": self._entries or []}, sync=False)
		except OSError as exc:
			log.warning("Couldn't save the upload queue: %s", exc)

	def add(self, path: str, owner: str, meta: Optional[Dict[str, Any]] = None) -> Optional[str]:
		"""Queue a file; returns why not, or None."""
		return self.add_many([path], owner, meta)[0]

	def add_many(self, paths: List[str], owner: str, meta: Optional[Dict[str, Any]] = None) -> List[Optional[str]]:
		"""Queue one capture's files together (so its RAW can't start before
		its JPEG is queued); for each, why not, or None."""
		owner = str(owner or "").strip()
		if not owner.isdigit():
			return ["No account id to upload under"] * len(paths)
		policy = _settings().get("raw", "after_jpeg")
		results: List[Optional[str]] = []
		with self._lock:
			entries = self._load()
			for path in paths:
				kind = kind_of(path)
				if kind == "raw" and policy == "never":
					results.append("RAW uploads are off")
					continue
				results.append(None)
				if any(e["path"] == path and e.get("state") != "failed" for e in entries):
					continue
				entries.append({"path": path, "owner": owner, "kind": kind, "meta": meta or {},
					"state": "held" if kind == "raw" and policy == "on_request" else "waiting",
					"tries": 0, "refusals": 0, "next_try": 0.0, "added": time.time()})
			self._save()
		self._wake.set()
		if self._autostart:
			self.start()
		return results

	def release(self, kind: str = "raw") -> int:
		"""Send held files (the on_request RAWs); returns how many."""
		count = 0
		with self._lock:
			for e in self._load():
				if e.get("state") == "held" and (kind == "all" or e.get("kind") == kind):
					e["state"] = "waiting"
					count += 1
			if count:
				self._save()
		self._wake.set()
		return count

	def retry_failed(self) -> int:
		count = 0
		with self._lock:
			for e in self._load():
				if e.get("state") == "failed":
					e.update(state="waiting", refusals=0, next_try=0.0)
					count += 1
			if count:
				self._save()
		self._wake.set()
		return count

	def _next(self) -> Optional[Dict[str, Any]]:
		now = time.time()
		policy = _settings().get("raw", "after_jpeg")
		with self._lock:
			waiting = [e for e in self._load() if e.get("state") == "waiting"]
			jpegs_waiting = any(e["kind"] != "raw" for e in waiting)
			ready = [e for e in waiting if e.get("next_try", 0) <= now]
			ready.sort(key=lambda e: (e["kind"] == "raw", e.get("added", 0)))
			for e in ready:
				if e["kind"] == "raw" and policy == "after_jpeg" and jpegs_waiting:
					continue
				return e
		return None

	def _done(self, entry: Dict[str, Any]) -> None:
		with self._lock:
			entries = self._load()
			if entry in entries:
				entries.remove(entry)
			self._save()

	def _failed(self, entry: Dict[str, Any], exc: Exception) -> None:
		retry_max = float(_settings().get("retry_max_s", 600) or 600)
		with self._lock:
			entry["tries"] = entry.get("tries", 0) + 1
			entry["error"] = str(exc)[:120]
			if isinstance(exc, UploadRefused):
				entry["refusals"] = entry.get("refusals", 0) + 1
				if entry["refusals"] >= GIVE_UP_AFTER_REFUSALS:
					entry["state"] = "failed"
			delay = min(retry_max, 10.0 * 2 ** min(10, entry["tries"] - 1))
			entry["next_try"] = time.time() + delay
			self._save()

	def run_once(self) -> bool:
		"""Send the next file if there is one and the server's there;
		returns whether anything was sent."""
		with self._sending:
			return self._send_next()

	def _send_next(self) -> bool:
		if not self._online():
			return False
		entry = self._next()
		if entry is None:
			return False
		path = entry["path"]
		if not os.path.exists(path):
			log.warning("Queued upload is gone from the Pi: %s", path)
			self._done(entry)
			return False
		name = upload_name(path, entry["owner"])
		self.current = name
		try:
			self._sender(path, name, entry.get("meta") or {})
		except Exception as exc:
			self.last_error = str(exc)[:120]
			log.warning("Upload of %s failed (%s); will try again", name, exc)
			self._failed(entry, exc)
			return False
		finally:
			self.current = ""
		self.sent += 1
		log.info("Uploaded %s", name)
		self._done(entry)
		return True

	def _run(self) -> None:
		while True:
			try:
				if not self.run_once():
					self._wake.wait(5.0)
					self._wake.clear()
			except Exception as exc:
				log.exception("Upload queue error: %s", exc)
				time.sleep(5.0)

	def start(self) -> None:
		if self._thread is None or not self._thread.is_alive():
			self._thread = threading.Thread(target=self._run, name="uploads", daemon=True)
			self._thread.start()

	def status(self) -> Dict[str, Any]:
		with self._lock:
			entries = list(self._load())
		count = lambda state, kind=None: sum(1 for e in entries if e.get("state") == state
			and (kind is None or e.get("kind") == kind))
		return {"waiting": count("waiting"), "waiting_raw": count("waiting", "raw"), "held": count("held"),
			"failed": count("failed"), "sent": self.sent, "current": self.current, "last_error": self.last_error}

	def queue_folder(self, folder: str, owner: str) -> int:
		"""Queue every photo under folder not queued or sent before (offline
		photos when back online). A .uploaded list in each folder remembers
		what was queued."""
		added = 0
		for root, _dirs, files in os.walk(folder):
			marker = os.path.join(root, ".uploaded")
			try:
				with open(marker) as f:
					done = set(json.load(f))
			except (OSError, ValueError):
				done = set()
			new = [f for f in sorted(files) if not f.startswith(".") and f.lower().endswith(tuple(
				f".{e}" for e in (JPEG_EXTENSIONS | RAW_EXTENSIONS | {"png", "tif", "tiff", "fits"})))
				and f not in done]
			results = self.add_many([os.path.join(root, name) for name in new], owner, {"offline": True})
			for name, problem in zip(new, results):
				if problem is None:
					done.add(name)
					added += 1
			if new:
				try:
					with open(marker, "w") as f:
						json.dump(sorted(done), f)
				except OSError:
					pass
		return added


def _server_online() -> bool:
	try:
		from core.networking.websocket import server_enabled, server_state

		return server_enabled() and server_state.get("state") == "connected"
	except Exception:
		return False


_queue: Optional[UploadQueue] = None
_queue_lock = threading.Lock()


def get_upload_queue() -> UploadQueue:
	global _queue
	with _queue_lock:
		if _queue is None:
			_queue = UploadQueue()
		return _queue
