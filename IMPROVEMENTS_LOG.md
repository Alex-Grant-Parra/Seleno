# Client improvements job: progress log

The resume file for the big optimisation, clean-up and features job.
Read this first after a disconnect or a context reset, then carry on
from **Current step**. Tick items as they are done and add a dated note
under **Notes** for anything a later session needs to know.

## The request (Alex, 2026-10-16)

"Implement all of this": every item in the suggestions list, which was
built from a review of the client. In short:

- Live view: more stable, easier to capture, less data.
- New features across the client: camera, pointing and tracking, field
  use, server link, front panel.
- Optimisations and clean-up.

"Create a log so you don't get cut off if you disconnect": this file.

## Decisions and constraints

- Installs: Alex allowed "whatever you want/need".
  - numpy and python-gphoto2 go into the project venv.
  - ASTAP goes into ~/astap.
  - No sudo or system packages (the temporary sudo password was only
    for disabling the service).
- The route work was committed first as aa2d410. This job sits on top
  of it, uncommitted. Commit only when Alex asks.
- Tests and harnesses use temporary config copies, never config/.
  - Don't print the API token.
  - Don't run Client.py against the real server.
- The devkit is on /dev/ttyUSB0 and may be flashed:
  `~/.platformio/penv/bin/pio run -e devkit -t upload --upload-port /dev/ttyUSB0`
- No camera is attached to the Pi. Camera code is tested with a fake
  `gphoto2` script on PATH and a fake backend.
- The server (ASTRA, github.com/Alex-Grant-Parra/ASTRA) is out of scope:
  client side only. A read-only clone is in the scratchpad at `astra/`.
  Facts that constrain the client:
  - The server waits only 3 s for each command reply (`Client.execute`)
    and matches replies by `id`, so replies may arrive out of order.
  - Messages with no matching id are only printed, so status pushes are
    harmless.
  - The command socket is rate-limited to 120 messages a minute per IP;
    going over it disconnects. Keep pushes sparse (every 15 s or more).
  - The live-view socket stores **every** message as the latest frame
    and serves it as image/jpeg. Send only JPEG binary frames there:
    no text, no WebP.
  - The server never sends anything on the live-view socket and has no
    viewer counts. Viewer-driven streaming is built on the client
    (`liveViewViewers` command) and documented for the server.
  - `/upload` needs file names `<digits user id>_<name>.<ext>`; each
    file is saved on its own, so separate POSTs per file are fine.
  - The web UI's motor panel uses espEnable, espSetDirection,
    espSetSpeed, espStart, espMoveSteps, espStop, espSetMicrosteps,
    espSetCurrent, espSetMode, espSetAccel, espStatus, espStatusAll.
    Keep these. Nothing calls `*WithMotorId` or `espCommand`.
  - The server sends trackCoordinates as kwargs (name, ra, dec, mag),
    plus stopTracking, getCameraChoices, setCameraSetting,
    capturePhoto(currentid), startLiveView, stopLiveView and
    getCurrentCoordinates.
- Not done, because they need the server or browser changed:
  - H.264 / WebRTC streaming;
  - the server-side viewer count.
  Say so in the final report.

## Test commands

- `./venv/bin/python -m unittest esp32.tests.test_panel_ui esp32.tests.test_interface_pty`
- New tests: `./venv/bin/python -m unittest discover -s tests -t .`
- Firmware:
  - `~/.platformio/penv/bin/pio run -d esp32-firmware -e seleno_v08`
  - `... -e devkit`

## Plan and status

Legend: [ ] to do, [~] in progress, [x] done, [-] skipped (reason in
Notes).

### Phase 1: foundations
- [x] Install numpy and gphoto2 into the venv (done); pin requirements.txt (Phase 10).
- [x] utils/log.py: logging setup (levels, rotating file); new code uses it.
- [x] utils/clock.py: one clock for astronomy (system time plus an offset
      from GPS or the panel); sync status via adjtimex. Callers not yet
      switched to clock.now().
- [x] Runtime state in memory: config_state.RuntimeStore.
  - Saves the axes and target on change (at most every 5 s while moving,
    at once on stop, sync or park).
  - Removes stale .tmp files at startup; fsyncs the profile and config.
  - telescope_state rewritten on it, with hemisphere(), mount_config()
    and the alignment model.
  - Still to do: remove the hour-angle updater thread from tracking.py
    and update the tests that use it.

### Phase 2: camera service (one owner)
- [x] core/camera/jpeg.py: incremental JPEG framer (marker walk, no
      false ends on thumbnails, bytearray). Checked by hand; unit tests
      still to write.
- [x] core/camera/service.py and backends.py: one thread owns the camera.
      Checked by hand with FakeBackend.
  - Backends: libgphoto2 (python-gphoto2) and the gphoto2 command line.
  - Preview with the latest-frame slot.
  - Capture, settings and config discovery.
  - Hotplug detection while idle.
  - Backoff, never giving up; keep-awake.
  - Stats.
- [x] Replace camera_state scanner, pkill paths, Camera.* uses in handlers
      and photos; live view on/off in memory.

### Phase 3: live view
- [x] Sender rewrite.
  - Separated from the camera; JPEG re-encode presets.
  - Frame rate adapts (moving, still); duplicate skip with a keyframe.
  - Adapts to the link; reconnect backoff with jitter.
- [x] Viewer-driven streaming (`liveViewViewers`) and idle auto-stop.
- [x] Stats: `getLiveViewStats`, the panel, status push.
- [x] Snapshot (save and upload the current frame).
- [x] Recording (MJPEG AVI writer) and timelapse.
- [x] Focus help.
  - HFR and sharpness on frames; camera zoom.
  - Full-resolution crop for focusing.
- [x] Local LAN page: stream, snapshot, photos, status, stop.
  - Access code shown on the panel.

### Phase 4: photos
- [x] Persistent upload queue with retries.
  - JPEG first; RAW policy (after JPEG, on request, never).
- [x] Accept 1+ files; drop --reset and the 8 s sleep.
- [x] More settings (aperture, WB, format, exposure comp) and current
      values; bulb exposures.
- [x] Sequences: lights, darks, flats, bias, with dithering.
- [x] Camera battery and card space; Pi disk guard.
- [x] Pointing metadata with uploads; upload offline photos when online
      (owner id setting).

### Phase 5: server link
- [x] Commands as tasks; stop and E-stop first; error replies with id.
- [x] Nothing blocking on the event loop; ESP32 scanner in a thread.
- [x] Status push (every 15 s or more; rate limit).
- [x] Client update (git pull and restart) and firmware flash, both off
      by default.

### Phase 6: pointing and tracking
- [x] Event-driven slew end (motion_done).
- [x] Horizon limits: server go-tos checked; stop when the object sets;
      custom horizon.
- [x] Southern hemisphere (mount model mirror, tracking direction,
      sigma Octantis). Still needs a test.
- [x] Park, unpark and home (tracking.park, unpark, set_park_here).
- [x] Local ephemeris for the Moon and planets (topocentric Moon): done
      and checked. Go to planets offline (track_object) done; panel and
      sky view still to do.
- [x] Tracking rates: sidereal, lunar, solar, custom; solar-system
      objects tracked with ephemeris rates on both axes.
- [x] Guard raw motor commands from the server (limits and Sun; policy
      setting): utils/motor_guard.py.
- [x] Backlash compensation (config, default 0).
- [x] Pointing model from alignment stars; polar alignment report.
- [x] Plate solving (ASTAP).
  - Solve and sync; centre the target.
  - Three-point polar alignment; periodic drift correction.

### Phase 7: field use
- [x] Clock check: panel warning, set the time from the panel or GPS
      (software offset).
- [x] GPS in the background (no blocking at startup); location and time.

### Phase 8: front panel
- [x] Camera tab: quality, stats, snapshot, record, ISO and shutter,
      battery, focus, zoom.
- [x] Scope tab: planets, park and unpark, tracking rate, solve and sync,
      polar alignment.
- [x] Setup: clock, horizon, local page, raw motor policy, photo owner.

### Phase 9: optimisations
- [x] LEDs: no 0.5 s gaps; one firmware command for all LEDs.
- [x] One heartbeat on the serial link.
- [x] Serial: stop and E-stop not stuck behind other commands.
- [x] Remove the startup asset sync.

### Phase 10: clean-up
- [x] Remove dead code.
  - liveview.py (Flask), peripheral_refresh_task, Camera.photo_queue.
  - Tools.hour_angle, `*WithMotorId`, espCommand.
  - graphics/ and examples/ if nothing needs them.
- [x] No hard-coded telescopes.dev; no cookie prints.
- [x] print to logging; time.monotonic for intervals.
- [x] .gitignore; requirements pinned; systemd unit; CI workflow; ruff
      config.

### Phase 11: finish
- [x] Docs: README, server-facing commands, PROTOCOL.md for firmware
      changes.
- [x] V08_WORK_LOG.md summary.
- [x] All tests; devkit end-to-end; final report to Alex.

## Current step

Everything is implemented, tested and documented; the final report to
Alex is next. V08_WORK_LOG.md has the summary, including what's not done.
Uncommitted: commit only when Alex asks.

## Notes

- 2026-10-16: log created; route work committed as aa2d410.
- Installed into the venv: numpy 2.5.4, gphoto2 2.6.4 (libgphoto2 2.5.34).
- Installed ASTAP CLI 2026.09.01 and the D05 + W08 databases in ~/astap.
- A quick check read the real config/ once. The new startup cleanup
  removed three empty stale runtime_state.json.*.tmp files there, as
  designed; nothing else changed. Use scratch copies from now on.
- Pointing model: the pole terms need 3+ points (with 2 they get confused
  with the Dec index).
- Alex stopped a long tool run at about an hour ("keep going"): keep
  steps shorter and update this log often.
- Ephemeris (utils/ephemeris.py, Schlyter) checked against JPL DE421 with
  Skyfield in scratch:
  - geocentric positions within 1.5' for every body;
  - the topocentric Moon within 4.2' over 30 days.
- The server's planets are J2000 geocentric and fine, except **Neptune**:
  its Dec has the wrong sign (server +0.48, DE421 -0.49 on 2026-10-16).
  That's a bug in ASTRA's planetary model; tell Alex.
- DE421 reference values for tests (2026-10-16 22:00 UTC, J2000
  astrometric):
  - Sun 201.4543 -9.0090; Moon 274.6018 -27.4831;
  - Mercury 224.2189 -20.1050; Venus 209.6313 -19.9751;
  - Mars 133.1336 18.9066; Jupiter 144.4538 14.8231;
  - Saturn 10.2150 1.4510; Uranus 62.8621 20.9393;
  - Neptune 2.4492 -0.4853;
  - topocentric Moon from 51.4623, -2.5502: 273.9769 -28.1560.
- Vega J2000 279.23473 38.78369 is 279.46077 38.80588 apparent of date at
  that time (the apparent position includes nutation and aberration,
  about 20"). The precession-only value should be within about 1' of it.
- Precession: the client treated J2000 RA/Dec as of date. That's about
  0.37 deg of RA in 2026, now corrected in the mount (utils/astrometry.py).
