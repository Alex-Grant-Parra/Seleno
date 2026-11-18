"""Uploading photos to the server, from a queue that survives restarts.

Photos used to be uploaded straight after capture, all files in one
request, with no second try: a dropped connection lost the upload (the
files stayed on the Pi, unsent). Now:

* each file is queued (photos/upload_queue.json) and sent on its own by a
  background thread, while the client is online;
* JPEGs go first, then videos (core/camera/recorder.py). RAW files (the big ones) follow the policy in
  config upload.raw: "after_jpeg" (sent once no JPEG is waiting),
  "on_request" (held until uploadPending("raw")) or "never";
* priority files jump the queue: a test shot's JPEG, and the RAWs the
  server asks for by name (release_matching);
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
VIDEO_EXTENSIONS = {"ser", "avi"}
GIVE_UP_AFTER_REFUSALS = 3


def kind_of(path: str) -> str:
	ext = os.path.splitext(path)[1].lower().lstrip(".")
	if ext in JPEG_EXTENSIONS:
		return "jpeg"
	if ext in RAW_EXTENSIONS:
		return "raw"
	if ext in VIDEO_EXTENSIONS:
		return "video"
	return "other"


def _mime(path: str) -> str:
	ext = os.path.splitext(path)[1].lower().lstrip(".")
	return {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "tif": "image/tiff",
		"tiff": "image/tiff", "fits": "application/fits",
		"avi": "video/x-msvideo"}.get(ext, "application/octet-stream")


def upload_name(path: str, owner: str) -> str:
	base = os.path.basename(path)
	return base if base.startswith(f"{owner}_") else f"{owner}_{base}"


def _settings() -> Dict[str, Any]:
	try:
		from utils.config_state import get_section

		return get_section("upload")
	except Exception:
		return {"raw": "after_jpeg", "retry_max_s": 600}


TUS_PATH = "api/telescope/uploads"
TUS_VERSION = "1.0.0"
CHUNK_BYTES = 4 * 1024 * 1024
TUS_KINDS = ("jpeg", "raw", "video")
# Where each file's upload is on the server, to carry on after a cut-out
# instead of starting again: (server, path, size) -> its URL
_resume: Dict[tuple, str] = {}


def _file_sha256(path: str) -> str:
	import hashlib

	digest = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _captured(meta: Dict[str, Any]) -> str:
	from datetime import datetime

	text = str(meta.get("captured") or "").strip()
	try:
		datetime.fromisoformat(text.replace("Z", "+00:00"))
	except ValueError:
		return ""
	return text


def _default_sender(path: str, name: str, meta: Dict[str, Any]) -> None:
	"""Send one file to the server's resumable uploads (tus 1.0,
	/api/telescope/uploads), with the telescope's token. A cut-out costs only
	the chunk in flight: the next try asks how much arrived and carries on.
	Raises UploadRefused for a refusal (a 4xx but 409 and 429), anything else
	for a failure worth retrying."""
	import base64
	import hashlib

	import requests

	from core.networking.csrf import SESSION
	from utils import server_link

	link = server_link.current()
	if link is None:
		raise RuntimeError("No server to upload to")
	base = link["http_url"].rstrip("/")
	headers = {"Tus-Resumable": TUS_VERSION, "Authorization": f"Bearer {link['api_token']}"}
	size = os.path.getsize(path)
	key = (base, path, size)

	def refused(response: "requests.Response") -> bool:
		# 460: a chunk's checksum didn't match (damaged on the way): send it again
		return 400 <= response.status_code < 500 and response.status_code not in (409, 429, 460)

	location = _resume.get(key)
	offset = 0
	if location:
		response = SESSION.head(location, headers=headers, timeout=(10, 30))
		if response.status_code == 200:
			offset = int(response.headers.get("Upload-Offset", 0))
		else:
			location = None
	if not location:
		kind = kind_of(path)
		fields = {"filename": name, "owner": name.split("_", 1)[0], "sha256": _file_sha256(path),
			"kind": kind if kind in TUS_KINDS else "", "captured": _captured(meta),
			"meta": json.dumps(meta, default=str)}
		headers_create = dict(headers, **{"Upload-Length": str(size), "Upload-Metadata": ",".join(
			f"{k} {base64.b64encode(v.encode()).decode()}" for k, v in fields.items() if v)})
		response = SESSION.post(f"{base}/{TUS_PATH}", headers=headers_create, timeout=(10, 60))
		if refused(response):
			raise UploadRefused(f"{response.status_code}: {response.text[:120]}")
		if response.status_code != 201 or not response.headers.get("Location"):
			raise RuntimeError(f"Couldn't start the upload: {response.status_code} {response.text[:80]}")
		location = response.headers["Location"]
		if location.startswith("/"):
			location = base + location
		_resume[key] = location
		if size == 0:
			_resume.pop(key, None)
			return
	with open(path, "rb") as f:
		while offset < size:
			f.seek(offset)
			chunk = f.read(CHUNK_BYTES)
			checksum = base64.b64encode(hashlib.sha1(chunk).digest()).decode()
			response = SESSION.patch(location, data=chunk, timeout=(10, 300), headers=dict(headers, **{
				"Content-Type": "application/offset+octet-stream", "Upload-Offset": str(offset),
				"Upload-Checksum": f"sha1 {checksum}"}))
			if response.status_code in (404, 410):
				_resume.pop(key, None)
				raise RuntimeError("The server dropped the upload; starting again")
			if response.status_code == 409:
				offset = int(response.headers.get("Upload-Offset", offset))
				continue
			if refused(response):
				_resume.pop(key, None)
				raise UploadRefused(f"{response.status_code}: {response.text[:120]}")
			if response.status_code != 204:
				raise RuntimeError(f"Upload stopped: {response.status_code} {response.text[:80]}")
			offset = int(response.headers.get("Upload-Offset", offset + len(chunk)))
	_resume.pop(key, None)


class UploadRefused(RuntimeError):
	pass


class UploadQueue:
	def __init__(self, folder: Optional[str] = None, sender: Optional[Callable[[str, str, Dict[str, Any]], None]] = None,
			online: Optional[Callable[[], bool]] = None, autostart: bool = True,
			link: Optional[Callable[[], str]] = None) -> None:
		self._folder = folder
		self._sender = sender or _default_sender
		self._online = online or _server_online
		# Which server a file is for: "server" (the one on the panel; files
		# queued offline wait for it). "hotspot" entries are left from an
		# earlier version that had a server on the Pi: never sent.
		self._link = link or _link_kind
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

	def add_many(self, paths: List[str], owner: str, meta: Optional[Dict[str, Any]] = None,
			priority: bool = False, metas: Optional[List[Dict[str, Any]]] = None) -> List[Optional[str]]:
		"""Queue one capture's files together (so its RAW can't start before
		its JPEG is queued); for each, why not, or None. priority: its
		JPEG goes ahead of everything queued (a test shot); the RAW still
		follows upload.raw. metas: a meta for each path, instead of one."""
		owner = str(owner or "").strip()
		if not owner.isdigit():
			return ["No account id to upload under"] * len(paths)
		policy = _settings().get("raw", "after_jpeg")
		link = self._link()
		results: List[Optional[str]] = []
		with self._lock:
			entries = self._load()
			for n, path in enumerate(paths):
				kind = kind_of(path)
				if kind == "raw" and policy == "never":
					results.append("RAW uploads are off")
					continue
				results.append(None)
				if any(e["path"] == path and e.get("state") != "failed" and e.get("link", "server") == link
						for e in entries):
					continue
				entry = {"path": path, "owner": owner, "kind": kind, "meta": (metas[n] if metas else meta) or {},
					"state": "held" if kind == "raw" and policy == "on_request" else "waiting",
					"tries": 0, "refusals": 0, "next_try": 0.0, "added": time.time(), "link": link}
				if priority and kind != "raw":
					entry["priority"] = True
				entries.append(entry)
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

	def release_matching(self, kind: str = "raw", files: Optional[List[str]] = None,
			pairs: Optional[List[str]] = None) -> Dict[str, Any]:
		"""Send just the held (or waiting) files of some captures, first.
		files: the uploaded JPEG names ("42_IMG_0412.JPG"; the owner prefix
		and the extension don't matter); pairs: their meta "pair" ids. A
		file matches on either. Returns {"queued": n, "missing": [...]}:
		the names and pairs it has no such file for."""
		def stem(name: str) -> str:
			return os.path.splitext(os.path.basename(str(name)))[0].lower()

		wanted_files = {stem(f): f for f in files or []}
		wanted_pairs = {str(p) for p in pairs or []}
		found_files: set = set()
		found_pairs: set = set()
		queued = 0
		with self._lock:
			for e in self._load():
				if e.get("state") not in ("held", "waiting") or (kind != "all" and e.get("kind") != kind):
					continue
				names = {stem(e["path"]), stem(upload_name(e["path"], e.get("owner", "")))}
				pair = str((e.get("meta") or {}).get("pair") or "")
				by_file = names & set(wanted_files)
				by_pair = bool(pair) and pair in wanted_pairs
				if not by_file and not by_pair:
					continue
				found_files |= by_file
				if by_pair:
					found_pairs.add(pair)
				e.update(state="waiting", priority=True, next_try=0.0)
				queued += 1
			if queued:
				self._save()
		self._wake.set()
		if files and pairs and len(files) == len(pairs):
			# One capture per position, named both ways: either finds it.
			missing = [name for name, p in zip(files, pairs)
				if stem(name) not in found_files and str(p) not in found_pairs]
		else:
			missing = [name for key, name in wanted_files.items() if key not in found_files]
			missing += [str(p) for p in pairs or [] if str(p) not in found_pairs]
		return {"queued": queued, "missing": missing}

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
		link = self._link()
		with self._lock:
			waiting = [e for e in self._load() if e.get("state") == "waiting" and e.get("link", "server") == link]
			jpegs_waiting = any(e["kind"] not in ("raw", "video") for e in waiting)
			ready = [e for e in waiting if e.get("next_try", 0) <= now]
			# Priority first, then pictures, videos and RAWs (the big ones last).
			rank = {"video": 1, "raw": 2}
			ready.sort(key=lambda e: (not e.get("priority"), rank.get(e["kind"], 0), e.get("added", 0)))
			for e in ready:
				if e["kind"] == "raw" and policy == "after_jpeg" and jpegs_waiting and not e.get("priority"):
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
		photos when back online), each with what photos.jsonl says about it.
		A .uploaded list in each folder remembers what was queued."""
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
			records = _records(root)
			results = self.add_many([os.path.join(root, name) for name in new], owner,
				metas=[dict(records.get(name, {}), offline=True) for name in new])
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


def _records(folder: str) -> Dict[str, Dict[str, Any]]:
	"""File name -> its line in the folder's photos.jsonl (utils/photos.py),
	without the bookkeeping."""
	from utils.photos import LOG_NAME

	out: Dict[str, Dict[str, Any]] = {}
	try:
		with open(os.path.join(folder, LOG_NAME)) as f:
			for line in f:
				try:
					entry = json.loads(line)
				except ValueError:
					continue
				meta = {k: v for k, v in entry.items() if k not in ("files", "time")}
				for name in entry.get("files") or []:
					out[name] = meta
	except OSError:
		pass
	return out


def _link_kind() -> str:
	try:
		from utils import server_link

		return server_link.kind() or "server"
	except Exception:
		return "server"


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
