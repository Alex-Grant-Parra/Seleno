"""ESP32Connection against a scripted fake ESP32 on a pseudo-terminal.

Covers what is hard to provoke with real hardware: stale ids, events mixed
into replies, old firmware without ids, garbage lines, lost replies and the
binary/upload framing.

Run from the repo root:  ./venv/bin/python -m unittest esp32.tests.test_interface_pty -v
"""
from __future__ import annotations

import json
import os
import pty
import select
import sys
import threading
import time
import tty
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from esp32.interfaceESP32 import (  # noqa: E402
	ESP32Connection,
	ESP32Error,
	ESP32Motor,
	ESP32SerialConfig,
)

Reply = Callable[[Dict[str, Any]], List[bytes]]


class FakeESP32:
	"""Reads newline-delimited JSON on the device side of a pty and answers
	with whatever the test's reply function returns."""

	def __init__(self, reply: Reply, echo_ids: bool = True) -> None:
		self.master, slave = pty.openpty()
		tty.setraw(slave)
		self.port = os.ttyname(slave)
		self._slave = slave
		self.reply = reply
		self.echo_ids = echo_ids
		self.requests: List[Dict[str, Any]] = []
		self.binary_expect = 0
		self.binary_received = b""
		self.on_binary_done: Optional[Callable[[], List[bytes]]] = None
		self._stop = False
		self._thread = threading.Thread(target=self._run, daemon=True)
		self._thread.start()

	def ok(self, req: Dict[str, Any], data: Optional[Dict[str, Any]] = None) -> bytes:
		resp: Dict[str, Any] = {"status": "ok"}
		if data is not None:
			resp["data"] = data
		if self.echo_ids and "id" in req:
			resp["id"] = req["id"]
		return (json.dumps(resp) + "\n").encode()

	def error(self, req: Dict[str, Any], message: str) -> bytes:
		resp: Dict[str, Any] = {"status": "error", "message": message}
		if self.echo_ids and "id" in req:
			resp["id"] = req["id"]
		return (json.dumps(resp) + "\n").encode()

	def send(self, data: bytes) -> None:
		os.write(self.master, data)

	def _run(self) -> None:
		buf = b""
		while not self._stop:
			# Poll, so close() can stop this thread and really hang up the pty.
			ready, _, _ = select.select([self.master], [], [], 0.05)
			if not ready:
				continue
			try:
				chunk = os.read(self.master, 4096)
			except OSError:
				return
			buf += chunk
			while True:
				if self.binary_expect > 0:
					take = buf[:self.binary_expect]
					buf = buf[len(take):]
					self.binary_received += take
					self.binary_expect -= len(take)
					if self.binary_expect > 0:
						break
					if self.on_binary_done is not None:
						for out in self.on_binary_done():
							self.send(out)
					continue
				if b"\n" not in buf:
					break
				line, buf = buf.split(b"\n", 1)
				if not line.strip():
					continue
				req = json.loads(line)
				self.requests.append(req)
				for out in self.reply(req):
					self.send(out)

	def close(self) -> None:
		self._stop = True
		if self._thread is not threading.current_thread():
			self._thread.join(timeout=1.0)
		for fd in (self.master, self._slave):
			try:
				os.close(fd)
			except OSError:
				pass


def default_reply(dev: FakeESP32) -> Reply:
	def reply(req: Dict[str, Any]) -> List[bytes]:
		cmd = req.get("cmd")
		if cmd == "list_motors":
			return [dev.ok(req, {"motors": ["motor1", "motor2"]})]
		if cmd == "ping":
			return [dev.ok(req, {"fw": "test", "board": "fake"})]
		return [dev.ok(req, {})]
	return reply


class InterfaceTest(unittest.TestCase):
	def connect(self, reply_factory=default_reply, echo_ids: bool = True) -> tuple[FakeESP32, ESP32Connection]:
		dev = FakeESP32(lambda req: [], echo_ids=echo_ids)
		dev.reply = reply_factory(dev)
		conn = ESP32Connection(ESP32SerialConfig(port=dev.port, timeout=0.5), scan_all_ports=False)
		self.addCleanup(dev.close)
		self.addCleanup(conn.close)
		return dev, conn

	def test_connects_and_reads_firmware_info(self) -> None:
		dev, conn = self.connect()
		self.assertTrue(conn.supports_ids)
		self.assertEqual(conn.firmware.get("fw"), "test")
		self.assertEqual(conn.list_motors(), {"motors": ["motor1", "motor2"]})

	def test_old_firmware_without_ids(self) -> None:
		dev, conn = self.connect(echo_ids=False)
		self.assertFalse(conn.supports_ids)
		self.assertEqual(conn.list_motors()["motors"], ["motor1", "motor2"])

	def test_stale_reply_is_ignored(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("cmd") == "status":
					stale = dict(req, id=req["id"] - 100)
					return [dev.ok(stale, {"which": "stale"}), dev.ok(req, {"which": "right"})]
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		self.assertEqual(conn.send({"cmd": "status", "motor": "motor1"})["which"], "right")
		self.assertEqual(conn.stats["stale_responses"], 1)

	def test_events_are_dispatched_and_not_taken_as_replies(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("cmd") == "estop":
					event = b'{"event":"estop","active":true,"source":"button"}\n'
					return [event, dev.ok(req, {"active": True})]
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		seen: List[Dict[str, Any]] = []
		got = threading.Event()
		conn.add_event_listener(lambda ev: (seen.append(ev), got.set()), "estop")
		self.assertEqual(conn.estop(), {"active": True})
		self.assertTrue(got.wait(1.0))
		self.assertEqual(seen[0]["source"], "button")

	def test_unsolicited_event_and_wait_for_event(self) -> None:
		dev, conn = self.connect()
		threading.Timer(0.1, lambda: dev.send(b'{"event":"button","button":"top","action":"press"}\n')).start()
		ev = conn.wait_for_event("button", timeout=2.0, predicate=lambda e: e["button"] == "top")
		self.assertIsNotNone(ev)

	def test_error_reply_raises_esp32_error(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("cmd") == "engage":
					return [dev.error(req, "Driver not responding on TMC UART")]
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		with self.assertRaises(ESP32Error) as ctx:
			conn.send({"cmd": "engage", "motor": "motor1"})
		self.assertIn("ESP32 error", str(ctx.exception))
		self.assertEqual(ctx.exception.error_message, "Driver not responding on TMC UART")

	def test_motion_command_is_never_retried(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("cmd") in ("move_steps", "status"):
					return []  # reply lost
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		with self.assertRaises(TimeoutError):
			conn.send({"cmd": "move_steps", "motor": "motor1", "steps": 100}, timeout=0.2)
		self.assertEqual(sum(1 for r in dev.requests if r.get("cmd") == "move_steps"), 1)
		with self.assertRaises(TimeoutError):
			conn.send({"cmd": "status", "motor": "motor1"}, timeout=0.2)
		self.assertEqual(sum(1 for r in dev.requests if r.get("cmd") == "status"), 2)

	def test_garbage_lines_are_skipped(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("cmd") == "info":
					return [b"\xff\xfe ets Jun  8 2016 rst:0x1\n", b'{"data":{"broken\n', dev.ok(req, {"ok": 1})]
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		self.assertEqual(conn.info(), {"ok": 1})
		self.assertEqual(conn.stats["noise_lines"], 1)
		self.assertEqual(conn.stats["corrupt_lines"], 1)

	def test_binary_blit_reply_comes_after_payload(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("action") == "blit":
					dev.binary_expect = req["length"]
					dev.on_binary_done = lambda: [dev.ok(req)]
					return []
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		payload = bytes(range(256)) * 20
		conn.send_binary({"cmd": "display", "action": "blit", "length": len(payload)}, payload)
		self.assertEqual(dev.binary_received, payload)

	def test_upload_gets_two_replies_with_one_id(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("action") == "store_begin":
					dev.binary_expect = req["length"]
					dev.on_binary_done = lambda: [dev.ok(req, {"stored": True})]
					return [dev.ok(req)]
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		data = os.urandom(3000)
		self.assertEqual(conn.upload_display_file("x.rgb", data), {"stored": True})
		self.assertEqual(dev.binary_received, data)

	def test_motor_steps_per_rev_comes_from_firmware(self) -> None:
		def factory(dev: FakeESP32) -> Reply:
			base = default_reply(dev)

			def reply(req: Dict[str, Any]) -> List[bytes]:
				if req.get("cmd") == "status":
					return [dev.ok(req, {"motor": req["motor"], "steps_per_rev": 3200, "microsteps": 16, "speed_us": 500})]
				return base(req)
			return reply

		dev, conn = self.connect(factory)
		m = ESP32Motor(conn, "motor1")
		self.assertEqual(m.steps_per_rev, 3200)
		self.assertEqual(ESP32Motor(conn, "motor1").steps_per_rev, 3200)  # cached
		self.assertEqual(sum(1 for r in dev.requests if r.get("cmd") == "status"), 1)

	def test_link_loss_raises_transport_error(self) -> None:
		dev, conn = self.connect()
		lost = threading.Event()
		conn.add_event_listener(lambda ev: lost.set(), "link_lost")
		dev.close()
		self.assertTrue(lost.wait(2.0))
		with self.assertRaises(ConnectionError):
			conn.send({"cmd": "ping"}, timeout=0.2)


if __name__ == "__main__":
	unittest.main()
