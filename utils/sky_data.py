"""Sky data for the front panel's sky view, from the Seleno server.

The same data the web star map draws: stars to magnitude 5 with the planets
(/api/stars?maxMag=5&include_planets=true), proper names (/api/star_names)
and constellation figures (/api/constellations). The server is whatever
`client_config.base_url` in config/client_profile.json says.

Everything is cached in config/sky_cache.json so the panel still shows the
sky with no network. Stars, names and figures are refetched weekly; planets
(they move) every 10 minutes from /api/planets. Offline mode never fetches,
and planet positions older than PLANETS_SHOWN_FOR_S aren't drawn (the Moon
moves half a degree an hour).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests

from utils.config_state import get_client_config, is_offline_mode

CACHE_FILE = os.path.join("config", "sky_cache.json")
STAR_MAG_LIMIT = 5.0
CATALOGUE_MAX_AGE_S = 7 * 24 * 3600
PLANETS_MAX_AGE_S = 10 * 60
PLANETS_SHOWN_FOR_S = 6 * 3600
REQUEST_TIMEOUT_S = 10.0

# (ra_deg, dec_deg, mag, catalogue id), brightest first
Star = Tuple[float, float, float, str]


class SkyData:
	def __init__(self, cache_file: str = CACHE_FILE) -> None:
		self._cache_file = cache_file
		self._lock = threading.Lock()
		self.base_url = ""
		self.stars: List[Star] = []
		self.names: Dict[str, str] = {}
		self.constellations: List[Dict[str, Any]] = []
		self.planets: List[Dict[str, Any]] = []
		self.catalogue_at = 0.0
		self.planets_at = 0.0
		self.last_error = ""

	# --- public -----------------------------------------------------------------

	def ready(self) -> bool:
		return bool(self.stars)

	def refresh(self, force: bool = False) -> bool:
		"""Load the cache, then fetch whatever is missing or stale. Never
		raises; returns whether any star data is available."""
		with self._lock:
			base = self._base_url()
			if not self.stars:
				self._load_cache()
			if base and base != self.base_url:
				# A different server: don't mix its data with the cached one.
				force = True
			now = time.time()
			if base and (force or not self.stars or now - self.catalogue_at > CATALOGUE_MAX_AGE_S):
				self._fetch_catalogue(base)
			elif base and now - self.planets_at > PLANETS_MAX_AGE_S:
				self._fetch_planets(base)
			return bool(self.stars)

	def planets_fresh(self) -> bool:
		return time.time() - self.planets_at < PLANETS_SHOWN_FOR_S

	def snapshot(self) -> Tuple[List[Star], Dict[str, str], List[Dict[str, Any]], List[Dict[str, Any]]]:
		with self._lock:
			planets = self.planets if self.planets_fresh() else []
			return self.stars, self.names, self.constellations, planets

	# --- internals --------------------------------------------------------------

	@staticmethod
	def _base_url() -> str:
		# "" (fetch nothing) in offline mode or before a server is set.
		try:
			if is_offline_mode():
				return ""
			base = str(get_client_config().get("base_url", "") or "").strip()
		except Exception:
			return ""
		return base if base.endswith("/") or not base else base + "/"

	def _get(self, base: str, path: str) -> Any:
		response = requests.get(urljoin(base, path), timeout=REQUEST_TIMEOUT_S)
		response.raise_for_status()
		return response.json()

	def _fetch_catalogue(self, base: str) -> None:
		try:
			objects = self._get(base, f"api/stars?maxMag={STAR_MAG_LIMIT:g}&include_planets=true")
			names = self._get(base, "api/star_names")
			figures = self._get(base, "api/constellations")
		except Exception as exc:
			self.last_error = f"sky data: {exc}"
			print(f"[sky] Could not fetch sky data from {base}: {exc}")
			return
		stars: List[Star] = []
		planets: List[Dict[str, Any]] = []
		for obj in objects if isinstance(objects, list) else []:
			if not isinstance(obj, dict) or "ra" not in obj or "dec" not in obj:
				continue
			if obj.get("type") == "planet":
				planets.append(obj)
			elif obj.get("mag") is not None:
				stars.append((float(obj["ra"]), float(obj["dec"]), float(obj["mag"]), str(obj.get("name", ""))))
		stars.sort(key=lambda s: s[2])
		self.stars = stars
		self.planets = planets
		self.names = {
			str(entry["id"]): str(entry["name"])
			for entry in (names.get("stars", []) if isinstance(names, dict) else [])
			if entry.get("id") and entry.get("name")
		}
		self.constellations = list(figures.get("constellations", [])) if isinstance(figures, dict) else []
		self.base_url = base
		self.catalogue_at = self.planets_at = time.time()
		self.last_error = ""
		print(f"[sky] {len(stars)} stars, {len(self.names)} names, {len(self.constellations)} figures, "
			f"{len(planets)} planets from {base}")
		self._save_cache()

	def _fetch_planets(self, base: str) -> None:
		try:
			planets = self._get(base, "api/planets")
		except Exception as exc:
			self.last_error = f"planets: {exc}"
			self.planets_at = time.time() - PLANETS_MAX_AGE_S + 60  # retry in a minute
			return
		if isinstance(planets, list):
			self.planets = [p for p in planets if isinstance(p, dict) and "ra" in p and "dec" in p]
			self.planets_at = time.time()
			self._save_cache()

	def _load_cache(self) -> None:
		try:
			with open(self._cache_file, "r") as f:
				data = json.load(f)
		except Exception:
			return
		self.base_url = str(data.get("base_url", ""))
		self.stars = [tuple(s) for s in data.get("stars", [])]  # type: ignore[misc]
		self.names = dict(data.get("names", {}))
		self.constellations = list(data.get("constellations", []))
		self.planets = list(data.get("planets", []))
		self.catalogue_at = float(data.get("catalogue_at", 0))
		self.planets_at = float(data.get("planets_at", 0))

	def _save_cache(self) -> None:
		payload = {
			"base_url": self.base_url,
			"catalogue_at": self.catalogue_at,
			"planets_at": self.planets_at,
			"stars": self.stars,
			"names": self.names,
			"constellations": self.constellations,
			"planets": self.planets,
		}
		try:
			os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
			tmp = f"{self._cache_file}.tmp"
			with open(tmp, "w") as f:
				json.dump(payload, f, separators=(",", ":"))
			os.replace(tmp, self._cache_file)
		except Exception as exc:
			print(f"[sky] Could not save the sky cache: {exc}")


_instance: Optional[SkyData] = None


def get_sky_data() -> SkyData:
	global _instance
	if _instance is None:
		_instance = SkyData()
	return _instance
