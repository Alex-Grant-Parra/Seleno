"""Lining frames up.

Star fields: triangles of bright stars are matched by their shape (side ratios), each
match proposes a transform, RANSAC keeps the one most stars agree with, and a least
squares fit over all the matched stars refines it.

The Moon and planets: phase correlation of the images, to a fraction of a pixel.

A transform is a 2x3 matrix M taking reference (x, y) to the frame's (x, y):
[x', y'] = M @ [x, y, 1].
"""
from itertools import combinations

import numpy as np
from scipy.spatial import cKDTree


class RegistrationError(Exception):
    pass


def apply_transform(m, xy):
    xy = np.asarray(xy, dtype=np.float64)
    return xy @ m[:, :2].T + m[:, 2]


def estimate_similarity(src, dst):
    """Rotation, uniform scale and shift taking src points to dst (Umeyama, no mirroring)."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    ms, md = src.mean(axis=0), dst.mean(axis=0)
    a, b = src - ms, dst - md
    var = (a * a).sum() / len(src)
    if var <= 1e-12:
        raise RegistrationError("degenerate points")
    cov = b.T @ a / len(src)
    u, s, vt = np.linalg.svd(cov)
    d = np.sign(np.linalg.det(u) * np.linalg.det(vt)) or 1.0
    sign = np.diag([1.0, d])
    rot = u @ sign @ vt
    scale = (s * np.diag(sign)).sum() / var
    m = np.zeros((2, 3))
    m[:, :2] = scale * rot
    m[:, 2] = md - m[:, :2] @ ms
    return m


def estimate_affine(src, dst):
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    a = np.hstack([src, np.ones((len(src), 1))])
    sol, *_ = np.linalg.lstsq(a, dst, rcond=None)
    return sol.T


def transform_scale(m):
    return float(np.sqrt(abs(np.linalg.det(m[:, :2]))))


def transform_rotation_deg(m):
    return float(np.degrees(np.arctan2(m[1, 0], m[0, 0])))


def _triangles(xy, neighbours=5):
    n = len(xy)
    if n < 3:
        return np.zeros((0, 3), dtype=int), np.zeros((0, 2))
    tree = cKDTree(xy)
    k = min(neighbours + 1, n)
    _, idx = tree.query(xy, k=k)
    tris = set()
    for i in range(n):
        for a, b in combinations(idx[i, 1:], 2):
            tris.add(tuple(sorted((i, int(a), int(b)))))
    tris = np.array(sorted(tris), dtype=int)
    p = xy[tris]
    # The side opposite each vertex
    sides = np.stack([
        np.linalg.norm(p[:, 1] - p[:, 2], axis=1),
        np.linalg.norm(p[:, 0] - p[:, 2], axis=1),
        np.linalg.norm(p[:, 0] - p[:, 1], axis=1),
    ], axis=1)
    order = np.argsort(sides, axis=1)
    sides = np.take_along_axis(sides, order, axis=1)
    tris = np.take_along_axis(tris, order, axis=1)
    ok = sides[:, 2] > 8.0
    inv = np.stack([sides[:, 0] / np.maximum(sides[:, 2], 1e-9), sides[:, 1] / np.maximum(sides[:, 2], 1e-9)], axis=1)
    # Near-isosceles triangles have an ambiguous vertex order
    ok &= (inv[:, 1] - inv[:, 0] > 0.03) & (1.0 - inv[:, 1] > 0.03)
    return tris[ok], inv[ok]


def match_stars(ref_xy, tgt_xy, n_bright=40, tolerance=2.5, transform="similarity",
                max_scale_change=0.2, min_matches=6, rng=None):
    """Find M taking reference stars to target stars. Both lists brightest first.
    Returns (M, matched_count, rms_px)."""
    ref_xy = np.asarray(ref_xy, dtype=np.float64)
    tgt_xy = np.asarray(tgt_xy, dtype=np.float64)
    if len(ref_xy) < 3 or len(tgt_xy) < 3:
        raise RegistrationError("not enough stars to match")

    ref_b, tgt_b = ref_xy[:n_bright], tgt_xy[:n_bright]
    tri_r, inv_r = _triangles(ref_b)
    tri_t, inv_t = _triangles(tgt_b)
    if not len(tri_r) or not len(tri_t):
        raise RegistrationError("not enough stars to match")

    tree_inv = cKDTree(inv_t)
    hits = tree_inv.query_ball_point(inv_r, r=0.012)
    candidates = [(i, j) for i, js in enumerate(hits) for j in js]
    if not candidates:
        raise RegistrationError("no matching star patterns")
    rng = rng or np.random.default_rng(1)
    if len(candidates) > 4000:
        pick = rng.choice(len(candidates), 4000, replace=False)
        candidates = [candidates[k] for k in pick]

    ref_all = ref_xy[:250]
    tgt_all = tgt_xy[:400]
    tgt_tree = cKDTree(tgt_all)
    need = min(len(ref_all), len(tgt_all))
    best_m, best_count = None, 0
    for i, j in candidates:
        try:
            m = estimate_similarity(ref_b[tri_r[i]], tgt_b[tri_t[j]])
        except RegistrationError:
            continue
        if abs(transform_scale(m) - 1.0) > max_scale_change:
            continue
        d, _ = tgt_tree.query(apply_transform(m, ref_all), distance_upper_bound=tolerance * 2)
        count = int((d < tolerance * 2).sum())
        if count > best_count:
            best_m, best_count = m, count
            if count >= 0.8 * need:
                break
    if best_m is None or best_count < min(min_matches, need):
        raise RegistrationError("star patterns didn't agree")

    m = best_m
    count, rms = 0, 0.0
    for tol in (tolerance * 2, tolerance, tolerance):
        d, nn = tgt_tree.query(apply_transform(m, ref_all), distance_upper_bound=tol)
        ok = d < tol
        if ok.sum() < 3:
            raise RegistrationError("too few stars matched")
        src, dst = ref_all[ok], tgt_all[nn[ok]]
        m = estimate_affine(src, dst) if (transform == "affine" and ok.sum() >= 6) else estimate_similarity(src, dst)
        resid = np.linalg.norm(apply_transform(m, src) - dst, axis=1)
        count, rms = int(ok.sum()), float(np.sqrt(np.mean(resid ** 2)))
    if count < min(min_matches, need):
        raise RegistrationError("too few stars matched")
    return m, count, rms


def scale_transform(m, factor, offset=0.0):
    """M measured on images binned by `factor` (pixel centres at factor*i + offset in
    the full image) as a full-resolution transform."""
    m = np.asarray(m, dtype=np.float64)
    full = np.zeros((2, 3))
    full[:, :2] = m[:, :2]
    c = np.array([offset, offset])
    # x_f = factor * (A ((x - c)/factor) + t) + c
    full[:, 2] = factor * m[:, 2] + c - m[:, :2] @ c
    return full


def to_ndimage(m):
    """Matrix and offset for scipy.ndimage.affine_transform, which works in (row, col):
    the output pixel at reference (y, x) samples the frame at M(x, y)."""
    matrix = np.array([[m[1, 1], m[1, 0]], [m[0, 1], m[0, 0]]])
    offset = np.array([m[1, 2], m[0, 2]])
    return matrix, offset


def phase_correlate(ref, img, upsample=True):
    """The (dy, dx) shift that lines img up with ref: img(y + dy, x + dx) ~ ref(y, x).
    Both the same shape. Returns (dy, dx, peak_strength)."""
    ref = np.asarray(ref, dtype=np.float32)
    img = np.asarray(img, dtype=np.float32)
    h, w = ref.shape
    win = np.outer(np.hanning(h), np.hanning(w)).astype(np.float32)
    a = np.fft.rfft2((ref - ref.mean()) * win)
    b = np.fft.rfft2((img - img.mean()) * win)
    cross = b * np.conj(a)
    cross /= np.maximum(np.abs(cross), 1e-12)
    corr = np.fft.irfft2(cross, s=(h, w))
    peak = np.unravel_index(np.argmax(corr), corr.shape)
    py, px = peak
    strength = float(corr[py, px])

    def refine(c_m, c_0, c_p):
        denom = c_m - 2 * c_0 + c_p
        return 0.0 if abs(denom) < 1e-12 else 0.5 * (c_m - c_p) / denom

    dy = py + (refine(corr[(py - 1) % h, px], corr[py, px], corr[(py + 1) % h, px]) if upsample else 0)
    dx = px + (refine(corr[py, (px - 1) % w], corr[py, px], corr[py, (px + 1) % w]) if upsample else 0)
    if dy > h / 2:
        dy -= h
    if dx > w / 2:
        dx -= w
    return float(dy), float(dx), strength
