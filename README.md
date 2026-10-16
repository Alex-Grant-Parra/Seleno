# SELENO telescope client

The client runs on the telescope's Raspberry Pi 5. It joins the telescope to the
ASTRA server ([github.com/Alex-Grant-Parra/ASTRA](https://github.com/Alex-Grant-Parra/ASTRA)), or works
without one. It drives the mount through the ESP32 controller board
(`esp32-firmware/`, protocol in `esp32-firmware/PROTOCOL.md`). It owns the
camera, streams live view, takes and uploads photos, and runs the front
panel's menus.

- [Setting up](#setting-up)
- [What it does](#what-it-does)
- [Configuration](#configuration)
- [Commands the server can send](#commands-the-server-can-send)
- [Tests](#tests)
- [Where things are](#where-things-are)

## Setting up

[QUICK_SETUP.md](QUICK_SETUP.md) has the step-by-step version. In short:

```bash
python -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python Client.py
```

**First start.** The front panel asks for the device name, the server and the
API key. Choose **Offline** there to use the telescope without a server.
Without a panel, use `config/client_profile.json` or `python Client.py setup`.

**Running as a service.** `deploy/seleno-client.service` is a systemd unit.
The comment at its top says how to install it. It restarts the client if it
fails. The log goes to the journal and to `logs/seleno.log`.

**Plate solving (optional).** This needs ASTAP's command-line solver and a
star database in `~/astap`:
- `astap_cli` for ARM64;
- D05 for telescope-sized fields, W08 for wide lenses.

Without them, everything else works and the solving items stay hidden.

## What it does

### Live view

One thread owns the camera (`core/camera/service.py`), through libgphoto2 in
the same process. If the `gphoto2` Python package is missing, it uses the
`gphoto2` command instead. Nothing kills gphoto2 processes or fights over the
USB port any more.

**Taking a photo during live view.** Live view pauses, the photo is taken,
and live view carries on.

**When live view fails.** It tries again after 1 s, then waits longer each
time, up to 30 s. It never turns itself off. An unplugged camera is noticed,
and found again when it's plugged back in.

**While live view runs.** The camera's auto power-off is switched off, and
put back afterwards.

**What reaches the server is smaller** (`core/camera/stream.py`):

| Quality (`liveview.quality`) | Frame |
|---|---|
| `original` | As the camera sends it |
| `high` | Full size, JPEG quality 80 |
| `medium` (default) | Half size, quality 70: about a ninth of the data |
| `low` | Quarter size, quality 60 |

- **Frame rate:** up to 10 fps while something moves or changes. 2 fps while
  the view is still, skipping frames that look the same, but always one every
  5 s.
- **Slow links:** quality steps down when sends get slow, and back up when
  they're quick again. Before, a send over 2 s dropped the connection and
  restarted the camera.
- **Nobody watching:** once the server reports viewer counts
  (`liveViewViewers`), live view pauses a minute after the last viewer goes,
  and the camera rests. `liveview.idle_stop_min` turns live view off after a
  while with no viewers.
- **Focus mode:** sends a full-resolution crop round the brightest star, and
  measures its HFR (half-flux radius; smaller is sharper).
- **Zoom:** the camera's 5× and 10× live-view zoom, where it has one (Canon).

**Capturing from live view** (`core/camera/recorder.py`):
- a snapshot saves the current frame at once, without the shutter;
- video records every frame into an MJPEG AVI, for stacking Moon and planet
  frames in AutoStakkert or PIPP;
- timelapse records one frame every few seconds.

### Photos

- **Photos the server asks for** are saved in `photos/default/` and queued
  for upload (`utils/uploads.py`).
- **How the queue works:**
  - it survives restarts and retries with growing waits;
  - JPEGs go first; RAWs follow the `upload.raw` setting (`after_jpeg`,
    `on_request` or `never`);
  - each file goes on its own, with a description of where the telescope
    pointed.
- **Panel photos** (Camera tab) go to `photos/offline/<night>/`, logged in
  `photos.jsonl`. A run can be lights, darks, flats or bias frames. It can
  use bulb exposures and dither between frames, and darks and bias ask for
  the cover first.
- **Uploading offline photos:** set the photo owner (your account id) in
  Setup, then use "Upload offline" in the Camera tab.
- **Low disk space:** photos and recordings stop when the Pi has under
  300 MB free.

### Pointing and tracking

Go-tos come from the panel (Scope tab) or the server. The order is:

1. **The Sun guard** (`utils/sun_guard.py`): a warning within 30° of the Sun,
   and a code to enter on the panel within 20°.
2. **The clock, the park and the horizon limit.**
3. **The slew:** the quickest way, over a pole if that's quicker, curving
   round the Sun (`core/hardware/mount.py`).
4. **Tracking**, with the motors run at the right rates.

- **Coordinates:**
  - people and catalogues use J2000;
  - the mount aims at the place of date, corrected for precession (0.37°
    by 2026) and lifted by refraction (`utils/astrometry.py`).
- **The Moon and planets** are worked out on the Pi (`utils/ephemeris.py`),
  within 1.5′ of JPL's DE421. That makes them work offline, in "Go to
  planet" and in the sky view.
- **Tracking rates:**
  - both axes follow the Moon's and planets' own motion;
  - stars use the sidereal rate;
  - lunar, solar and King rates can be chosen, and a custom rate can be set
    in the config;
  - a gentle correction removes any drift.
- **Alignment** (`core/hardware/pointing.py`):
  - each "Align on star" or plate solve is a point, and a pointing model is
    fitted to the points;
  - one point corrects the offsets;
  - three or more find how far the polar axis is out, and say how to move it.
- **Plate solving** (`utils/plate_solve.py`, with ASTAP):
  - solve and sync;
  - centre the target;
  - three-point polar alignment;
  - optional drift checks while tracking.
- **Park:** slews to the park position (the pole, counterweight down, by
  default) round the Sun. Go-tos wait until it's unparked. "Shut down Pi"
  can park first.
- **Southern hemisphere:** picked from the site's latitude. The mount model
  is mirrored and sidereal tracking turns the RA motor the other way.
- **Horizon:**
  - set a minimum altitude, and optionally a profile of trees and roofs;
  - server go-tos below it are refused, and the panel asks;
  - tracking stops when the target sets.
- **Backlash:** extra steps when an axis reverses (`mount.backlash_steps`).
- **Raw motor commands** from the server (the web page's motor panel) are
  guarded (`utils/motor_guard.py`). They're refused past the axis limits or
  towards the Sun, and continuous moves are stopped there.

### Field use

- **Clock** (`utils/clock.py`):
  - go-tos need the right time;
  - offline, with no RTC battery, the Pi's clock is wrong after a power cut;
  - the panel warns, and the time can be set from GPS or typed in
    (Setup > UTC);
  - nothing needs root.
- **GPS:** read from `gpsd` in the background, for the site and the time.
- **Local page** (`core/networking/local_page.py`):
  - turn it on in Setup;
  - it serves live view, photos, snapshots, recording and stop to a phone at
    `http://<pi>:8080/`;
  - it asks for the code shown on the panel.

### Server link

- **Command lanes:** commands run in lanes (`core/networking/websocket.py`),
  so a photo doesn't hold up a slew, and a stop never waits.
- **Replies:** every reply carries the request's id.
- **Live view connection:** opened only while live view is on.
- **Reconnects:** wait 2 s, then longer each time up to a minute, with jitter.

## Configuration

**`config/client_profile.json`:** the device name, server and API key, offline
mode and the site.

**`config/client_config.json`:** everything else, by section. Missing values
take their defaults, and most can be set from the panel.

| Section | Settings |
|---|---|
| `slew_config` | Speeds, gear ratios, centring thresholds |
| `mount` | `ra_axis_limits`, `dec_axis_limits`, `pole_crossing`, `hemisphere` (`auto`/`north`/`south`), `park_axes`, `backlash_steps` |
| `horizon` | `min_alt_deg`, `profile` (`[[az, alt], ...]`), `stop_when_set` |
| `tracking` | `rate` (`sidereal`/`lunar`/`solar`/`king`/`custom`), `custom_arcsec_per_s`, `auto_rate`, `refraction` |
| `liveview` | `quality`, `max_fps`, `still_fps`, `keyframe_s`, `adaptive`, `idle_stop_min`, `viewer_grace_s` |
| `camera` | `backend` (`auto`/`libgphoto2`/`cli`/`fake`), `keep_awake` |
| `upload` | `raw` (`after_jpeg`/`on_request`/`never`), `retry_max_s` |
| `photos` | `local_dir`, `owner_id`, `shots`, `kind`, `exposure_s`, `dither` |
| `local_page` | `enabled`, `port` |
| `server` | `raw_motor_commands` (`guarded`/`off`/`unrestricted`), `status_push_s` (0 = off), `allow_remote_update`, `allow_firmware_update` (both off by default) |
| `solver` | `path`, `fov_deg` (0 = learn it), `search_radius_deg`, `timeout_s`, `centre_tolerance_arcmin`, `drift_check_min` |
| `gps` | `enabled` |

`camera.backend: "fake"` gives a made-up camera with a star field, for trying
everything without one.

**`config/runtime_state.json`:** where the telescope points and this session's
alignment. It's kept in memory and written only when something changes.

## Commands the server can send

The server calls these by name: `{"type": "call", "function", "args",
"kwargs", "id"}`. The existing ASTRA web page uses the first group unchanged.
Long jobs (park, plate solves) start in the background and reply at once,
because the server waits only about 3 s. `getJobStatus(name)` says how a job
went.

| Group | Functions |
|---|---|
| Already used by ASTRA | `trackCoordinates(name, ra, dec, mag)`, `stopTracking()`, `getCurrentCoordinates()`, `getCameraChoices()` (now also `current` values and more settings), `setCameraSetting(label, value)`, `capturePhoto(currentid, exposure_s=None)`, `startLiveView(quality=None)`, `stopLiveView()`, `espEnable`, `espSetDirection`, `espSetSpeed`, `espStart`, `espMoveSteps`, `espStop`, `espSetMicrosteps`, `espSetCurrent`, `espSetMode`, `espSetAccel`, `espStatus`, `espStatusAll` |
| Live view | `liveViewViewers(count)`, `getLiveViewStats()`, `setLiveViewOptions(**options)`, `setLiveViewQuality(quality)`, `setLiveViewFocus(on)`, `setCameraZoom(level, x, y)`, `liveViewSnapshot(currentid=None)`, `startRecording(kind, interval_s)`, `stopRecording()`, `getRecordingStatus()` |
| Camera and photos | `getCameraStatus()`, `startPhotoSequence(kind, count, exposure_s, interval_s, dither_every, dither_arcmin)`, `stopPhotoSequence()`, `getPhotoSequenceStatus()`, `getUploadQueue()`, `uploadPending(kind)`, `retryFailedUploads()`, `uploadOfflinePhotos(currentid)` |
| Pointing and tracking | `trackObject(name)` (Moon, planets, named stars), `park()`, `unpark()`, `setParkHere()`, `setTrackingRate(rate, custom_arcsec_per_s)`, `dither(arcmin)`, `getAlignment()`, `clearAlignment()`, `solveAndSync(source)`, `centreTarget(source)`, `polarAlign(step_deg, source)`, `getMountStatus()`, `getJobStatus(name)`, `setClock(utc)`, `setHorizon(min_alt_deg, profile, stop_when_set)`, `getPlanets()` |
| ESP32 extras | `espSetAuto`, `espDriverStatus`, `espEstop`, `espClearEstop`, `espEstopStatus`, `espInfo`, `espPanelStatus`, `espImuRead`, `espTurnDegrees` |
| The client | `echo`, `getClientStatus()`, `updateClient()`, `updateFirmware()` (the last two only when allowed in config) |

**Two server-side additions would help:**
- **Viewer counts.** Call `liveViewViewers(n)` when people start or stop
  watching `/liveview/<client>`, so the stream can pause with nobody
  watching. Until then it streams whenever live view is on.
- **Status messages.** With `server.status_push_s` set, the client sends a
  `{"type": "status", ...}` message without an id that often. ASTRA
  currently only logs messages without an id, so this is off by default.

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -t .                                  # the client
./venv/bin/python -m unittest esp32.tests.test_panel_ui esp32.tests.test_interface_pty  # panel and serial link
./venv/bin/ruff check .                                                               # lint
```

No camera, ESP32 or network is needed. The camera tests use a fake camera and
a fake `gphoto2`, and the solver test runs the real ASTAP if it's installed.
`.github/workflows/tests.yml` runs the tests, the lint and both firmware builds.

## Where things are

| Folder | What's in it |
|---|---|
| `Client.py` | Startup |
| `core/camera/` | The camera service and backends, live view stream, JPEG framing, focus, recording |
| `core/hardware/` | Tracking, the mount model and route planner, the pointing model |
| `core/astrometry/` | ASTAP plate solving |
| `core/networking/` | The server links, CSRF, the local page |
| `utils/` | Configuration and state, the front panel (`panel_ui.py`, `panel_menu.py`, `manual_control.py`), the Sun guard, ephemeris, astrometry, clock, GPS, uploads, photos, LEDs |
| `esp32/` | The ESP32 serial interface and bring-up tools |
| `esp32-firmware/` | The controller firmware (PlatformIO) |
| `deploy/` | The systemd unit |
| `tests/` | The client's tests |
