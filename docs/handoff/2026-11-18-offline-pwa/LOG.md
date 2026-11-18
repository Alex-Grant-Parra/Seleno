# Offline PWA (option A): work log

Kept as the work goes, so a new session can carry on if this one stops. Newest
entries at the bottom of "Log". Paths: server repo `/home/alex/Server` (branch
`Server/canary`), client repo `~/SelenoClient` (the Pi, SSHFS; run commands on
the Pi with `ssh -p 2222 alex@82.36.204.156`). Nothing is committed unless the
log says so.

## Resume here (updated 2026-11-18 18:55)

**Safety first:** the Pi has the hotspot helper installed
(`/usr/local/sbin/seleno-hotspot`, passwordless sudo). Anything that turns on
offline mode for real switches its Wi-Fi to the hotspot and the Pi drops off
the network (port 2222 forwards to its Wi-Fi address, 192.168.0.55). Tests are
guarded (`SELENO_TESTS=1` hides the helper). Never put the live client in
offline mode from here, and never call the helper. If it happens: Alex runs
`sudo /usr/local/sbin/seleno-hotspot down </dev/null` on the Pi (or reboots).

**State: option A is built and tested; nothing committed.** Steps 1-7 done
(see Plan). The server was restarted at 18:50 with all of it (healthy; new
endpoints answer). The Pi fetched its real credentials from seleno.org at
18:55: name `seleno-01-22.tel.seleno.org`, secret, pack; certificate pending.

**What's left (needs Alex):**
1. `CLOUDFLARE_API_TOKEN` (Zone:DNS:Edit on seleno.org) in instance/.env, then
   restart the server: the job publishes the names and gets certificates
   (try `ACME_DIRECTORY=https://acme-staging-v02.api.letsencrypt.org/directory`
   first if wanted). Untested against the real Let's Encrypt/Cloudflare.
2. Rerun `sudo deploy/hotspot-setup.sh` on the Pi (dnsmasq-base + DNS unit).
3. Restart the live client (it runs pre-session code); it then fetches the
   certificate by itself while online (Setup > App link).
4. A real phone test: online, open seleno.org (owner account), install the
   app, open the interface once; then offline mode on the telescope, join its
   Wi-Fi, open the app. Untested so far: real phone browsers (iOS Safari,
   Android Chrome's local-network prompt), real certificates, hostapd/dnsmasq.
5. Delete `~/SelenoServer` (388 MB, session 1's server copy) on the Pi.
6. Commit when happy (server: Server/canary; client repo too).

## What Alex asked for

1. (Session 1, done) In offline mode the telescope broadcasts a Wi-Fi hotspot
   with a password; name (= the telescope's offline identifier) and password
   are set on the front panel and shown on it; phones use the interface, star
   map and album over it; telescope settings changeable from the phone.
2. (Session 2, this work, "option A") Instead of opening an IP: the phone
   uses the seleno.org PWA, installed while online. On the hotspot:
   - the PWA itself works offline (cached by its service worker);
   - the star map is the PWA's own page, running in the phone; it only sends
     the coordinates to track over the link to the telescope;
   - the interface's telescope selector shows the connected telescope as the
     only option, and the interface works as normal;
   - album can differ from online.
   Capture plans: Alex didn't answer, so online-only for now.

## Design (agreed)

- PWA pages (interface, star map, album) cached by the service worker at
  seleno.org; the star map already works offline (`static/js/star_map/offline.js`,
  `static/js/star_map_sw.js`).
- Each telescope gets an HTTPS name `<slug>.tel.<APP_DOMAIN>` pointing at its
  hotspot address 10.42.0.1, with a Let's Encrypt certificate the server gets by
  DNS-01 and the Pi downloads while online. The Pi also answers DNS for that
  name on the hotspot. (Mixed content: an https page can't call http://10.42.0.1.)
- Online, seleno.org gives the PWA a signed "offline pass" per telescope the
  user may use; the Pi checks it with a key it got while online.
- The Pi runs a small local API (no Flask server copy): a secure websocket into
  the client's function map, live view frames, a photo list/thumbnails/files,
  getSettings/setSettings.
- The interface JS gets a link layer: seleno.org online, the telescope directly
  offline. Server-side helpers (sky, targets, search) move to the browser.
- Album offline: photos straight from the Pi.

## Plan / status

Status markers: [ ] todo, [~] in progress, [x] done.

1. [x] **Pi local API** (client, `core/networking/phone_link.py`): HTTPS on
   port 8443 (cert from step 2, self-signed for tests) answering the same paths
   the pages call on seleno.org (`/interface/command`, `/interface/take_photo`,
   `/track_star`, `/get_tracking_status`, `/api/telescope_position`,
   `/interface/telescope_settings/data`, ...), mapped straight to
   `utils/handlers.function_map` (a Python mirror of the server's
   `TELESCOPE_ACTIONS` in controllers/interface.py). CORS for the seleno.org
   origin, `Access-Control-Allow-Private-Network`. Every request carries an
   offline pass (`X-Seleno-Pass`). Live view: WSS on port 8444 speaking
   the relay's viewer protocol (app/liveview.py `_handle_viewer`), so
   static/js/liveview-player.js works unchanged. Album: library list,
   thumbnails (Pillow), files, from the Pi's photos folder.
2. [x] **Credentials** (server): per telescope an offline secret (HMAC key),
   an HTTPS name `<slug>.tel.<APP_DOMAIN>` -> 10.42.0.1 (Cloudflare DNS, DNS
   only) and a Let's Encrypt cert by DNS-01 (Cloudflare TXT). Needs
   `CLOUDFLARE_API_TOKEN` (Zone.DNS edit) in instance/.env: Alex has to add it;
   without it everything else works with a self-signed cert in tests only.
   Telescopes fetch them with their API token (`/api/telescope/offline_credentials`)
   while online; renewed daily when < 30 days left.
3. [x] **Offline passes** (server): `/interface/offline_passes` gives the
   logged-in PWA a signed pass for each telescope it may control (owner/shared,
   role, expiry 30 days), plus its HTTPS name. Stored in the browser.
4. [x] **PWA** (server static): service worker caches the interface, album
   and star map pages and their assets (CDN files too); `static/js/offline-link.js`
   (loaded first by base.html) probes the known telescopes' HTTPS names; when
   one answers, page fetches to the telescope paths go to it with the pass, and
   the selector shows just that telescope. Star map: own page from cache, only
   tracking goes to the telescope. Sky/targets/search answered in the browser
   or by the Pi. Capture plans: "needs seleno.org" offline.
5. [x] **Hotspot DNS** (client deploy): dnsmasq on the hotspot answering
   `*.tel.<domain>` -> 10.42.0.1 (phones without mobile data have no other DNS).
6. [x] **Retire the Pi server copy** from session 1 (offline_mode.py, the
   bundle, offline_server.py) once 1-5 work: decide with Alex, note in the
   final message.
7. [x] Tests (server unit, client unit, e2e in headless Chrome with a
   self-signed cert), docs (both READMEs), this log.

## Log

- 2026-11-18: Session 1 finished (see the README sections "Offline hotspot" in
  the client and "Offline mode" in the server). Option A agreed; log started.
- 2026-11-18 14:30: Commit `e885341 Offline mode p1` (Alex) holds session 1
  plus leftovers of another version Alex didn't want (refs to
  `models/local_access.py`, `controllers/local_access.py`,
  `offline_mode.start_tls_server/tls_context`, a "local mode" in the service
  worker) whose files don't exist: the server would fail at its next restart.
  Removed those refs (Server.py, app/liveview.py, templates/base.html,
  tests/harness.py) and restored static/js/star_map_sw.js to 29d5e50. Kept:
  base.html registering /sw.js on every page (option A needs it).
  Alex: don't read the other sessions' transcripts; the commit is fine to read.
- 14:45: Decisions for step 1 (Pi API): no aiohttp on the Pi, so stdlib
  ThreadingHTTPServer+ssl for the API (port 443) and `websockets` for live
  view (WSS 8443, relay viewer protocol, client JPEGs wrapped in SLV2
  headers). Endpoint logic in client `utils/phone_api.py`, servers/auth/CORS in
  `core/networking/phone_link.py`. Client `utils/sky_tonight.py` = copy of the
  server's app/sky.py (gettext shim) for /interface/api/sky and targets.
  Search offline: the Pi's sky_cache (stars <= mag 5 + names) plus an "offline
  pack" from the server (featured targets, named DSOs) saved with the
  credentials. Photos taken from the phone: capturePhoto(owner = the pass's
  seleno.org user id), kept in tonight's offline folder and queued for
  seleno.org, so they upload by themselves once back online.
- 15:10: Step 1 written (not yet tested): client `utils/phone_api.py`
  (routes), `utils/offline_pack.py` (search/featured targets),
  `utils/sky_tonight.py` (copy of app/sky.py), `core/networking/phone_link.py`
  (HTTPS :8443 + WSS :8444, passes "v1.<b64 json>.<b64 hmac>", live view
  tickets "lv....", CORS from creds["origins"]), `utils/phone_credentials.py`
  (GET /api/telescope/offline_credentials -> config/phone_link/). Ports 8443/
  8444 chosen so no privileged ports are needed. Still to wire: hotspot.apply
  -> phone_link.apply (instead of offline_server), phone_credentials.start()
  in websocket.start_services, panel status item.
- 15:40: Step 1 tested (tests/test_phone_link.py: passes, API routes, real
  TLS + CORS + Range + WSS live view). Retired session 1's Pi server copy on
  the client: deleted core/networking/offline_server.py and its tests;
  server_link offline -> None; uploads queued offline go to seleno.org later.
  Found the other version's edits in client files too (handlers.py
  capturePhoto/liveViewViewers, panel_ui.py log throttling, panel_menu.py
  undefined _local_access_* items): reverted to session-1 + this work.
  Server side of session 1 (app/offline_mode.py, offline_bundle,
  build_offline_bundle) still to retire, plus ~/SelenoServer on the Pi (388 MB,
  leave for Alex to delete).
- 16:05: INCIDENT. Alex had installed the hotspot helper
  (/usr/local/sbin/seleno-hotspot) since session 1. Running
  esp32.tests.test_panel_ui toggles offline mode -> hotspot.apply() -> the
  real helper -> the Pi's Wi-Fi switched to the hotspot (wlan0 10.42.0.1),
  so the Pi dropped off the network (ssh -p 2222 goes to its Wi-Fi address).
  Alex plugged in ethernet (eth0 192.168.0.58). To restore:
  `sudo /usr/local/sbin/seleno-hotspot down </dev/null` on the Pi.
  FIX BEFORE RUNNING CLIENT TESTS AGAIN: tests must never reach the helper
  (patch hotspot.HELPER / helper_installed in tests/helpers.ScratchConfig and
  esp32/tests/test_panel_ui.py setUp, and a guard in hotspot._run when
  SELENO_TESTS is set).
- Server side in progress (not yet tested): models/telescope_offline.py,
  app/cloudflare_dns.py, app/acme.py, app/offline_access.py. Still to write:
  controllers/offline_access.py (GET /api/telescope/offline_credentials,
  GET /interface/offline_passes), Server.py wiring (model import, start_job),
  tests, then the PWA (step 4).
- 16:20: Alex ran the helper's `down`; Pi back on Wi-Fi (wlan0
  192.168.0.55, eth0 192.168.0.58 also plugged in). Guard added: tests set
  `SELENO_TESTS=1` (tests/helpers.py, esp32/tests/test_panel_ui.py) and
  `core/networking/hotspot.helper_installed()` returns False while it's set, so
  no test can reach the helper (every helper call checks it first). Verified on
  the Pi: helper on disk, invisible to tests.
- 16:25: Client: 119 tests + 67 panel tests pass, ruff clean, Wi-Fi untouched
  after each run, real config has no hotspot section. Server new modules
  compile. See "Resume here" at the top for the next steps.
- 16:50: Steps 2+3 done (see "Resume here"). Next: step 4, the PWA.
- 18:10: Step 4 done, e2e passes (tests/e2e/hotspot.py). Next: step 5 (hotspot
  DNS), step 6 (retire server-side session-1 offline_mode/bundle), READMEs,
  then restart the server and hand over (Alex: CLOUDFLARE_API_TOKEN, rerun
  hotspot-setup.sh for dnsmasq, real phone test).
- 18:45: Steps 5, 6, docs done. Restarting the server next (deploys the new
  endpoints, worker and nav button).
- 18:55: Server restarted (healthy). Pi fetched real credentials (cert
  pending: no Cloudflare token). Handover: see "Resume here".
- 18:40 (Pi clock): Cloudflare token works: the Pi has a real Let's Encrypt
  cert for seleno-01-22.tel.seleno.org (to 2027-02-16; DNS -> 10.42.0.1).
  hotspot-setup.sh was rerun (seleno-dns unit, dnsmasq present).
  Alex asked: (1) make sure the hotspot is on, (2) keep its on/off, name and
  password in a JSON file in config/. Done (2): config/hotspot.json
  (`enabled`, `name`, `password`, 0600; utils/config_state.py HOTSPOT_FILE_*,
  hotspot.json sits next to client_config.json so the tests' scratch config
  moves it too; old values in client_config.json's hotspot section move over
  on the next save). hotspot.start() makes it (ensure_credentials) and a
  thread checks it every 3 s for changes by hand (hotspot.check_file());
  a name/password that won't do is replaced and logged. Tests: HotspotFile
  (4) in tests/test_hotspot.py, and the dns_names test no longer reads the
  Pi's real credentials. 125 client tests + 54 panel tests OK, ruff clean.
  Real file made: Seleno-01 / (see config/hotspot.json).
  (1): offline mode was off (client_profile.json offline=false), which is why
  there was no Wi-Fi. A reverse SSH tunnel to keep access was refused (auto
  mode won't add a key to the server), so: set offline=true and restarted
  the client; SSH on 2222 (-> 192.168.0.55, wlan0) drops when it switches.
  Undo: panel Setup > Offline mode > Off, or `sudo /usr/local/sbin/seleno-hotspot
  down </dev/null` on the Pi, or reboot. If hostapd fails, the helper puts
  the ordinary Wi-Fi back by itself.

- 19:05: Alex's phone test: the phone joined the hotspot (so hostapd works),
  but the PWA showed the "You're offline" page (star map / try again). Cause:
  the app opens at "/", which the worker doesn't store, and the offline page
  had no way to the interface; the manifest also had no start_url/display.
  Fixed: star_map_sw.js otherPage(): "/" offline (4 s) -> 302 to the stored
  /interface/; offline.html gets a "Telescope interface" button; manifest has
  id/start_url/scope "/" and display standalone. e2e hotspot.py: 21/21 (new:
  the start page offline goes to the interface and finds the telescope, with
  the telescope forgotten first; until_reloads() survives the page's reload).
  Server restarted, live. Alex turned the hotspot off again.
  Next: Alex opens the app online once (new worker), then retries on the
  hotspot. Router port-forward 2222 -> 192.168.0.58 (eth0) suggested so SSH
  survives the hotspot (the server is remote, can't reach 192.168.0.58).
- 19:15: Alex asked to remove the "Telescope nearby?" menu button (the site
  needs internet, the telescope's Wi-Fi has none, so the automatic look is
  enough). Removed from base.html and offline-link.js (SelenoLink.
  connectNearby() kept for the tests/console); README updated; e2e 21/21
  (step 2 calls connectNearby(); checks the button is gone). Server restarted.
