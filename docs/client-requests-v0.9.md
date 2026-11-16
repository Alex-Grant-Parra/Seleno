# Telescope client v0.9: what the server and web pages want from the Pi

From the server side, for whoever works on the telescope client (the Pi). Each item
says what to do, the exact format, why the web pages need it, and whether the server
already accepts it. Everything here is optional for the client: the server works with
v0.8 clients and uses each feature once the client offers it.

## How the server knows what a client can do

**New: send a `capabilities` list in every status message.** The server only sends a new
argument (like `frame_type` on `capturePhoto`) to clients that list its capability, so a
v0.8 Pi never gets an argument it doesn't understand.

```json
{"type": "status", "capabilities": ["capture_frame_type", "upload_pending_filter", "liveview_jpeg_options"], ...}
```

Strings only, up to 64 of them. *Server: ready.*

| Capability | Means the client supports |
|---|---|
| `capture_frame_type` | `capturePhoto(..., frame_type=...)`, item 3 |
| `upload_pending_filter` | `uploadPending(kind, files=..., pairs=...)`, item 5 |
| `liveview_jpeg_options` | `setLiveViewOptions(jpeg_quality=..., max_fps=...)`, item 4 |

## Priority 1: the interface's guided steps need these

### 1. Status: where the telescope is and what the camera is doing

Add two new parts to the status message, and two fields to `camera`:

```json
{
  "type": "status",
  "site":  {"lat": 51.4769, "lon": -0.0005, "elevation_m": 46},
  "solve": {"time": "2026-10-18T21:13:02Z", "ra": 83.81, "dec": -5.40, "pixel_scale": 1.98,
            "rotation_deg": 91.4, "field_w_deg": 3.08, "field_h_deg": 2.05, "offset_arcmin": 0.4},
  "camera": {"connected": true, "model": "Canon EOS 5D Mark II", "battery": 74,
             "temperature_c": 18.5, "card_free_bytes": 22000000000}
}
```

- `site`: decimal degrees, east positive. **Why:** the interface works out twilight, the
  Moon, each target's altitude and its hours above 30° from the telescope's site. Today
  it only knows the site if the owner typed it in, or from upload metadata. The server
  rounds it to about a kilometre before showing it to anyone but the owner.
- `solve`: the last successful plate solve, all angles in degrees. `offset_arcmin` is how
  far the solved centre was from the target. **Why:** shown under the mount controls, and
  the pixel scale turns star sizes into arcseconds.
- `camera.temperature_c`: the sensor (or camera) temperature if gphoto2 reports it.
  **Why:** darks only match lights at a similar temperature; the album will warn when they
  don't.
- `camera.battery` as a number (percent) and `camera.card_free_bytes`.

*Server: ready* (both parts are kept and the interface already reads `site`, `solve`,
`temperature_c` and `battery`).

### 2. Metadata for every uploaded frame

The tus upload's `meta` JSON already carries `frame_type`, `label`, `exposure_s`, `iso`,
`target`, `ra`, `dec`, `site`, `plan`, `step`, `frame` and `pair`. Please add:

```json
{
  "camera": "Canon EOS 5D Mark II",
  "temperature_c": 18.5,
  "focal_mm": 650,
  "solve": {"ra": 83.81, "dec": -5.40, "pixel_scale": 1.98, "rotation_deg": 91.4,
            "field_w_deg": 3.08, "field_h_deg": 2.05}
}
```

- `solve` only for frames that were plate solved (the centring frame, say); leave it out
  otherwise.
- Keep `captured` (in the upload's own metadata) in **UTC, ISO 8601, with milliseconds**:
  `2026-10-18T21:09:12.345Z`.
- `ra` and `dec` are where the mount pointed, J2000 degrees.

**Why:** the album groups frames by night and target, matches darks to lights by exposure,
ISO and temperature, and the stacker uses the pixel scale and pointing.

*Server: ready.* The whole `meta` object is now stored with the photo (`photos.meta_json`,
up to 16 KB), so nothing the client sends is lost.

### 3. Test shots

The interface's new **Test shot** button takes one photo and, within seconds, shows plain
checks on it (are the stars round, is it in focus, is the exposure right).

- Accept a `frame_type` keyword on `capturePhoto`: `capturePhoto(owner_id, exposure_s, frame_type="test")`.
  Values the server sends: `test`, `light`, `snapshot`. Tag the upload's `meta.frame_type`
  with it.
- **Upload a test shot's JPEG straight away, ahead of anything queued** (RAWs, other
  JPEGs). Its RAW can follow in the normal queue (or wait to be asked for, under
  `jpeg_first`).
- Advertise `capture_frame_type`.

*Server: ready.* `frame_type` is sent only to clients with the capability; the server
accepts `test` as a frame type.

### 4. Sharper focus frames in live view

Focusing measures the stars' half-flux radius in the live view picture. H.264 smears
small stars, so at 5× and 10× zoom a JPEG stream is more accurate even at a low frame rate.

- Accept two more options on `setLiveViewOptions`: `jpeg_quality` (50–98) and `max_fps`
  (0.2–30). With `codec: "jpeg"`, `jpeg_quality: 92`, `max_fps: 2`, send the zoomed crop
  as high-quality JPEG frames about twice a second.
- Advertise `liveview_jpeg_options`.

*Server: ready* (the `set_liveview_options` command validates and passes both). The
interface doesn't switch to it automatically yet; it will once clients advertise it.

### 5. Ask for some RAWs, not all

Under `upload: "jpeg_first"`, RAWs wait on the Pi until asked for. The album's
**Ask the telescope for RAWs** button (and the stack dialog) asks for the RAWs of just the
frames the user wants to stack:

```
uploadPending("raw", files=["IMG_0412.JPG", "IMG_0413.JPG"], pairs=["p412", "p413"])
```

- `files`: the uploaded JPEG names; `pairs`: their `pair` ids (match on either).
- Queue those RAWs first and leave the other held RAWs where they are.
- Reply `{"queued": 2, "missing": []}`, listing in `missing` any it no longer has.
- Advertise `upload_pending_filter`.

*Server: sends this already* when a user asks for specific frames. Until the client
supports the keywords the call fails, and the album tells the user to send all held RAWs
from the interface instead.

## Priority 2: better data for stacking

### 6. Flats with an automatic exposure

The plan schema already accepts `{"type": "flat", "count": 20, "exposure_s": "auto"}`.
For `auto`: take short test exposures and adjust until the RAW's histogram peak (or the
JPEG's, when shooting JPEG only) sits at about **50% of full scale**, then take the flats
at that exposure. Put the chosen exposure in each flat's `meta.exposure_s`. If the light
changes (twilight flats), adjust between frames so each stays between 35% and 65%.

### 7. Settle after dithering

Light steps can carry `dither_every` and `dither_arcmin`. Add `dither_settle_s` (0–120 s,
default 5): after each dither, wait that long (or until the mount reports it's settled)
before the next exposure. Trailed frames right after a dither are the commonest reason the
stacker leaves frames out. *Server: the schema accepts `dither_settle_s`.*

### 8. Centring waits for the slew

When `centreTarget` arrives while the mount is still slewing (the interface's
"Go to + centre" sends both close together), wait for the slew to finish before taking
the solve frame, rather than failing.

## Priority 3: planets and the Moon

### 9. Planetary video worth stacking

Lucky imaging keeps the sharpest few percent of a planet video's frames. The compressed
live-view recording throws away the fine detail it needs.

- For `video` steps with `"quality": "lossless"`, record the **zoomed live-view crop**
  (the planet, a few hundred pixels across) without lossy compression, as
  **SER** (8-bit, or 16-bit if available): the format planetary software uses, simple to
  read in the browser.
- Use file extension `.ser`. *Server: accepts `.ser` uploads and lists them as videos.*
- Put `{"crop": {"x": 0.41, "y": 0.33, "width": 640, "height": 480}, "fps": 30}` in the
  upload's `meta`, so the album knows what it's looking at.

## Not needed now

### 10. Linear previews alongside RAWs

The handoff floated sending a small linear preview (16-bit PNG or float FITS) with each
RAW, so browsers wouldn't have to decode RAW. **Not needed for now**: the album will decode
Canon CR2 files in the browser itself. It may come back for cameras whose RAW the browser
can't read (CR3, NEF and others). If so, it'll be specified then.

## Checklist

| # | What | Client | Server |
|---|---|---|---|
| 0 | `capabilities` in status | to do | ready |
| 1 | `site`, `solve`, camera temperature and battery in status | to do | ready |
| 2 | `camera`, `temperature_c`, `focal_mm`, `solve` in upload meta; UTC times with ms | to do | ready (stored) |
| 3 | `capturePhoto(frame_type=...)`, test shots uploaded first | to do | ready |
| 4 | `setLiveViewOptions(jpeg_quality, max_fps)` | to do | ready |
| 5 | `uploadPending(kind, files, pairs)` | to do | ready (already sent) |
| 6 | `flat.exposure_s: "auto"` | to do | ready |
| 7 | `dither_settle_s` | to do | ready |
| 8 | `centreTarget` waits for the slew | to do | nothing needed |
| 9 | Lossless `.ser` planetary video | to do | ready |
| 10 | Linear previews | not now | — |
