# Handoff: interface and album redesign (updated 18 Oct 2026, third session)

## Read this first

- **The in-browser processing engine is built** (`static/js/processing/`, third session)
  and wired into the album: measuring quality, stacking (with calibration, quality
  rejection and three combine modes), multi-night projects with resumable rebuilds,
  lucky imaging (videos, SER, photo bursts), and the live editor with full-size export.
  Tested in Node against the Python reference and end to end in headless Chrome. See
  "The engine" below.
- **Not committed.** The pages and APIs from the first two sessions are committed
  (`184e321`, `a2e5004`); everything from the third session is in the working tree.
- **On the live server's next restart:** one new table, `projects`. Nothing else
  migrates; no new Python packages. Album photos are now served with range support
  (`send_file(..., conditional=True)`), which SER reading needs.
- **Waiting on Alex:** item 9 of the engine brief (per-frame measurement on the Pi, sent
  in the upload metadata) is not in `docs/client-requests-v0.9.md`; Alex asked to be
  asked first.
- **Mockups:** <https://claude.ai/artifact/ECb8nkLLfTNahpRazeQ24c> (source in
  `scratch-backup/mockups/project/`).
- `HANDOFF.docx` in this folder is the first session's version; this file replaces it.

## What Alex asked for

> Completely redo the album and interface pages, with a focus on taking proper, accurate astrophotography photos. The interface should be about getting the data; the album about processing and viewing it, including stacking. Have a basic and an advanced mode, chosen with a toggle in the top right. Keep most of the existing features; feel free to add more. If anything should be added to the telescope nodes (the Pi clients), write it up in a file so it can be passed on, and be clear about what's wanted.

## The engine (third session)

Everything runs in the user's browser tab, in Web Workers; the server only stores and
serves. Closing the tab (or the computer sleeping) stops a job; the stack dialog, the
lucky imaging dialog and the Activity tab say so, and the page warns before closing
while something runs. `app/processing/` (Python) stays as the tested reference.

### Modules (`static/js/processing/`, ES modules)

| File | What |
|---|---|
| `cr2.js`, `canon-colordata.js` | Canon CR2 reader: TIFF IFDs, lossless JPEG (SOF3) with Canon's slices, the visible area from LibRaw's Canon size table (SensorInfo as a fallback), black from the masked columns, white and matrix from LibRaw's table, daylight WB from the matrix, as-shot WB from ColorData. Bit-exact with rawpy on the CC0 5D Mark II sample. |
| `decode.js` | File to frame: CR2 (others fall back to their JPEG), JPEG through `createImageBitmap` without colour conversion, sRGB to linear. |
| `image.js`, `stars.js`, `register.js`, `integrate.js`, `develop.js`, `calibrate.js`, `colour.js` | Ports of `frames.py`, `stars.py`, `register.py`, `integrate.py`, `develop.py` and stack.py's calibration, matching Python on the oracle data. |
| `warp.js`, `half.js`, `fits.js`, `tiff.js` | Keys bicubic (a = -0.5, weights from a 1/1024 px table) or bilinear warping, NaN outside; float16; FITS (bottom-up, strip-wise); 16-bit TIFF. |
| `frame-ops.js`, `tasks.js`, `worker.js`, `pool.js` | Per-frame steps; the tasks a worker runs; the worker; the pool (sized from `hardwareConcurrency` and `deviceMemory`, about 500 MB a worker at full size), plus an inline pool for Node. ArrayBuffers move by transfer. |
| `store.js`, `fetch-cache.js` | OPFS job folders (sync access handles in workers, `getFile().slice()` to read, stale folders cleaned after a day) or memory; RAWs kept in Cache Storage between re-stacks (8 GB cap, oldest out). |
| `combine.js` | The swappable combine stage: `memory` (aligned float16 frames in the tab), `disk` (in OPFS), `stream` (running weighted sums, values checked against rejection limits from a reference stack of the first 24 frames; fixed memory, split into row shards across workers). `chooseMode()` picks from frame count and size against memory and storage. `ResultSink` writes `master.fits` strip by strip, the preview, coverage and the common-area crop. |
| `stack.js` | The pipeline (`stack.run`): masters, measuring pass (calibrated frames cached in OPFS for the second pass when there's room), reference and quality rejection (plus `keep_best`), alignment, warp pass, combine, outputs and report. Also a night of a project (`ctx.project`). |
| `lucky.js`, `lucky-tasks.js`, `ser.js` | Lucky imaging in one pass: frames scored and cropped in workers, only the best crops kept, phase-correlated to the sharpest and averaged. Sources: `<video>` with `requestVideoFrameCallback` (seek stepping without it), SER by range requests, photo bursts. |
| `projects.js`, `client.js`, `session.js`, `develop-tasks.js` | Project totals to and from the server; the engine as the album calls it; the editor's session (live render, before/after/split, background model, export JPEG/PNG/TIFF at full size from the master by range requests). |

### Album wiring

`static/js/album/engine.js` exposes `measure`, `stack`, `rebuildProject` and `develop`.
**Measure quality** (bulk bar) and the stacker's own measurements post to
`/album/api/metrics`. The **stack dialog** gained "Add to a project" (Advanced), a quick
JPEG mode when every light has a JPEG, and half size by default in Basic on weak
machines. **Edit this photo** opens the editor on one frame. **Lucky imaging** appears
for videos (inspector) and photo bursts (bulk bar). Stacked images has a **Projects**
card (open the result, re-stack from scratch or carry on, abandon, delete).

### Projects (multi-night)

`models/album.py` `Project`, `controllers/projects_api.py`. The totals (`sum.fits`,
`wsum.fits`, `coverage.fits`, `limits.fits`: 13 float32 planes, about 1.1 GB at full size,
a quarter at half size) are uploaded in resumable chunks to `staging/`, then a commit
renames them into `current/` (a night) or `rebuild/` (a checkpoint), so a broken upload
never replaces good totals. The project keeps its reference stars, level and spread, so
every night lines up and normalises the same way; its alignment and combining settings
are fixed. It remembers every frame it has considered, so a frame left out once isn't
re-judged and added later. A rebuild goes a night at a time (with that night's
calibration) and checkpoints after each.

Endpoints: `GET/POST /album/api/projects`, `GET/PATCH/DELETE /album/api/projects/<id>`,
`PUT /album/api/projects/<id>/upload/<file>?offset=`, `POST .../commit` (mode `night`,
`checkpoint` or `rebuilt`), `DELETE .../rebuild`, `GET /album/projects/<id>/<current|rebuild>/<file>` (ranges).

### Decisions and departures from the Python reference

- **Alignment is stricter than `register.py`:** a match must account for at least 15%
  of the comparable stars, not just six. With six, frames of an unrelated dense field
  "aligned" by chance in testing (5 of 10). Worth porting back to Python if it stays.
- **Streaming limits are stored as float16**, so a value right on a limit can go either
  way: fewer than 0.1% of pixels differ from memory mode.
- **Phase correlation pads to powers of two** (Python transforms at the image size):
  the shifts agree to about 0.2 px on the test data.
- **Bicubic is Keys (a = -0.5)**, not scipy's cubic spline; **JPEG luminance** for
  measuring is Rec. 709 luma of linear values, not PIL's "L".
- WebGPU, multi-point alignment for lucky imaging, and `keep_masters` are not built.
  The combine stage and the warp are where a WebGPU backend would go.

### Verification

- Node (`tests/processing/`, about 10 s): 19 tests. Core maths against the Python oracle
  (stats, background, filters, star positions and sizes to 0.1%, registration including a
  meridian flip, every integration method with and without weights to 2e-6, debayer,
  colour matrix, develop); the pipeline on synthetic Bayer frames (all 30 hot pixels
  fixed, vignetting flattened, blurred frame left out with its reason, trail clipped,
  noise down with frame count, memory = disk bit for bit, stream within float16 of
  them); a two-night project equal to one stack (mean difference under 2e-5); lucky
  imaging choosing exactly the sharp frames.
- Python: 57 tests pass (`test_projects_api.py` new).
- Headless Chrome (`tests/e2e/run.py interface album engine`): all pass, no page or
  console errors. The engine scenario measures (FWHM 2.6-2.7 px against a true 2.7),
  stacks the fixture night through the dialog in about 5 s (10 of 12 frames, the two
  soft ones left out with reasons), opens the live editor, re-renders, exports, starts a
  project and checks a second run skips everything, lucky-images a SER video (keeps
  exactly the 15 sharp frames of 60), and reads the real CR2 (5634 x 3752).
- Fixed along the way: the interface scenario clicked the target picker's outer `div`
  since `a2e5004` (now the button), and the e2e star fields were a different sky in
  every frame (now one field, dithered).

### Speed and memory (measured on this VPS: one virtualised Haswell core)

Per 21 MP CR2 frame through the whole per-frame pipeline: 7.7 s at full size (decode
0.8, cosmetic 1.1, measure 1.6, debayer and colour 1.1, bicubic warp 3.2) with a 615 MB
peak in Node (about 570 MB of it the frame's data, a little over the 500 MB aim), and
4.6 s at half size (322 MB). Your Ryzen 5 7600 should be roughly two to three times
faster per core, so about 1 s a frame overall with 5 workers, in line with the brief's
estimates. That's an extrapolation: measure it there (the stack report has `stats.seconds`
and the combine mode).

### Next

1. Try a real night of CR2s on Alex's machine and note the timings, memory and any
   frames that fail; tune `workersFor()` and the strip sizes from that.
2. Ask Alex about item 9 (Pi-side measurement); add it to `docs/client-requests-v0.9.md` if yes.
3. WebGPU backend for warp and combine behind the same interface; multi-point alignment
   for lucky imaging.
4. Port the stricter match rule back to `register.py`.
5. List the new endpoints in `templates/routes.txt` and `docs/wiki/Key-Endpoints.md`.

## Decisions from the second session

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
