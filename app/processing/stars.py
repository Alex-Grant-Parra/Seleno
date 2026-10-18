"""Finding stars and measuring them: position, flux, HFR, FWHM and eccentricity.

Sizes are measured with adaptive Gaussian moments (a Gaussian window matched to each
star, corrected for the window), which hold up to noise far better than plain moments.
HFR is the flux-weighted mean distance from the centre, as focusing tools use it.
"""
import numpy as np
from scipy import ndimage

GAUSS_FWHM = 2.0 * np.sqrt(2.0 * np.log(2.0))   # 2.3548


def robust_stats(values):
    """(median, robust standard deviation from the MAD) of the finite values."""
    v = np.asarray(values, dtype=np.float32).ravel()
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0, 0.0
    med = float(np.median(v))
    return med, 1.4826 * float(np.median(np.abs(v - med)))


def _interp_matrix(n_out, n_grid, cell):
    pos = (np.arange(n_out) + 0.5) / cell - 0.5
    pos = np.clip(pos, 0, n_grid - 1)
    i0 = np.floor(pos).astype(int)
    i1 = np.minimum(i0 + 1, n_grid - 1)
    frac = (pos - i0).astype(np.float32)
    m = np.zeros((n_out, n_grid), dtype=np.float32)
    rows = np.arange(n_out)
    np.add.at(m, (rows, i0), 1 - frac)
    np.add.at(m, (rows, i1), frac)
    return m


def background_map(img, cell=64):
    """A smooth background: medians of cell x cell blocks, median-filtered so stars and
    small objects don't pull it up, then bilinearly interpolated to full size."""
    h, w = img.shape
    cell = int(max(8, min(cell, h // 2 or 1, w // 2 or 1)))
    ny, nx = max(1, h // cell), max(1, w // cell)
    step = 2 if cell >= 16 else 1
    blocks = img[:ny * cell, :nx * cell].reshape(ny, cell, nx, cell)[:, ::step, :, ::step]
    grid = np.median(blocks.transpose(0, 2, 1, 3).reshape(ny, nx, -1), axis=2).astype(np.float32)
    if ny >= 3 and nx >= 3:
        grid = ndimage.median_filter(grid, size=3, mode="nearest")
    ry = _interp_matrix(h, ny, cell)
    rx = _interp_matrix(w, nx, cell)
    return (ry @ grid @ rx.T).astype(np.float32)


def detect_stars(img, k=5.0, max_stars=1500, saturation=None, radius=None, min_fwhm=0.9,
                 background=None):
    """Stars in a monochrome float image. Returns a dict of arrays (x, y, flux, peak,
    snr, hfr, fwhm, ecc, saturated) brightest first, plus 'background' (median level)
    and 'noise' (robust sigma of the background)."""
    img = np.asarray(img, dtype=np.float32)
    h, w = img.shape
    bg = background_map(img) if background is None else background
    data = img - bg
    _, noise = robust_stats(data[::3, ::3])
    noise = max(noise, 1e-7)
    smooth = ndimage.gaussian_filter(data, 1.0)
    _, noise_s = robust_stats(smooth[::3, ::3])
    noise_s = max(noise_s, 1e-7)

    peaks = (smooth == ndimage.maximum_filter(smooth, size=5)) & (smooth > k * noise_s)
    ys, xs = np.nonzero(peaks)

    empty = {key: np.zeros(0, dtype=np.float32) for key in ("x", "y", "flux", "peak", "snr", "hfr", "fwhm", "ecc")}
    empty["saturated"] = np.zeros(0, dtype=bool)
    empty.update(background=float(np.median(bg[::4, ::4])), noise=noise)
    if ys.size == 0:
        return empty

    # Window size: start small and grow only while the typical star fills the window
    # (defocused stars do; trails and crowded neighbours mustn't make every window huge)
    if radius is not None:
        return _measure(img, data, smooth, noise, ys, xs, int(radius), k, max_stars, saturation, min_fwhm, bg)
    r = 6
    while True:
        result = _measure(img, data, smooth, noise, ys, xs, r, k, max_stars, saturation, min_fwhm, bg)
        cap_fwhm = GAUSS_FWHM * r / 2.5
        n = result["fwhm"].size
        if r >= 24 or n < 3:
            return result
        bright = result["fwhm"][:max(3, n // 2)]
        if float(np.median(bright)) < 0.8 * cap_fwhm:
            return result
        r = min(24, r * 2)


def _measure(img, data, smooth, noise, ys, xs, r, k, max_stars, saturation, min_fwhm, bg):
    h, w = data.shape
    empty = {key: np.zeros(0, dtype=np.float32) for key in ("x", "y", "flux", "peak", "snr", "hfr", "fwhm", "ecc")}
    empty["saturated"] = np.zeros(0, dtype=bool)
    empty.update(background=float(np.median(bg[::4, ::4])), noise=noise)
    inside = (ys >= r) & (ys < h - r) & (xs >= r) & (xs < w - r)
    ys, xs = ys[inside], xs[inside]
    if ys.size == 0:
        return empty
    order = np.argsort(-smooth[ys, xs])[:max_stars * 3]
    ys, xs = ys[order], xs[order]

    off = np.arange(-r, r + 1)
    oy, ox = np.meshgrid(off, off, indexing="ij")
    win = data[ys[:, None, None] + oy[None], xs[:, None, None] + ox[None]]
    raw_peak = img[ys, xs]

    ring = np.hypot(oy, ox) > r - 1.5
    local_bg = np.median(win[:, ring], axis=1)
    win = win - local_bg[:, None, None]
    peak = win[:, r - 1:r + 2, r - 1:r + 2].max(axis=(1, 2))
    keep = peak > k * noise
    win, ys, xs, peak, raw_peak = win[keep], ys[keep], xs[keep], peak[keep], raw_peak[keep]
    if ys.size == 0:
        return empty

    # Centroid from the pixels above the noise, then size and shape from adaptive
    # Gaussian moments, then HFR inside an aperture of 3 sigma
    wp = np.where(win > noise, win, 0.0)
    f0 = np.maximum(wp.sum(axis=(1, 2)), 1e-12)
    cx = (wp * ox).sum(axis=(1, 2)) / f0
    cy = (wp * oy).sum(axis=(1, 2)) / f0
    sxx, syy, sxy, cx, cy = _adaptive_moments(win, ox, oy, cx, cy, r)
    tr = sxx + syy
    det = sxx * syy - sxy * sxy
    disc = np.sqrt(np.maximum((tr * 0.5) ** 2 - det, 0.0))
    l1 = np.maximum(tr * 0.5 + disc, 1e-6)
    l2 = np.maximum(tr * 0.5 - disc, 1e-6)
    sigma = np.sqrt(np.sqrt(l1 * l2))
    fwhm = GAUSS_FWHM * sigma
    ecc = np.sqrt(np.clip(1.0 - l2 / l1, 0.0, 1.0))

    d = np.hypot(ox[None] - cx[:, None, None], oy[None] - cy[:, None, None])
    aperture = np.clip(3.0 * np.sqrt(l1), 2.0, r)[:, None, None]
    wa = np.where(d <= aperture, np.maximum(win, 0.0), 0.0)
    flux = np.maximum(wa.sum(axis=(1, 2)), 1e-12)
    hfr = (wa * d).sum(axis=(1, 2)) / flux

    sat_level = saturation if saturation is not None else np.inf
    saturated = raw_peak >= sat_level
    good = np.isfinite(fwhm) & (fwhm >= min_fwhm) & (fwhm < 4 * r)
    result = {
        "x": (xs + cx)[good].astype(np.float32),
        "y": (ys + cy)[good].astype(np.float32),
        "flux": flux[good].astype(np.float32),
        "peak": peak[good].astype(np.float32),
        "snr": (peak / noise)[good].astype(np.float32),
        "hfr": hfr[good].astype(np.float32),
        "fwhm": fwhm[good].astype(np.float32),
        "ecc": ecc[good].astype(np.float32),
        "saturated": saturated[good],
        "background": float(np.median(bg[::4, ::4])),
        "noise": noise,
    }
    order = np.argsort(-result["flux"])[:max_stars]
    for key in ("x", "y", "flux", "peak", "snr", "hfr", "fwhm", "ecc", "saturated"):
        result[key] = result[key][order]
    return result


def _adaptive_moments(win, ox, oy, cx, cy, r, iterations=5):
    """Covariance of each star's Gaussian, from moments weighted by a Gaussian window
    and corrected for that window: C_star = (C_meas^-1 - C_window^-1)^-1. The data
    isn't clipped at zero, so noise averages out instead of inflating the wings."""
    s2 = np.full(cx.shape, 2.0)
    sxx = syy = s2
    sxy = np.zeros_like(s2)
    wpos = win
    # The window may not outgrow the box, nor the centre wander off to a neighbour
    cap = max((r / 2.5) ** 2, 1.0)
    cx0, cy0 = cx.copy(), cy.copy()
    for _ in range(iterations):
        sw2 = np.clip(2.0 * (sxx + syy) * 0.5, 0.6, cap)
        dx = ox[None] - cx[:, None, None]
        dy = oy[None] - cy[:, None, None]
        g = np.exp(-(dx * dx + dy * dy) / (2.0 * sw2[:, None, None]))
        g = np.where(dx * dx + dy * dy <= 9.0 * sw2[:, None, None], g, 0.0)
        wg = wpos * g
        f = np.maximum(wg.sum(axis=(1, 2)), 1e-12)
        mx = np.clip((wg * dx).sum(axis=(1, 2)) / f, -1.0, 1.0)
        my = np.clip((wg * dy).sum(axis=(1, 2)) / f, -1.0, 1.0)
        cx = np.clip(cx + mx, cx0 - 1.5, cx0 + 1.5)
        cy = np.clip(cy + my, cy0 - 1.5, cy0 + 1.5)
        dx = dx - mx[:, None, None]
        dy = dy - my[:, None, None]
        mxx = (wg * dx * dx).sum(axis=(1, 2)) / f
        myy = (wg * dy * dy).sum(axis=(1, 2)) / f
        mxy = (wg * dx * dy).sum(axis=(1, 2)) / f
        # Invert the measured covariance, take away the window's, invert back
        det = np.maximum(mxx * myy - mxy * mxy, 1e-9)
        ixx, iyy, ixy = myy / det, mxx / det, -mxy / det
        ixx = ixx - 1.0 / sw2
        iyy = iyy - 1.0 / sw2
        det2 = ixx * iyy - ixy * ixy
        bad = (ixx <= 0) | (iyy <= 0) | (det2 <= 1e-9)
        det2 = np.where(bad, 1.0, det2)
        nxx, nyy, nxy = iyy / det2, ixx / det2, -ixy / det2
        sxx = np.where(bad, mxx * 2.0, nxx)
        syy = np.where(bad, myy * 2.0, nyy)
        sxy = np.where(bad, mxy * 2.0, nxy)
    # A pixel is a box, not a point: take away its 1/12 px^2 of variance
    sxx = np.clip(sxx - 1.0 / 12.0, 0.05, cap)
    syy = np.clip(syy - 1.0 / 12.0, 0.05, cap)
    return sxx, syy, sxy, cx, cy


def summarise(stars, pixel_scale=1.0, min_snr=10.0):
    """Frame-level numbers from detect_stars: median sizes of the well-exposed,
    unsaturated stars, in the frame's own pixels times pixel_scale."""
    n = int(stars["x"].size)
    # Streaks (satellite and plane trails) show up as strings of very stretched
    # "stars"; while they're a minority they say nothing about focus or tracking
    # (a frame with real tracking trouble has nearly every star stretched)
    streak = stars["ecc"] > 0.85
    if n and streak.mean() < 0.5:
        stars = {key: (value[~streak] if isinstance(value, np.ndarray) else value) for key, value in stars.items()}
        n = int(stars["x"].size)
    good = (~stars["saturated"]) & (stars["snr"] >= 3 * min_snr)
    if good.sum() < 10:
        good = (~stars["saturated"]) & (stars["snr"] >= min_snr)
    if good.sum() < 3:
        good = ~stars["saturated"]
    if good.sum() < 3:
        good = np.ones(n, dtype=bool)

    def med(key, scale=1.0):
        values = stars[key][good]
        return float(np.median(values)) * scale if values.size else None

    return {
        "stars": n,
        "fwhm": med("fwhm", pixel_scale),
        "hfr": med("hfr", pixel_scale),
        "ecc": med("ecc"),
        "snr": med("snr"),
        "background": float(stars["background"]),
        "noise": float(stars["noise"]),
        "saturated_stars": int(stars["saturated"].sum()),
    }
