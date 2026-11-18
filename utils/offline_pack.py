"""What the telescope knows about the sky for the phone app, offline.

seleno.org gives it, with its offline credentials (utils/phone_credentials.py),
an "offline pack": the featured targets basic mode suggests, and the named
objects (Messier, bright NGC/IC, named stars) with their J2000 positions. The
sky view's cache (utils/sky_data.py: stars to magnitude 5 and their names)
adds to it. utils/phone_api.py looks names up and searches here.

The pack is config/phone_link/pack.json:
    {"featured_targets": [{designation, name, track_name, kind, preset,
      size_arcmin, blurb, wiki, ra, dec}...],
     "objects": [[designation, label, ra, dec, mag, [aliases]], ...]}
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Dict, List, Optional

PACK_FILE = os.path.join("config", "phone_link", "pack.json")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": None, "pack": {}, "index": None}


def _norm(text: str) -> str:
	return re.sub(r"[\s_\-]+", "", str(text or "").lower())


def save(pack: Dict[str, Any]) -> None:
	from utils.config_state import _save_json

	_save_json(PACK_FILE, pack, sync=False)


def _load() -> Dict[str, Any]:
	try:
		mtime = os.path.getmtime(PACK_FILE)
	except OSError:
		mtime = None
	with _lock:
		if _cache["mtime"] != mtime or _cache["index"] is None:
			pack: Dict[str, Any] = {}
			if mtime is not None:
				try:
					with open(PACK_FILE) as f:
						pack = json.load(f)
				except (OSError, ValueError):
					pack = {}
			_cache.update(mtime=mtime, pack=pack if isinstance(pack, dict) else {}, index=None)
			_cache["index"] = _build_index(_cache["pack"])
		return _cache


def _build_index(pack: Dict[str, Any]) -> List[Dict[str, Any]]:
	"""[{name, label, ra, dec, mag, keys: [normalised names]}], one per object."""
	entries: Dict[str, Dict[str, Any]] = {}
	for row in pack.get("objects") or []:
		try:
			designation, label, ra, dec = str(row[0]), str(row[1] or row[0]), float(row[2]), float(row[3])
		except (IndexError, TypeError, ValueError):
			continue
		mag = row[4] if len(row) > 4 else None
		aliases = row[5] if len(row) > 5 and isinstance(row[5], list) else []
		entries[designation] = {"name": designation, "label": label, "ra": ra, "dec": dec, "mag": mag,
			"keys": {_norm(designation), _norm(label)} | {_norm(a) for a in aliases}}
	for target in pack.get("featured_targets") or []:
		if target.get("ra") is None:
			continue
		designation = str(target.get("designation") or "")
		entry = entries.setdefault(designation, {"name": designation, "label": target.get("name") or designation,
			"ra": float(target["ra"]), "dec": float(target["dec"]), "mag": None, "keys": set()})
		entry["keys"] |= {_norm(designation), _norm(target.get("name") or ""), _norm(target.get("track_name") or "")}
	try:
		from utils.sky_data import get_sky_data

		data = get_sky_data()
		names = dict(getattr(data, "names", {}) or {})
		for star in getattr(data, "stars", []) or []:
			ra, dec, mag, designation = star[0], star[1], star[2], str(star[3])
			label = names.get(designation)
			if not label and designation in entries:
				continue
			entry = entries.setdefault(designation, {"name": designation, "label": label or designation,
				"ra": float(ra), "dec": float(dec), "mag": mag, "keys": set()})
			entry["keys"] |= {_norm(designation)} | ({_norm(label)} if label else set())
	except Exception:
		pass
	for entry in entries.values():
		entry["keys"].discard("")
	return list(entries.values())


def featured_targets() -> List[Dict[str, Any]]:
	return list(_load()["pack"].get("featured_targets") or [])


def lookup(name: str) -> Optional[Dict[str, Any]]:
	key = _norm(name)
	for entry in _load()["index"]:
		if key in entry["keys"]:
			return entry
	return None


def search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
	"""The best matches: exact names first, then names starting with it, then
	names containing it; brighter first within each."""
	key = _norm(query)
	if not key:
		return []
	ranked = []
	for entry in _load()["index"]:
		match = None
		matched = entry["label"]
		for k in entry["keys"]:
			if k == key:
				match = "exact"
				break
			if k.startswith(key) and match != "prefix":
				match = "prefix"
			elif key in k and match is None and len(key) >= 3:
				match = "fuzzy"
		if match is None:
			continue
		rank = {"exact": 0, "prefix": 1, "fuzzy": 2}[match]
		mag = entry.get("mag")
		ranked.append((rank, mag if isinstance(mag, (int, float)) else 99, {**{k: v for k, v in entry.items()
			if k != "keys"}, "match": match, "matched": matched, "score": 3 - rank}))
	ranked.sort(key=lambda r: (r[0], r[1]))
	return [r[2] for r in ranked[:limit]]
