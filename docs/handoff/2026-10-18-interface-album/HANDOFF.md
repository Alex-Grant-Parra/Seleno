# Handoff: interface and album redesign (updated 18 Oct 2026, second session)

## Read this first

- **The pages are rebuilt, the in-browser processing engine is not.** The interface page
  (Basic and Advanced) and the album (library, inspector, viewer and blink, quality chart,
  flags and types, stack dialog, saved stacks, editor, activity) are done, tested end to end
  in headless Chrome, and match the mockups. Everything that needs pixels processed in the
  browser (decoding RAWs, measuring stars, aligning, stacking, developing) waits on the
  **JS processing engine**, which Alex asked to leave for a later session. The pages call
  it through one hook, `static/js/album/engine.js`; see "Next: the engine" below.
- **Mockups:** <https://claude.ai/artifact/ECb8nkLLfTNahpRazeQ24c> (source in
  `scratch-backup/mockups/project/`). Alex approved them.
- **Stacking runs in the browser, in JS**, not on the server (Alex's decision; the VPS can't
  stack for every user). `app/processing/` (Python) is the tested reference to port.
- **Nothing from this session is committed.** The v0.8 server work from the session before
  is committed (`38cd426 match clientside`).
- **For the Pi team:** `docs/client-requests-v0.9.md`. The server is already ready for every
  item in it.
- `HANDOFF.docx` in this folder is the first session's version; this file replaces it.

## What Alex asked for

> Completely redo the album and interface pages, with a focus on taking proper, accurate astrophotography photos. The interface should be about getting the data; the album about processing and viewing it, including stacking. Have a basic and an advanced mode, chosen with a toggle in the top right. Keep most of the existing features; feel free to add more. If anything should be added to the telescope nodes (the Pi clients), write it up in a file so it can be passed on, and be clear about what's wanted.

## Repo state (all uncommitted)

New:

| Path | What |
|---|---|
| `app/sky.py` | Low-precision Sun and Moon, twilight, night window, target altitude, transit, hours above 30°, verdicts. Checked against the site's planetary model (within 0.4°, all precession) and London twilight tables (minutes). |
| `app/album_index.py` | Indexes every file in `camera_photos/<uid>/` into `frame_meta` (EXIF cached, re-read only when a file changes); pairs JPEG and RAW by name or upload `pair_id`; a small TIFF reader for RAW EXIF and the embedded preview (skipping the lossless-JPEG sensor data); cached thumbnails in `.thumbs/` (Pillow `draft`, about 65 ms for a 21 MP JPEG; ffmpeg for videos). |
| `models/album.py` | `frame_meta` (per-file index, user flags/type/rating, target and notes, browser-measured metrics), `stacks` (saved results), `featured_targets` (Basic mode's suggestions, seeded at startup, coordinates from the catalogue). |
| `controllers/album_api.py` | `GET /album/api/library`, `GET /album/thumb/<f>?size=sm\|md`, `POST /album/api/frames` (flag, rating, type, target, notes; JPEGs keep target and notes in EXIF as before), `POST /album/api/metrics`, `POST /album/api/request_raws`, stacks (`POST /album/api/stacks`, `PUT .../file/<name>`, chunked `PUT .../master?offset=`, `POST .../finish`, `GET/PATCH/DELETE /album/api/stacks/<id>`, `GET /album/stacks/<id>/<file>` with ranges), `POST /album/api/save_image`. |
| `controllers/interface_api.py` | `GET /interface/api/sky` (Sun, twilight, Moon, target), `GET /interface/api/targets` (featured targets ranked for tonight), `GET /interface/api/latest_captures`. Site from the telescope row, then the latest status, then uploads; rounded to ~1 km. |
| `static/workspace.css`, `static/js/workspace.js` | Shared full-width layout, components (`ws-`), the Basic/Advanced toggle (`localStorage['seleno_mode']`, `data-ui-mode` on `<html>` and `<body>`, `.basic-only`/`.adv-only`), helpers (`Seleno.api`, `fmt`, `toast`, `sparkline`, RA/Dec parsing). |
| `static/js/interface-guide.js` | Basic mode: the four steps, target picker, exposure presets, test shot with plain checks (the wording of `analysis.checks`), one-card capture plan with estimate against the booking and the target's altitude, tonight's strip. |
| `static/js/focus-meter.js` | Live star size (HFR) from the live-view canvas: a small estimator for focusing, separate from the engine. |
| `static/js/album/*.js` | The album as ES modules: `main.js` (library, groups, grid/table, inspector, bulk actions, views), `quality.js` (grouping, quality judgement, chart), `viewer.js`, `stack-dialog.js`, `stacks.js` (saved stacks, the chunked uploader), `editor.js`, `jobs.js` (Activity), `engine.js` (the hook). |
| `tests/` | The harness and the previous session's tests moved here, new API tests, and `tests/e2e/` (stub server with synthetic star fields and a fake telescope streaming an H.264 focus sweep; Chrome runner; scenarios). See `tests/README.md`. |
| `docs/client-requests-v0.9.md` | The Pi write-up. |

Changed: `templates/interface.html` and `static/js/interface.js` (rewritten; every old
control kept; the plan builder's IDs and globals kept), `templates/album.html` (rewritten;
all old routes kept), `controllers/album.py` (zips spool to a temp file instead of RAM,
store already-compressed files, accept a form post; deleting clears index rows and
thumbnails), `controllers/interface.py` (`telescope_status` returns the booking window;
`take_photo` takes `frame_type`; live-view options `jpeg_quality`, `max_fps`),
`app/telescopeLink.py` (`has_capability`, `upload_pending` filters, `capture_photo`
`frame_type`), `app/WebsocketServer.py` (status keeps `site`, `solve`, `capabilities`),
`models/photo.py` (`meta_json`, added by `ensure_photo_columns()` at startup),
`controllers/telescope_uploads.py` (stores the full meta, accepts `test`, `darkflat`,
`.ser`), `app/capture_plans.py` (`dither_settle_s`), `Server.py` (startup hooks).

**On the live server's next restart:** three new tables (`frame_meta`, `stacks`,
`featured_targets`), one new column (`photos.meta_json`), and 30 featured targets seeded.
Nothing else migrates. No new Python packages.

### Tests (all passing)

- `cd tests && ../venv/bin/python -m unittest test_commands test_uploads_plans test_liveview test_album_api test_interface_api`: 56 tests, about 10 s.
- `tests/e2e/run.py` (interface, album; also `--dark` and `--width=390 narrow narrow_album`):
  both scenarios pass with no page or console errors, light and dark. The interface run
  covers live H.264 decoding, the focus meter over the sweep (HFR 1.9 to 3.2 px and the
  advice), a test shot the fake telescope uploads, starting a plan, every Advanced panel,
  and the plan builder. The album run covers grouping, quality reasons, notes following the
  right photo, flags, the viewer, stretch and blink, the chart and limits, sorting and type
  filters, setting types, the stack dialog's calibration matching, the editor, saving to the
  album, and a 40 MB stack through the real chunked uploader.

## Decisions this session

- **RAW in the browser: our own CR2 reader, not `libraw-wasm`.** Every `libraw-wasm` build
  (1.1.2 to 1.6.0) is a threaded Emscripten build: it needs `SharedArrayBuffer`, so the album
  would have to be cross-origin isolated (COOP/COEP), which risks breaking the CDN assets
  (Bootstrap, Font Awesome, Google Fonts). Canon CR2 is lossless JPEG (SOF3) in TIFF with
  Canon's slicing; dcraw's logic for it is short and well known. Other formats fall back to
  the JPEG. The telescope's camera (5D Mark II) writes CR2.
- **Oracle for the CR2 reader:** `rawpy` on the CC0 sample
  `https://raw.pixls.us/data/Canon/EOS%205D%20Mark%20II/08.canon.raw.cr2`. LibRaw reads it as
  a 5792 × 3804 raw, visible 5634 × 3752 at top 52, left 158; pattern GBRG; black per channel
  1023/1024 (integer mean of the masked left columns 2..156, rows 52..3803, as dcraw's
  `crop_masked_pixels`); white 15600; matrix 4716, 603, −830, −7798, 15474, 2480, −1496, 1937,
  6651 (/10000); as-shot WB 2016/1024/2335 (Canon ColorData v4, offset 63, RGGB); daylight WB
  from the matrix (dcraw `cam_xyz_coeff`, pre_mul = 1/row sums) 2.391/0.929/1.289.
- **Camera matrices:** LibRaw's `src/tables/colordata.cpp` has 83 Canon EOS rows
  (prefix, black, maximum, 9 coefficients from Adobe DNG Converter), matched by prefix in
  order. Extract them with a regex over
  `{ LIBRAW_CAMERAMAKER_Canon, "EOS…", black, max, { … } }` and credit LibRaw (LGPL 2.1 /
  CDDL) and Adobe in `THIRD_PARTY_NOTICES.md` when the table goes in.
- **Featured targets live in the database** (`featured_targets`), per Alex's preference
  for data in `Data.db`; only the designations and blurbs are seeded from code.
- **New client features are negotiated:** the Pi lists `capabilities` in its status; the
  server sends new arguments only to clients that list them, so v0.8 Pis keep working.

## Next: the engine (static/js/processing/, ES modules, run in Web Workers)

The pages are wired to `static/js/album/engine.js`; set `engine.available = true` and
provide the three calls documented there (`measure`, `stack`, `develop`). What calls them:

- **Measure quality** (album bulk bar): `engine.measure(frames)` → `POST /album/api/metrics`
  (keys already accepted server-side). The quality chart, dots, reasons, limits and the
  interface's test-shot checks (`metrics` in `latest_captures`) light up from those numbers.
- **Start stacking** (stack dialog): `engine.stack(spec)` with the spec `stack-dialog.js`
  builds (frames by type, with `url` pointing at the RAW when stacking RAW, and the settings
  of `stack.py`'s `DEFAULT_SETTINGS` plus `flat_per_channel` and `keep_best`). The result goes
  to `stacks.saveStackResult` (tested) and shows under Stacked images.
- **Editor**: `engine.develop(stack, stageElement)` returns a session that renders
  `develop.py`'s params (already modelled in `editor.js`) and exports JPEG/PNG/TIFF.
- **Edit this photo** and Basic's single-photo edit: same session on one frame.

Suggested modules: `cr2.js` (+ the Canon table), `jpeg.js` (createImageBitmap/OffscreenCanvas,
sRGB→linear LUT), `fits.js`, `half.js` (float16), `image.js` (robust stats by quickselect,
background map, separable blurs, debayer), `stars.js`, `register.js` (triangles, RANSAC,
2-D similarity in closed form, affine, phase correlation), `integrate.js`, `calibrate.js`,
`develop.js`, `stack.js` (pipeline over OPFS), `worker.js`, `client.js`. Test the maths in
Node against `app/processing` on the synthetic data from `scratch-backup/proc/synth.py`.

Notes for the port:

- **Combining in JS:** per pixel, sort the N values (insertion sort under ~32, else a
  shell or quick sort), then apply `integrate.py`'s rule on index ranges: identical results,
  about 400 operations a pixel for N = 40. Split strips across 2–4 workers.
- **Storage:** OPFS with sync access handles in workers for writing aligned float16
  frames (one file per frame); read strips back with `File.slice().arrayBuffer()` (no
  exclusive lock). Cache each downloaded RAW (or its calibrated mosaic) in OPFS between
  the measuring and warping passes. Fall back to memory (super-pixel only) without OPFS.
- **Warping:** Keys cubic (a = −0.5) or bilinear; `scale_transform(m, 2, 0.5)` from the
  half-size star coordinates, as in `stack.py`.
- **Default to super-pixel (half size) in Basic** on machines with
  `navigator.deviceMemory < 8`; the Basic dialog has the checkbox already.
- **Lucky imaging:** SER videos are easy to read in JS (item 9 of the Pi write-up); MP4 needs
  WebCodecs plus a demuxer, or `<video>` frame grabbing.

## Smaller follow-ups

- Per-user storage limits: stacks (up to 2 GB masters) and uploads have none; the server
  only refuses a stack when free disk would drop under 2 GB.
- `captured_at` from camera EXIF is camera-local time but treated as UTC; uploads from the
  Pi are UTC. Night grouping can be off by the time-zone offset for old, legacy files.
- The interface could switch focus mode to the JPEG live-view options once Pis advertise
  `liveview_jpeg_options`.
- `templates/routes.txt` and the wiki (`docs/wiki/Key-Endpoints.md`) don't list the new
  endpoints yet.

## The Python reference (`app/processing/`, numpy): what the JS engine ports

| File | What it does |
|---|---|
| `fitsio.py` | Writes and reads 32-bit float FITS files with 1 or 3 planes. Rows are stored bottom-up with `ROWORDER`, and reading uses a memory map. |
| `frames.py` | Reads RAW through LibRaw (`rawpy`). It returns the black-subtracted Bayer mosaic scaled 0..1, the 2×2 CFA pattern, the daylight and as-shot white balance, and a camera→sRGB matrix computed dcraw-style from `rgb_xyz_matrix`. It also reads JPEGs (an sRGB→linear lookup table, plus a fast luma-only reduced decode), debayers (super-pixel at half size, or bilinear by normalised convolution, a strip at a time), and applies colour. |
| `stars.py` | Detection and measurement: background grid, noise from the MAD, peak finding, then **adaptive Gaussian moments** for FWHM and eccentricity, HFR in a 3σ aperture, and per-frame summaries. |
| `register.py` | Triangle matching with RANSAC and a least-squares refinement (similarity or affine). Also transform helpers, conversion of matrices to scipy's (row, col) order, and phase correlation with sub-pixel peaks. |
| `integrate.py` | Combines aligned frames with average, median, sigma clipping or winsorized sigma clipping. It sorts each strip once, rejects a contiguous range, then takes a weighted mean. Also strip-size budgeting and normalisation. |
| `stack.py` | The full pipeline: master bias, dark, dark flat and flat; calibration on the mosaic; hot-pixel and cosmetic fixes; a measuring pass; reference choice; quality rejection with reasons; alignment; a warp pass to a float16 disk stack; a strip-wise combine; then the outputs (`master.fits`, `preview.npy`, `auto.jpg`, `thumb.jpg`, `coverage.npy`) and a result report. |
| `planetary.py` | Lucky imaging from a video (decoded through ffmpeg) or a burst: score each frame's sharpness by Laplacian variance, keep the best N%, align by phase correlation, and average. The result is display-referred, not linear. |
| `develop.py` | The editor's processing: crop → background polynomial (subtract or divide) → neutral background and white balance (from the stars, the average, or manual) → stretch (auto screen-transfer, manual, arcsinh or none) → contrast → saturation → chroma noise reduction → luma wavelet noise reduction → wavelet sharpening. `resolve()` measures the image-dependent numbers on the preview; `apply()` reuses them at any size, so the preview and the export match. |
| `analysis.py` | Per-frame metrics, plus plain-language checks (tracking, focus, exposure, clipping) for the interface's "last test shot" card. |
| `render.py` | Full-size export to JPEG, PNG, 16-bit TIFF, or a linear FITS crop, developed a strip at a time. |
| `worker.py` | A separate process: `python -m app.processing.worker <job dir>`. It reads `spec.json` and writes `progress.json` and `result.json`. |

**Not written:** `jobs.py`, the server-side runner. It is probably not needed now that processing moves to the browser.

### Verified results (scripts in `scratch-backup/proc/`)

- **Star sizes:** the measured FWHM is within 2–6% of the truth for 2, 3, 5, 8 and 14 px stars, and elongated stars measure correctly (eccentricity 0.80 against a true 0.78). Round stars read an eccentricity of 0.09 at high SNR; faint stars read higher purely from noise, so summaries prefer stars with SNR ≥ 30.
- **Alignment:** error under 0.07 px, including a 180° meridian flip.
- **Synthetic RAW stack:** 8 lights plus a frame with a satellite trail and a blurred frame, 8 darks, 8 flats and 10 bias frames.
  - All 60 planted hot pixels were removed.
  - Vignetting was flattened.
  - Noise was 0.0010, against 0.0012 for √N from a single frame.
  - Stacked FWHM was 3.15 px, against 2.94 px in a single frame.
  - The blurred frame was left out with the reason given.
  - The trail frame was kept, and sigma clipping removed the trail completely.
- **Full size (5634×3752 mosaic) through the real worker:**
  - 6 frames took 185 s.
  - After the fixes, the worker's private memory peaked at 748 MB against the 900 MB budget, at the per-frame warp.
  - Before the fixes, it reached 2.4 GB.

### Lessons that carry over to a JS port

- **Crowded fields:** a large measuring window lets neighbouring stars pull the adaptive moments into a runaway. The fix:
  - cap the Gaussian window at (r/2.5)²;
  - clamp centroid drift to ±1.5 px;
  - start with r = 6 and double the window only while the median star fills it (`detect_stars`). That's how defocused stars still measure correctly.
- **Satellite trails:** trails appear as strings of very bright, stretched "stars" (eccentricity > 0.85). Drop them from the summary statistics when they're under half of all detections. A frame with genuinely bad tracking has nearly every star stretched, so it still gets flagged.
- **Combine memory:** the per-pixel index and threshold maps (about 160 bytes per pixel, whatever the frame count) dominate when there are few frames. Size strips with `width × (N×24 + 160)` bytes per row.
- **Memory maps:** pages of a memory-mapped file count toward a process's resident memory. Use plain file reads and writes for big temporary stacks. In the browser, the equivalent is OPFS (Origin Private File System) with sync access handles in a worker.
- **Flat level:** dividing by a normalised flat preserves the median level, not the absolute one. That's expected, not a bug.
- **float16 storage** is accurate enough for calibrated frames, since its quantisation sits far below the shot noise.
- **Auto stretch:** the midtones value that maps x to t is `mtf(t, x)`, where `mtf(m,x) = (m−1)x / ((2m−1)x − m)`. Black point = median + (−2.8)·MAD; the target sky level is 0.20.

## Rerunning the Python tests

```bash
S=<scratch dir>   # any folder
venv/bin/pip install --target $S/pylib rawpy scipy tifffile
PYTHONPATH=$S/pylib venv/bin/python docs/handoff/2026-10-18-interface-album/scratch-backup/proc/t_core.py docs/handoff/2026-10-18-interface-album/scratch-backup/proc
PYTHONPATH=$S/pylib venv/bin/python docs/handoff/2026-10-18-interface-album/scratch-backup/proc/t_stack.py $S/out
NL=3 PYTHONPATH=$S/pylib venv/bin/python docs/handoff/2026-10-18-interface-album/scratch-backup/proc/t_bigmem.py $S/big $S/pylib   # full size, about 2 minutes
```

`t_core.py` runs in seconds and `t_stack.py` in about 30 s. The tests from the previous session (`test_commands.py`, `test_uploads_plans.py`, `test_liveview.py`) and `harness.py` are in `scratch-backup/`, with their last outputs in the matching `.out` files.

## Images in `images/`

All the images are synthetic test data or screenshots; none are user photos. Nothing was rendered from the user's photos.

| File | What it is |
|---|---|
| `old-interface-e2e-screenshot.png` | The current (pre-redesign) interface page in headless Chrome during last session's end-to-end test: real H.264 live view from a fake telescope, with the plan builder. |
| `synthetic-single-jpeg-frame.jpg` | One synthetic light frame (stars, a faint nebula, a sky gradient and noise), for comparison. |
| `synthetic-raw-stack-auto.jpg` | The engine's auto-developed result from the synthetic RAW stack: 8 lights, calibrated and aligned, with gradient removal and auto stretch. |
| `synthetic-raw-stack-trail-frame-kept.jpg` | The same stack with the satellite-trail frame left in, showing that sigma clipping removes the trail. |
| `synthetic-jpeg-stack-auto.jpg` | A stack of six synthetic JPEGs. |
| `synthetic-fullsize-21mp-stack-auto.jpg` | The preview from the full-size 21 MP worker test. It shows the engine copes with full size; the image itself is a plain synthetic star field. |

## Scratch backup (`scratch-backup/`)

- `harness.py`, `test_*.py` and `.out`: the first session's harness and tests (now copied into `tests/`, which is the copy to use).
- `e2e*.py` and `e2e*.js`, `chrome.sh` and `web/probe.html`: the end-to-end browser tests.
- `*_block*.py`, `*_block*.js` and `interface_*.py`: code fragments used while rewriting `interface.py` and `interface.js` last session, kept for reference only.
- `proc/`: the synthetic data generator (`synth.py`) and the processing tests.
- `mockups/project/`: the source of the mockup canvas.

Not backed up, because they can be regenerated: `pylib/` (pip-installed `rawpy`, `scipy` and `tifffile`, 148 MB) and the synthetic frames in `proc/out` and `proc/big` (1.3 GB).
