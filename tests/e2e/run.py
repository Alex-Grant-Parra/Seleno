"""Run the end-to-end scenarios in headless Chrome and save screenshots.

    E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/run.py [--dark] [--width=390] [scenario ...]

Each scenario is a script in this folder (scenario_<name>.js) that drives a page and
returns {checks: {...}, failures: [...]}. A scenario calls `await shot('name')` to
save a screenshot to E2E_OUT (default tests/e2e/out); --dark runs in the dark theme.
Exits non-zero on any failure or page error. Takes about a minute."""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

CHROME = os.environ.get("E2E_CHROME") or shutil.which("google-chrome") or shutil.which("chromium")
OUT = os.environ.get("E2E_OUT") or os.path.join(HERE, "out")
DEBUG_PORT = 19222
SUFFIX = ""
SCENARIOS = {
    "interface": "/e2e-login?next=/interface/",
    "album": "/e2e-login?next=/album",
    "narrow": "/e2e-login?next=/interface/",
    "narrow_album": "/e2e-login?next=/album",
}
WIDTH = 1500

PRELUDE = r"""
window.__e2eErrors = window.__e2eErrors || [];
window.addEventListener('error', e => window.__e2eErrors.push(`${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener('unhandledrejection', e => window.__e2eErrors.push(`unhandled: ${e.reason && (e.reason.stack || e.reason.message) || e.reason}`));
window.confirm = () => true;
window.alert = (m) => window.__e2eErrors.push('alert: ' + m);
window.prompt = (q, d) => (d || '') + ' (renamed)';
window.sleep = ms => new Promise(r => setTimeout(r, ms));
window.until = async (fn, ms = 8000) => {
    const t = Date.now();
    while (Date.now() - t < ms) { try { if (await fn()) return true; } catch (e) {} await sleep(100); }
    return false;
};
window.shot = (name) => new Promise(resolve => { window.__shotDone = resolve; console.log('E2E_SHOT ' + name); });
window.$q = (s) => document.querySelector(s);
window.$$q = (s) => [...document.querySelectorAll(s)];
window.txt = (id) => (document.getElementById(id) || {}).textContent || '';
window.visible = (el) => !!el && el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
"""


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.pending = {}
        self.console = []
        self.errors = []
        self.shot_requests = asyncio.Queue()
        self.loaded = asyncio.Event()

    async def reader(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg and msg["id"] in self.pending:
                self.pending.pop(msg["id"]).set_result(msg)
                continue
            method = msg.get("method")
            params = msg.get("params", {})
            if method == "Runtime.consoleAPICalled":
                text = " ".join(str(a.get("value", a.get("description", ""))) for a in params.get("args", []))
                if text.startswith("E2E_SHOT "):
                    self.shot_requests.put_nowait(text[9:])
                else:
                    self.console.append(f"{params.get('type')}: {text}")
                    if params.get("type") in ("error", "assert"):
                        self.errors.append(text)
            elif method == "Runtime.exceptionThrown":
                details = params.get("exceptionDetails", {})
                self.errors.append(details.get("exception", {}).get("description") or details.get("text"))
            elif method == "Log.entryAdded":
                entry = params.get("entry", {})
                if entry.get("level") == "error" and "favicon" not in entry.get("url", ""):
                    self.errors.append(f"{entry.get('source')}: {entry.get('text')} {entry.get('url', '')}")
            elif method == "Page.loadEventFired":
                self.loaded.set()

    async def send(self, method, params=None, timeout=60):
        self.next_id += 1
        future = asyncio.get_event_loop().create_future()
        self.pending[self.next_id] = future
        await self.ws.send(json.dumps({"id": self.next_id, "method": method, "params": params or {}}))
        msg = await asyncio.wait_for(future, timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error']}")
        return msg.get("result", {})

    async def evaluate(self, expression, timeout=60):
        result = await self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                                     "returnByValue": True}, timeout=timeout)
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"].get("exception", {}).get("description") or str(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    async def screenshot(self, path):
        metrics = await self.send("Page.getLayoutMetrics")
        size = metrics.get("cssContentSize") or metrics.get("contentSize")
        width = WIDTH
        height = min(int(size["height"]), 4000)
        shot = await self.send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": True,
                                                          "clip": {"x": 0, "y": 0, "width": width, "height": height, "scale": 1}})
        with open(path, "wb") as f:
            f.write(base64.b64decode(shot["data"]))


async def run_scenario(page, name, base_url):
    page.loaded.clear()
    await page.send("Page.navigate", {"url": base_url + SCENARIOS[name]})
    await asyncio.wait_for(page.loaded.wait(), 30)
    await page.evaluate(PRELUDE)
    script = open(os.path.join(HERE, f"scenario_{name}.js")).read()
    task = asyncio.ensure_future(page.evaluate(script, timeout=240))
    shots = []
    while not task.done():
        try:
            shot_name = await asyncio.wait_for(page.shot_requests.get(), 0.25)
        except asyncio.TimeoutError:
            continue
        path = os.path.join(OUT, f"{name}-{shot_name}{SUFFIX}.png")
        await asyncio.sleep(0.3)
        await page.screenshot(path)
        shots.append(path)
        await page.evaluate("window.__shotDone && window.__shotDone()")
    result = task.result() or {}
    page_errors = await page.evaluate("window.__e2eErrors || []")
    result["page_errors"] = page_errors
    result["shots"] = shots
    return result


async def main(names, dark=False):
    import websockets
    import server

    os.makedirs(OUT, exist_ok=True)
    web, stop = server.start()
    profile = tempfile.mkdtemp(prefix="e2e-chrome-")
    chrome = subprocess.Popen([CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--no-first-run",
                               "--no-default-browser-check", f"--user-data-dir={profile}",
                               f"--remote-debugging-port={DEBUG_PORT}", "--window-size=1500,1000", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failed = False
    try:
        targets = None
        for _ in range(100):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json"))
                break
            except Exception:
                time.sleep(0.1)
        ws_url = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        async with websockets.connect(ws_url, max_size=200_000_000) as ws:
            page = Page(ws)
            reader = asyncio.ensure_future(page.reader())
            for domain in ("Page", "Runtime", "Log"):
                await page.send(f"{domain}.enable")
            await page.send("Emulation.setDeviceMetricsOverride", {"width": WIDTH, "height": 1000 if WIDTH > 800 else 844,
                                                                   "deviceScaleFactor": 1, "mobile": WIDTH <= 800})
            if dark:
                await page.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "dark"}]})
            base_url = f"http://127.0.0.1:{server.WEB_PORT}"
            for name in names:
                started = time.time()
                page.errors.clear()
                try:
                    result = await run_scenario(page, name, base_url)
                except Exception as error:
                    result = {"failures": [f"scenario crashed: {error}"]}
                result["console_errors"] = list(page.errors)
                result["seconds"] = round(time.time() - started, 1)
                problems = result.get("failures", []) + result.get("page_errors", []) + result.get("console_errors", [])
                failed = failed or bool(problems)
                print(f"== {name}: {'FAILED' if problems else 'ok'} ({result['seconds']} s)")
                print(json.dumps(result, indent=1, default=str))
            reader.cancel()
    finally:
        chrome.kill()
        stop.set()
        shutil.rmtree(profile, ignore_errors=True)
    print("commands the fake telescope got:", sorted({c[0] for c in server.fake.calls}))
    print("plans started:", [p["name"] for p in server.plans_started.values()])
    return 1 if failed else 0


if __name__ == "__main__":
    args = sys.argv[1:]
    dark = "--dark" in args
    if dark:
        SUFFIX = "-dark"
    for arg in args:
        if arg.startswith("--width="):
            WIDTH = int(arg.split("=", 1)[1])
            SUFFIX += f"-{WIDTH}"
    names = [a for a in args if not a.startswith("--")] or ["interface", "album"]
    sys.exit(asyncio.run(main(names, dark)))
