import asyncio
import json
import struct
import threading
import time
import unittest

import websockets

from harness import app, db, liveview, connect_fake, disconnect_fake, make_user, make_telescope

PORT = 18765
liveview.LIVEVIEW_WS_PORT = PORT
liveview.WS_IP = "127.0.0.1"
ORIGIN = "https://example.test"
URL = f"ws://127.0.0.1:{PORT}"

HEADER = struct.Struct(">4sBBHIQI")


def frame(kind, key, seq, payload, stream=1):
    return HEADER.pack(b"SLV2", kind, 1 if key else 0, stream, seq, int(time.time() * 1e6), len(payload)) + payload


with app.app_context():
    make_telescope("lv-scope", token="lv-token-0123456789")
    make_telescope("lv-legacy", token="lv-legacy-token-0123")
    make_telescope("lv-other", token="lv-other-token-01234")

threading.Thread(target=liveview.start_liveview_ws_server, daemon=True).start()
time.sleep(0.5)


async def recv(ws, timeout=2.0):
    return await asyncio.wait_for(ws.recv(), timeout)


async def recv_json(ws, timeout=2.0):
    return json.loads(await recv(ws, timeout))


async def nothing(ws, timeout=0.4):
    try:
        message = await asyncio.wait_for(ws.recv(), timeout)
    except asyncio.TimeoutError:
        return None
    return message


async def viewer(cid, decode=("h264", "jpeg"), origin=ORIGIN, ticket=None):
    with app.app_context():
        ticket = ticket or liveview.make_watch_ticket(cid, 1)
    ws = await websockets.connect(f"{URL}/watch/{cid}?ticket={ticket}", origin=origin)
    try:
        await ws.send(json.dumps({"type": "hello", "decode": list(decode)}))
    except websockets.exceptions.ConnectionClosed:
        pass  # refused before the hello; the test reads the close code
    return ws


class LiveView(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_proto2_relay(self):
        async def scenario():
            tel = await websockets.connect(URL)
            await tel.send(json.dumps({"token": "lv-token-0123456789", "client_id": "lv-scope", "proto": 2, "codecs": ["h264", "jpeg"]}))
            hello = await recv_json(tel)
            self.assertEqual(hello, {"type": "hello", "proto": 2, "codec": "h264", "poster_s": 5})

            stream = {"type": "stream", "stream": 1, "codec": "avc1.42E01F", "width": 1024, "height": 680, "fps": 10, "gop_s": 2}
            await tel.send(json.dumps(stream))
            # a delta before any keyframe is dropped, and a keyframe requested
            await tel.send(frame(2, False, 0, b"early"))
            self.assertEqual(await recv_json(tel), {"type": "keyframe"})
            poster = frame(1, True, 0, b"\xff\xd8poster\xff\xd9")
            key, d1, d2 = frame(2, True, 1, b"KEY"), frame(2, False, 2, b"D1"), frame(2, False, 3, b"D2")
            for message in (poster, key, d1, d2):
                await tel.send(message)
            await asyncio.sleep(0.1)
            self.assertEqual(liveview.latest_frames["lv-scope"], b"\xff\xd8poster\xff\xd9")

            # a malformed frame is dropped
            await tel.send(b"SLV2" + b"\x00" * 30)

            v1 = await viewer("lv-scope")
            self.assertEqual(await recv_json(v1), {"type": "telescope", "connected": True})
            self.assertEqual(json.loads(await recv(v1)), stream)
            self.assertEqual(await recv(v1), poster)
            self.assertEqual([await recv(v1) for _ in range(3)], [key, d1, d2])
            d3 = frame(2, False, 4, b"D3")
            await tel.send(d3)
            self.assertEqual(await recv(v1), d3)

            # a viewer that can't decode H.264 switches the telescope to JPEG, and back when it leaves
            v2 = await viewer("lv-scope", decode=("jpeg",))
            self.assertEqual(await recv_json(tel), {"type": "codec", "codec": "jpeg"})
            await v2.close()
            self.assertEqual(await recv_json(tel), {"type": "codec", "codec": "h264"})

            # keyframe requests from the browser: at most one a second
            await asyncio.sleep(1.1)
            await v1.send(json.dumps({"type": "keyframe"}))
            await v1.send(json.dumps({"type": "keyframe"}))
            self.assertEqual(await recv_json(tel), {"type": "keyframe"})
            self.assertIsNone(await nothing(tel))

            # after the counts settle, the telescope hears how many are watching
            message = await recv_json(tel, timeout=4)
            self.assertEqual(message, {"type": "viewers", "count": 1})

            await v1.close()
            await tel.close()
            await asyncio.sleep(0.2)
            self.assertNotIn("lv-scope", liveview.latest_frames)

        self.run_async(scenario())

    def test_viewer_checks(self):
        async def scenario():
            bad_origin = await viewer("lv-scope", origin="https://evil.example")
            with self.assertRaises(websockets.exceptions.ConnectionClosed) as cm:
                await recv(bad_origin)
            self.assertEqual(cm.exception.rcvd.code, 4003)

            with app.app_context():
                other = liveview.make_watch_ticket("lv-other", 1)
            wrong = await viewer("lv-scope", ticket=other)
            with self.assertRaises(websockets.exceptions.ConnectionClosed) as cm:
                await recv(wrong)
            self.assertEqual(cm.exception.rcvd.code, 4001)

            forged = await viewer("lv-scope", ticket="not-a-ticket")
            with self.assertRaises(websockets.exceptions.ConnectionClosed) as cm:
                await recv(forged)
            self.assertEqual(cm.exception.rcvd.code, 4001)

            # one telescope's token can't stream as another
            tel = await websockets.connect(URL)
            await tel.send(json.dumps({"token": "lv-other-token-01234", "client_id": "lv-scope", "proto": 2}))
            with self.assertRaises(websockets.exceptions.ConnectionClosed) as cm:
                await recv(tel)
            self.assertEqual(cm.exception.rcvd.code, 4001)

        self.run_async(scenario())

    def test_proto1_path(self):
        fake = connect_fake("lv-legacy")

        async def scenario():
            tel = await websockets.connect(URL)
            await tel.send(json.dumps({"token": "lv-legacy-token-0123", "client_id": "lv-legacy"}))
            jpeg = b"\xff\xd8legacy\xff\xd9"
            await tel.send(jpeg)
            await asyncio.sleep(0.1)
            self.assertEqual(liveview.latest_frames["lv-legacy"], jpeg)

            v = await viewer("lv-legacy", decode=("jpeg",))
            self.assertEqual(await recv_json(v), {"type": "telescope", "connected": True})
            self.assertEqual(await recv_json(v), {"type": "stream", "stream": 0, "codec": "jpeg"})
            wrapped = await recv(v)
            kind, flags, length = wrapped[4], wrapped[5], HEADER.unpack_from(wrapped)[6]
            self.assertEqual((kind, flags, wrapped[24:], length), (1, 1, jpeg, len(jpeg)))

            # the proto 1 telescope is told the count by command, and never sent anything on this socket
            await asyncio.sleep(2.6)
            self.assertIsNone(await nothing(tel, 0.2))
            await v.close()
            await tel.close()

        try:
            self.run_async(scenario())
            call = fake.last("liveViewViewers")
            self.assertIsNotNone(call)
            self.assertEqual(call[1], [1])
        finally:
            disconnect_fake("lv-legacy")

    def test_mjpeg_endpoint(self):
        liveview.MJPEG_IDLE_END_S = 1.0
        liveview.MJPEG_RESEND_S = 0.3
        liveview.store_frame("mj", b"F1")
        before = liveview.viewer_counts.count("mj")
        gen = liveview.liveview_handler("mj").response
        parts = [next(gen)]
        self.assertEqual(liveview.viewer_counts.count("mj"), before + 1)
        t0 = time.monotonic()
        parts.append(next(gen))  # resent after MJPEG_RESEND_S, not 10 times a second
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        threading.Timer(0.1, liveview.store_frame, args=("mj", b"F2")).start()
        parts.append(next(gen))
        self.assertIn(b"F2", parts[-1])
        # no new frame for MJPEG_IDLE_END_S: the response ends
        rest = list(gen)
        self.assertLessEqual(len(rest), 4)
        self.assertEqual(liveview.viewer_counts.count("mj"), before)

    def test_backpressure(self):
        liveview.VIEWER_QUEUE_BYTES = 100

        class WS:
            pass

        async def scenario():
            v = liveview.Viewer(WS(), h264=True)
            v.offer(b"k" * 40, liveview.KIND_H264, True)
            v.offer(b"d" * 40, liveview.KIND_H264, False)
            v.offer(b"x" * 40, liveview.KIND_H264, False)   # full: dropped, skip to next keyframe
            v.queue.popleft(); v.queued_bytes -= 40
            v.offer(b"y" * 10, liveview.KIND_H264, False)   # still skipping
            v.offer(b"j" * 30, liveview.KIND_JPEG, True)    # JPEG frames drop one at a time
            v.offer(b"K" * 20, liveview.KIND_H264, True)    # next keyframe resumes
            self.assertEqual([m[:1] for m in v.queue], [b"d", b"j", b"K"])
            jpeg_only = liveview.Viewer(WS(), h264=False)
            jpeg_only.offer(b"k", liveview.KIND_H264, True)
            self.assertEqual(len(jpeg_only.queue), 0)

        try:
            self.run_async(scenario())
        finally:
            liveview.VIEWER_QUEUE_BYTES = 4 * 1024 * 1024


if __name__ == "__main__":
    unittest.main(verbosity=1)
