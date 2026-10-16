"""Shared bits for the tests: scratch config files, waiting, test images.

Run from the repo root:  ./venv/bin/python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
from typing import Any, Callable, Dict, Optional

import utils.config_state as config_state


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0, step: float = 0.02) -> bool:
	deadline = time.monotonic() + timeout
	while time.monotonic() < deadline:
		if predicate():
			return True
		time.sleep(step)
	return False


def jpeg(width: int = 320, height: int = 200, seed: int = 1, quality: int = 85, progressive: bool = False) -> bytes:
	from PIL import Image

	img = Image.effect_noise((width, height), 30 + seed).convert("RGB")
	out = io.BytesIO()
	img.save(out, "JPEG", quality=quality, progressive=progressive)
	return out.getvalue()


class ScratchConfig(unittest.TestCase):
	"""Points the three config files at a temporary folder for each test
	(never the real config/)."""

	profile: Dict[str, Any] = {"client_config": {"client_id": "test", "base_url": "", "api_token": "", "offline": True},
		"location": {"latitude": 51.4623, "longitude": -2.5502, "source": "manual"}}
	static: Optional[Dict[str, Any]] = None

	def setUp(self) -> None:
		self.tmp = tempfile.TemporaryDirectory()
		self.dir = self.tmp.name
		self.saved = (config_state.CLIENT_PROFILE_FILE, config_state.STATIC_STATE_FILE,
			config_state.RUNTIME_STATE_FILE, config_state._STATE_READY)
		static = config_state._merge_defaults(config_state._default_static_state(), self.static or {})
		files = {
			"CLIENT_PROFILE_FILE": ("client_profile.json",
				config_state._merge_defaults(config_state._default_client_profile(), self.profile)),
			"STATIC_STATE_FILE": ("client_config.json", static),
			"RUNTIME_STATE_FILE": ("runtime_state.json", config_state._default_runtime_state()),
		}
		for attr, (name, content) in files.items():
			path = os.path.join(self.dir, name)
			config_state._save_json(path, content, sync=False)
			setattr(config_state, attr, path)
		config_state._STATE_READY = True
		config_state.runtime_store().reload()
		from utils import telescope_state

		telescope_state._STATE_CACHE = None
		self._cwd = os.getcwd()

	def tearDown(self) -> None:
		(config_state.CLIENT_PROFILE_FILE, config_state.STATIC_STATE_FILE,
			config_state.RUNTIME_STATE_FILE, config_state._STATE_READY) = self.saved
		config_state.runtime_store()._path = None  # reloaded lazily, if anything needs it
		os.chdir(self._cwd)
		self.tmp.cleanup()

	def in_scratch_dir(self) -> None:
		"""Run from the scratch folder (photos/, logs/ land there)."""
		os.chdir(self.dir)
