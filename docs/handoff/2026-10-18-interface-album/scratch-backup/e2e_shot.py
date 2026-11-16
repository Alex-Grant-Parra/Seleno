# Screenshot of the interface page with live view playing, via Chrome's DevTools protocol
import asyncio, base64, json, os, subprocess, sys, threading, time, urllib.request
os.environ["E2E_SCRIPT"] = "e2e_shot.js"
sys.argv = ["e2e.py"]
import e2e
from werkzeug.serving import make_server

def main():
    e2e.liveview.LIVEVIEW_WS_PORT = e2e.LV_PORT
    e2e.liveview.WS_IP = "127.0.0.1"
    threading.Thread(target=e2e.liveview.start_liveview_ws_server, daemon=True).start()
    server = make_server("127.0.0.1", e2e.WEB_PORT, e2e.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)
    stop = threading.Event()
    threading.Thread(target=lambda: asyncio.run(e2e.fake_liveview_telescope(stop)), daemon=True).start()
    chrome = subprocess.Popen([os.path.join(e2e.HERE, "chrome.sh"), "--window-size=1600,1100", "--remote-debugging-port=19222",
                               f"http://127.0.0.1:{e2e.WEB_PORT}/e2e-login"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not e2e.result_ready.wait(40):
            print("timeout"); return
        tabs = json.load(urllib.request.urlopen("http://127.0.0.1:19222/json"))
        ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
        async def shot():
            import websockets
            async with websockets.connect(ws_url, max_size=50_000_000) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == 1:
                        return base64.b64decode(msg["result"]["data"])
        png = asyncio.run(shot())
        open(os.path.join(e2e.HERE, "interface.png"), "wb").write(png)
        print("saved", len(png))
    finally:
        chrome.kill(); stop.set()

main()
