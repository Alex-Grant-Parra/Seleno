"""Lucky imaging for the Moon and planets.

Turbulence blurs most frames of a planetary video; a few are sharp. Every frame is
scored for sharpness around the planet (or across the Moon), the best few percent are
lined up to a fraction of a pixel with phase correlation, and averaged. The average
is smooth enough to sharpen hard afterwards (the editor's wavelet layers).

Works on a video (decoded with ffmpeg) or a burst of photos. Results are
display-referred (like the video itself), not linear.
"""
import json
import os
import subprocess

import numpy as np
from scipy import ndimage

from . import develop, frames, register, stack, stars

DEFAULT_SETTINGS = {
    "keep_percent": 25.0,      # best share of frames to stack
    "min_frames": 5,
    "max_frames": 4000,        # stop reading a long video after this many frames
    "roi": "auto",             # auto (around the planet) | full
    "sigma_reject": False,     # sigma-clip the kept frames (needs memory: capped)
}


class FrameSource:
    """Frames as float32 RGB 0..1, display-referred: from a video, or from photos."""

    def __init__(self, entries, max_frames):
        self.entries = entries
        self.video = len(entries) == 1 and frames.kind_of(entries[0]["path"]) == "video"
        self.max_frames = max_frames
        if self.video:
            self.width, self.height, self.count = _probe(entries[0]["path"])
            if self.count:
                self.count = min(self.count, max_frames)
        else:
            self.count = min(len(entries), max_frames)

    def __iter__(self):
        if self.video:
            yield from _video_frames(self.entries[0]["path"], self.width, self.height, self.max_frames)
            return
        for entry in self.entries[:self.max_frames]:
            yield _image_frame(entry["path"])

    def get(self, index):
        if not self.video:
            return _image_frame(self.entries[index]["path"])
        for i, frame in enumerate(self):
            if i == index:
                return frame
        raise stack.StackError("Couldn't read the reference frame back from the video")


def _probe(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                              "-show_entries", "stream=width,height,nb_read_packets", "-of", "json", path],
                             capture_output=True, text=True, timeout=120, check=True).stdout
        stream = json.loads(out)["streams"][0]
        return int(stream["width"]), int(stream["height"]), int(stream.get("nb_read_packets") or 0)
    except FileNotFoundError:
        raise stack.StackError("ffmpeg isn't installed on the server, so videos can't be read")
    except Exception as e:
        raise stack.StackError(f"Couldn't read the video: {e}")


def _video_frames(path, width, height, max_frames):
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size = width * height * 3
    try:
        for _ in range(max_frames):
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield np.frombuffer(buf, dtype=np.uint8).reshape(height, width, 3).astype(np.float32) / 255.0
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()


def _image_frame(path):
    kind = frames.kind_of(path)
    if kind == "jpeg":
        return frames.read_jpeg(path, linear=False)
    if kind == "raw":
        rgb = frames.read_frame_rgb(path, debayer_method="bilinear")
        peak = float(np.percentile(rgb[::4, ::4], 99.9)) or 1.0
        return frames.linear_to_srgb(np.clip(rgb / max(peak, 1e-6), 0, 1))
    raise stack.StackError(f"{os.path.basename(path)} can't be used for lucky imaging")


def _analyse(rgb):
    """(quality, cx, cy, extent_px, brightness) for one frame."""
    lum = rgb @ frames.LUMA
    small = lum if max(lum.shape) <= 1400 else lum[::2, ::2]
    scale = lum.shape[0] / small.shape[0]
    bg = float(np.percentile(small, 10))
    top = float(np.percentile(small, 99.8))
    if top - bg < 0.02:
        return 0.0, lum.shape[1] / 2, lum.shape[0] / 2, max(lum.shape), 0.0
    mask = small > bg + 0.4 * (top - bg)
    ys, xs = np.nonzero(mask)
    weights = small[ys, xs] - bg
    cy = float((ys * weights).sum() / weights.sum()) * scale
    cx = float((xs * weights).sum() / weights.sum()) * scale
    extent = max(np.percentile(ys, 99) - np.percentile(ys, 1), np.percentile(xs, 99) - np.percentile(xs, 1)) * scale
    half = int(max(extent * 0.6, 24) / scale)
    y0, x0 = int(cy / scale) - half, int(cx / scale) - half
    roi = small[max(0, y0):y0 + 2 * half, max(0, x0):x0 + 2 * half]
    smooth = ndimage.gaussian_filter(roi, 1.0)
    lap = ndimage.laplace(smooth)
    brightness = float(roi.mean())
    quality = float(lap.var() / max(brightness, 1e-3) ** 2)
    return quality, cx, cy, float(extent), brightness


def _crop(rgb, cx, cy, h, w):
    H, W = rgb.shape[:2]
    y0 = int(round(cy - h / 2))
    x0 = int(round(cx - w / 2))
    y0 = min(max(y0, 0), max(H - h, 0))
    x0 = min(max(x0, 0), max(W - w, 0))
    return rgb[y0:y0 + h, x0:x0 + w]


def run(spec, progress):
    settings = dict(DEFAULT_SETTINGS)
    settings.update({k: v for k, v in (spec.get("settings") or {}).items() if k in DEFAULT_SETTINGS})
    keep_percent = min(max(float(settings["keep_percent"]), 1.0), 100.0)
    max_frames = int(min(max(int(settings["max_frames"]), 10), 20000))
    entries = list((spec.get("frames") or {}).get("light") or [])
    if not entries:
        raise stack.StackError("Nothing to stack")
    out_dir = spec["output_dir"]
    os.makedirs(out_dir, exist_ok=True)
    source = FrameSource(entries, max_frames)

    # Pass 1: score every frame
    progress.stage("measure", 0.45)
    scores = []
    shape = None
    for i, rgb in enumerate(source):
        if shape is None:
            shape = rgb.shape
        elif rgb.shape != shape:
            raise stack.StackError("The frames aren't all the same size")
        scores.append(_analyse(rgb))
        if i % 10 == 0:
            progress.sub(i / max(source.count or len(scores) + 50, 1), f"Scoring frames for sharpness ({i + 1})")
    if len(scores) < 2:
        raise stack.StackError("Need at least two frames for lucky imaging")
    q = np.array([s[0] for s in scores])
    bright = np.array([s[4] for s in scores])
    usable = bright >= 0.5 * np.median(bright)
    order = [i for i in np.argsort(-q) if usable[i]]
    keep = max(int(settings["min_frames"]), int(round(len(scores) * keep_percent / 100.0)))
    chosen = sorted(order[:min(keep, len(order))])
    ref_index = order[0]

    H, W = shape[:2]
    extent = float(np.median([scores[i][3] for i in chosen]))
    if settings["roi"] == "full" or extent > 0.7 * min(H, W):
        crop_h, crop_w = H, W
    else:
        side = int(min(max(extent * 1.6, 64), min(H, W)))
        side -= side % 2
        crop_h = crop_w = side

    progress.stage("reference", 0.05)
    progress.sub(0.0, "Reading the sharpest frame")
    ref_rgb = source.get(ref_index)
    ref_crop = _crop(ref_rgb, scores[ref_index][1], scores[ref_index][2], crop_h, crop_w)
    ref_lum = ref_crop @ frames.LUMA
    del ref_rgb

    # Pass 2: align and average the chosen frames
    progress.stage("stack", 0.45)
    chosen_set = set(chosen)
    total = np.zeros(ref_crop.shape, dtype=np.float64)
    weight = np.zeros(ref_crop.shape[:2], dtype=np.float64)
    shifts = []
    used = 0
    for i, rgb in enumerate(source):
        if i not in chosen_set:
            continue
        crop = _crop(rgb, scores[i][1], scores[i][2], crop_h, crop_w)
        if crop.shape != ref_crop.shape:
            continue
        dy, dx, strength = register.phase_correlate(ref_lum, crop @ frames.LUMA)
        if abs(dy) > crop_h / 4 or abs(dx) > crop_w / 4:
            continue
        shifted = np.stack([ndimage.shift(crop[..., c], (-dy, -dx), order=3, mode="constant", cval=np.nan)
                            for c in range(3)], axis=-1)
        valid = np.all(np.isfinite(shifted), axis=-1)
        total[valid] += shifted[valid]
        weight += valid
        shifts.append([round(dx, 2), round(dy, 2)])
        used += 1
        progress.sub(used / len(chosen), f"Stacking the best frames ({used} of {len(chosen)})")
    if used == 0:
        raise stack.StackError("None of the chosen frames could be aligned")
    result = (total / np.maximum(weight, 1)[..., None]).astype(np.float32)
    result_chw = np.ascontiguousarray(np.moveaxis(result, 2, 0))

    progress.stage("output", 0.05)
    progress.sub(0.0, "Writing the result")
    header = {"IMAGETYP": "Lucky imaging stack", "NCOMBINE": used, "CREATOR": "Seleno",
              "OBJECT": spec.get("target") or None}
    auto = stack.write_outputs(result_chw, None, out_dir, header, None, planetary=True)
    return {
        "kind": "planetary",
        "width": int(result.shape[1]),
        "height": int(result.shape[0]),
        "frames_used": used,
        "frames_total": len(scores),
        "keep_percent": keep_percent,
        "reference": f"frame {ref_index + 1}",
        "quality_range": [float(q.min()), float(np.median(q)), float(q.max())],
        "frames": [{"name": entries[0]["name"] if source.video else entries[i]["name"], "frame": i,
                    "quality": round(float(q[i]), 4), "accepted": i in chosen_set} for i in range(len(scores))][:5000],
        "auto_params": auto,
        "settings": settings,
        "warnings": [],
        "linear": False,
    }
