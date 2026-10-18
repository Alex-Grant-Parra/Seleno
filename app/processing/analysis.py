"""Measuring single frames: star count and size, roundness, sky level, noise and
clipping. The album shows these per frame and the interface turns them into plain
checks on the last test shot.

A RAW is measured from its mosaic (linear, the camera's real data). A JPEG is measured
at half size from its luminance; its numbers are close but the camera's sharpening
and noise reduction nudge them.
"""
import os

import numpy as np

from . import frames, stars


def measure_file(path, raw_path=None):
    """Metrics for one frame. raw_path, when given, is measured instead of path."""
    use = raw_path if raw_path and os.path.isfile(raw_path) else path
    kind = frames.kind_of(use)
    if kind == "raw":
        mosaic = frames.read_raw(use)
        lum = frames.mosaic_luminance(mosaic.data)
        full_h, full_w = mosaic.data.shape
        sat = 0.97
        del mosaic
    elif kind == "jpeg":
        lum = frames.read_jpeg_luminance(use, reduce=2, linear=True)
        from PIL import Image
        with Image.open(use) as img:
            full_w, full_h = img.size
        sat = 0.97
    else:
        raise ValueError("Only photos can be measured")
    st = stars.detect_stars(lum, k=5.0, max_stars=1000, saturation=sat)
    summary = stars.summarise(st, pixel_scale=full_w / lum.shape[1])
    sample = lum[::3, ::3]
    summary.update(
        source=kind,
        width=int(full_w),
        height=int(full_h),
        clipped_pct=round(100.0 * float((sample >= sat).mean()), 4),
        background_display=float(frames.linear_to_srgb(np.float32(summary["background"]))),
        background_pct=round(100.0 * summary["background"], 3),
        noise_pct=round(100.0 * summary["noise"], 4),
    )
    for key in ("fwhm", "hfr", "ecc", "snr"):
        if summary[key] is not None:
            summary[key] = round(float(summary[key]), 3)
    summary["background"] = round(float(summary["background"]), 6)
    summary["noise"] = round(float(summary["noise"]), 7)
    summary["background_display"] = round(summary["background_display"], 4)
    return summary


def checks(metrics, exposure_s=None, pixel_scale_arcsec=None):
    """Plain-language verdicts on a frame: [{key, level: ok|warn|bad, title, detail}]."""
    out = []
    if not metrics:
        return out
    n = metrics.get("stars") or 0
    ecc = metrics.get("ecc")
    fwhm = metrics.get("fwhm")
    bg = metrics.get("background_display")
    clipped = metrics.get("clipped_pct") or 0

    if n < 5:
        out.append({"key": "stars", "level": "bad", "title": "Hardly any stars",
                    "detail": "Clouds, the cover still on, badly out of focus, or a target with no stars (the Moon or a planet)."})
        return out
    if ecc is not None:
        if ecc < 0.45:
            out.append({"key": "tracking", "level": "ok", "title": "Stars are round",
                        "detail": f"Tracking is steady (eccentricity {ecc:.2f})."})
        elif ecc < 0.6:
            out.append({"key": "tracking", "level": "warn", "title": "Stars are slightly stretched",
                        "detail": f"Eccentricity {ecc:.2f}. A shorter exposure, a better polar alignment or less wind helps."})
        else:
            out.append({"key": "tracking", "level": "bad", "title": "Stars are trailing",
                        "detail": f"Eccentricity {ecc:.2f}. Check the mount is tracking and polar aligned, or shorten the exposure."})
    if fwhm is not None:
        size = f"{fwhm:.1f} px"
        if pixel_scale_arcsec:
            size += f" ({fwhm * pixel_scale_arcsec:.1f}″)"
        if fwhm <= 4.0:
            out.append({"key": "focus", "level": "ok", "title": "In focus", "detail": f"Stars are {size} across."})
        elif fwhm <= 6.5:
            out.append({"key": "focus", "level": "warn", "title": "Focus could be sharper",
                        "detail": f"Stars are {size} across. Seeing may be poor, or refocus with the focus helper."})
        else:
            out.append({"key": "focus", "level": "bad", "title": "Out of focus",
                        "detail": f"Stars are {size} across. Refocus before capturing."})
    if bg is not None:
        pct = round(bg * 100)
        if bg < 0.08:
            out.append({"key": "exposure", "level": "warn", "title": "Underexposed",
                        "detail": f"The sky sits at {pct}% of the histogram. Expose longer or raise the ISO so faint detail clears the noise."})
        elif bg > 0.55:
            out.append({"key": "exposure", "level": "bad", "title": "Overexposed",
                        "detail": f"The sky sits at {pct}%. Shorten the exposure or lower the ISO; light pollution or twilight may be washing it out."})
        else:
            out.append({"key": "exposure", "level": "ok", "title": "Exposure is right",
                        "detail": f"The sky sits at {pct}% of the histogram."})
    sat_stars = metrics.get("saturated_stars") or 0
    if clipped > 0.5:
        out.append({"key": "clipping", "level": "bad", "title": "Highlights are clipped",
                    "detail": f"{clipped:.2f}% of the picture is pure white. Shorten the exposure."})
    elif sat_stars > 0.1 * n:
        out.append({"key": "clipping", "level": "warn", "title": "Many bright stars are clipped",
                    "detail": "Normal for long exposures; shorten it if star colour matters."})
    else:
        out.append({"key": "clipping", "level": "ok", "title": "Nothing important is clipped",
                    "detail": f"{clipped:.2f}% of pixels at full brightness."})
    return out


def run(spec, progress):
    """A batch job: measure each frame in spec['frames']."""
    entries = spec.get("frames", {}).get("all") or []
    results = {}
    for i, entry in enumerate(entries):
        progress.sub(i / max(len(entries), 1), f"Measuring {entry['name']} ({i + 1} of {len(entries)})")
        try:
            results[entry["name"]] = measure_file(entry["path"], entry.get("raw_path"))
        except Exception as e:
            results[entry["name"]] = {"error": str(e)}
    return {"kind": "analyse", "metrics": results}
