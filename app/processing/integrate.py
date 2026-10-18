"""Combining a stack of aligned frames into one, pixel by pixel.

Frames are given as an (N, ...) float32 array with NaN where a frame has no data
(outside its field after alignment). One sort per strip gives medians and robust
spreads cheaply; rejection then keeps a contiguous range of each pixel's sorted
values, and the result is a (weighted) mean of what's left.

Methods:
  average     plain mean (no rejection)
  median      median
  sigma       kappa-sigma clipping around the median
  winsorized  winsorized sigma clipping (steadier with few frames or many outliers)
"""
import numpy as np

METHODS = ("average", "median", "sigma", "winsorized")


def _take(sorted_data, index):
    return np.take_along_axis(sorted_data, index[None], axis=0)[0]


def _median_sorted(sorted_data, lo, hi):
    a = _take(sorted_data, (lo + hi - 1) // 2)
    b = _take(sorted_data, (lo + hi) // 2)
    return 0.5 * (a + b)


def combine(stack, method="sigma", kappa_low=3.0, kappa_high=3.0, iterations=3, weights=None):
    """Returns (result, coverage, rejected_low, rejected_high) for an (N, ...) stack.
    coverage counts the frames with data at each pixel."""
    stack = np.asarray(stack, dtype=np.float32)
    n = stack.shape[0]
    valid = np.isfinite(stack)
    coverage = valid.sum(axis=0).astype(np.int32)
    shape = stack.shape[1:]
    zeros = np.zeros(shape, dtype=np.int32)

    if method not in METHODS:
        raise ValueError(f"Unknown integration method: {method}")
    if method == "average" or n < 3 and method != "median":
        return _weighted_mean(stack, valid, weights), coverage, zeros, zeros.copy()

    srt = np.sort(stack, axis=0)        # NaN sort last
    lo = np.zeros(shape, dtype=np.int32)
    hi = coverage.astype(np.int32)
    empty = hi == 0
    hi_safe = np.where(empty, 1, hi)

    if method == "median":
        result = _median_sorted(srt, lo, hi_safe)
        result[empty] = np.nan
        return result.astype(np.float32), coverage, zeros, zeros.copy()

    low_thr = np.full(shape, -np.inf, dtype=np.float32)
    high_thr = np.full(shape, np.inf, dtype=np.float32)
    for _ in range(max(1, iterations)):
        count = hi - lo
        active = count >= 3
        if not active.any():
            break
        h_safe = np.where(hi > lo, hi, lo + 1)
        med = _median_sorted(srt, lo, h_safe)
        sigma = _spread(srt, valid_range=(lo, hi), med=med, method=method)
        new_low = np.where(active, med - kappa_low * sigma, low_thr).astype(np.float32)
        new_high = np.where(active, med + kappa_high * sigma, high_thr).astype(np.float32)
        new_low = np.maximum(new_low, low_thr)
        new_high = np.minimum(new_high, high_thr)
        # Rejection keeps a contiguous run of the sorted values
        below = (srt < new_low[None]) & np.isfinite(srt)
        within = (srt <= new_high[None]) & np.isfinite(srt)
        new_lo = below.sum(axis=0, dtype=np.int32)
        new_hi = within.sum(axis=0, dtype=np.int32)
        del below, within
        # Never reject everything: keep at least the middle value
        collapse = new_hi <= new_lo
        new_lo = np.where(collapse, lo, new_lo)
        new_hi = np.where(collapse, hi, new_hi)
        changed = (new_lo != lo) | (new_hi != hi)
        lo, hi = new_lo, new_hi
        low_thr, high_thr = new_low, new_high
        if not changed.any():
            break
    del srt

    keep = valid & (stack >= low_thr[None]) & (stack <= high_thr[None])
    result = _weighted_mean(stack, keep, weights)
    rej_low = (valid & (stack < low_thr[None])).sum(axis=0).astype(np.int32)
    rej_high = (valid & (stack > high_thr[None])).sum(axis=0).astype(np.int32)
    return result, coverage, rej_low, rej_high


def _spread(srt, valid_range, med, method):
    lo, hi = valid_range
    count = np.maximum(hi - lo, 1)
    # Robust sigma from the 15.9 and 84.1 percentiles of the kept values
    q1 = lo + np.floor(0.159 * (count - 1)).astype(np.int32)
    q2 = lo + np.ceil(0.841 * (count - 1)).astype(np.int32)
    sigma = 0.5 * (_take(srt, q2) - _take(srt, q1))
    if method == "winsorized":
        # Winsorize at 1.5 sigma and take the standard deviation, scaled back up
        a = (med - 1.5 * sigma)[None]
        b = (med + 1.5 * sigma)[None]
        idx = np.arange(srt.shape[0]).reshape((-1,) + (1,) * (srt.ndim - 1))
        inside = (idx >= lo[None]) & (idx < hi[None])
        wv = np.clip(srt, a, b)
        wv = np.where(inside, wv, 0.0)
        mean = wv.sum(axis=0) / count
        var = (np.where(inside, (wv - mean[None]) ** 2, 0.0)).sum(axis=0) / np.maximum(count - 1, 1)
        sigma = 1.134 * np.sqrt(var)
    return np.maximum(sigma, 1e-7).astype(np.float32)


def _weighted_mean(stack, keep, weights):
    if weights is None:
        total = np.where(keep, stack, 0.0).sum(axis=0, dtype=np.float64)
        count = keep.sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = total / count
    else:
        w = np.asarray(weights, dtype=np.float64).reshape((-1,) + (1,) * (stack.ndim - 1))
        total = (np.where(keep, stack, 0.0) * w).sum(axis=0)
        wsum = (keep * w).sum(axis=0)
        with np.errstate(invalid="ignore", divide="ignore"):
            out = total / wsum
    return out.astype(np.float32)


def strip_rows_for(n_frames, width, budget_bytes, share=0.5):
    """How many rows of an (N, rows, width) float32 strip fit in `share` of the memory
    budget. Per frame and pixel: the strip, its sorted copy, the masks and clipped copies
    (about 24 bytes); per pixel, whatever the frame count: the index and threshold maps
    and the temporaries of the median and spread (about 160 bytes)."""
    per_row = width * (n_frames * 24 + 160)
    return int(max(8, min(4096, (budget_bytes * share) // max(per_row, 1))))


def normalise_strip(strip, loc, scale, ref_loc, ref_scale):
    """Match each frame's background level and spread to the reference, in place:
    x' = (x - loc_i) * (ref_scale / scale_i) + ref_loc."""
    for i in range(strip.shape[0]):
        k = ref_scale / scale[i] if scale[i] > 0 else 1.0
        strip[i] -= loc[i]
        strip[i] *= k
        strip[i] += ref_loc
    return strip
