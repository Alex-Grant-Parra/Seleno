"""Logging for the client.

Everything goes to stdout (the journal, when the client runs as a
service) and to a rotating file, logs/seleno.log (5 MB, three old files
kept). Modules log through their own logger:

	from utils.log import get_logger
	log = get_logger("camera")
	log.info("Camera found: %s", model)

The level is INFO unless SELENO_LOG_LEVEL says otherwise (DEBUG, INFO,
WARNING, ERROR).
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys

LOG_DIR = "logs"
LOG_FILE = "seleno.log"
_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup(level: str | int | None = None, log_dir: str = LOG_DIR) -> None:
	"""Configure the root logger once (Client.py calls this at startup)."""
	global _configured
	if _configured:
		return
	_configured = True
	level = level or os.environ.get("SELENO_LOG_LEVEL", "INFO")
	if isinstance(level, str):
		level = getattr(logging, level.upper(), logging.INFO)
	root = logging.getLogger()
	root.setLevel(level)
	formatter = logging.Formatter(_FORMAT, _DATE_FORMAT)
	console = logging.StreamHandler(sys.stdout)
	console.setFormatter(formatter)
	root.addHandler(console)
	try:
		os.makedirs(log_dir, exist_ok=True)
		file_handler = logging.handlers.RotatingFileHandler(
			os.path.join(log_dir, LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3)
		file_handler.setFormatter(formatter)
		root.addHandler(file_handler)
	except OSError as exc:
		root.warning("No log file (%s); logging to stdout only", exc)
	# Libraries' chatter stays at warnings.
	for noisy in ("websockets", "urllib3", "PIL", "asyncio"):
		logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
	return logging.getLogger(f"seleno.{name}")
