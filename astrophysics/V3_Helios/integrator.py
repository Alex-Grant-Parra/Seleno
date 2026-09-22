"""
Force model and integrator for V3 Helios: the readable reference.

cpp/helios_core.cpp is a line-for-line port used for speed.  This module is
what the C++ is checked against, and the fallback when it is not built.

Forces (all in ECLIPJ2000, SI units):
  * Newtonian point-mass gravity between all bodies.
  * Relativity: full Einstein-Infeld-Hoffmann 1PN n-body terms (as JPL's DE
    ephemerides use), or the Sun's Schwarzschild term only.
  * The Earth's J2 (equatorial bulge) acting on the Moon.
  * The Moon's own shape (J2, C22), which pulls slightly harder along the
    Earth-Moon line because the Moon keeps its long axis towards the Earth.
  * Tides raised on the Earth by the Moon, with a constant time lag, which
    slowly push the Moon outwards and decelerate it in longitude.
J2 and tides depend on the direction of the Earth's pole, which precesses by
~11 degrees over 2000 years.  The pole is held fixed within each interval of
`pole_interval` seconds from the epoch, at its value for the middle of that
interval; this keeps runs deterministic however they are split into chunks.

Integrator: Suzuki's 4th-order composition of velocity Verlet for the
position-dependent forces (Newtonian + J2), with the small velocity-dependent
part (relativity + tides, ~1e-8 of the total) applied as half-kicks at both
ends of each step and evaluated once per step.
"""

import math

import numpy as np

try:
    from .constants import (G, C_LIGHT, EARTH_J2, EARTH_RADIUS_M, EARTH_K2, EARTH_ROTATION_RATE_RAD_S,
                            MOON_J2, MOON_C22, MOON_RADIUS_M,
                            EARTH_TIDAL_TIME_LAG_S, OBLIQUITY_J2000_RAD, ARCSEC_TO_RAD,
                            SECONDS_PER_JULIAN_CENTURY, PRECESSION_PSI_A_ARCSEC,
                            PRECESSION_OMEGA_A_ARCSEC)
    from .frames import earthPoleEclipJ2000
except ImportError:
    from constants import (G, C_LIGHT, EARTH_J2, EARTH_RADIUS_M, EARTH_K2, EARTH_ROTATION_RATE_RAD_S,
                           MOON_J2, MOON_C22, MOON_RADIUS_M,
                           EARTH_TIDAL_TIME_LAG_S, OBLIQUITY_J2000_RAD, ARCSEC_TO_RAD,
                           SECONDS_PER_JULIAN_CENTURY, PRECESSION_PSI_A_ARCSEC,
                           PRECESSION_OMEGA_A_ARCSEC)
    from frames import earthPoleEclipJ2000

# Suzuki's fractal 4th-order composition: substeps 0.41, 0.41, -0.66, 0.41, 0.41.
_SUZUKI_P = 1.0 / (4.0 - 4.0 ** (1.0 / 3.0))
SUZUKI_WEIGHTS = (_SUZUKI_P, _SUZUKI_P, 1.0 - 4.0 * _SUZUKI_P, _SUZUKI_P, _SUZUKI_P)

_NO_PRECESSION = (0.0,) * 6


class Physics:
    """The force model: positional(r, pole) and correction(r, v, pole).

    positional  : Newtonian gravity + the Earth's J2 on the Moon + the Moon's
                  shape (depends on r).
    correction  : relativity + Earth tides from the Moon (depends on r and v).
    """

    def __init__(self, masses, sun_idx, earth_idx=None, moon_idx=None, *, relativity="eih",
                 earth_j2=True, lunar_figure=True, tides=True, precession=True, et0=0.0,
                 pole_interval=16 * 86400.0, tidal_lag=EARTH_TIDAL_TIME_LAG_S, eps=1e-6):
        if relativity not in (None, "sun", "eih"):
            raise ValueError(f"Unknown relativity model: {relativity!r}")
        self.masses = np.asarray(masses, dtype=np.float64)
        self.mu = G * self.masses
        self.mu_list = self.mu.tolist()
        self.relativity = relativity
        self.sun_idx = int(sun_idx)
        has_earth_moon = earth_idx is not None and moon_idx is not None
        self.earth_idx = int(earth_idx) if earth_idx is not None else -1
        self.moon_idx = int(moon_idx) if moon_idx is not None else -1
        self.earth_j2 = bool(earth_j2 and has_earth_moon)
        self.lunar_figure = bool(lunar_figure and has_earth_moon)
        self.tides = bool(tides and has_earth_moon)
        self.precession = bool(precession)
        self.j2_coeff = 1.5 * EARTH_J2 * EARTH_RADIUS_M ** 2
        self.lunar_figure_coeff = (1.5 * MOON_J2 + 9.0 * MOON_C22) * MOON_RADIUS_M ** 2
        self.tide_k2r5 = EARTH_K2 * EARTH_RADIUS_M ** 5
        self.tidal_lag = float(tidal_lag)
        self.earth_rate = EARTH_ROTATION_RATE_RAD_S
        self.eps_sq = eps * eps
        self.inv_c2 = 1.0 / (C_LIGHT * C_LIGHT)
        self.et0 = float(et0)
        self.pole_interval = float(pole_interval)
        self.psi_coeffs = PRECESSION_PSI_A_ARCSEC if self.precession else _NO_PRECESSION
        self.omega_coeffs = PRECESSION_OMEGA_A_ARCSEC if self.precession else _NO_PRECESSION
        if self.earth_j2 or self.tides or self.lunar_figure:
            self.moon_to_earth_mass = float(self.masses[self.moon_idx] / self.masses[self.earth_idx])

    def settings(self):
        """Everything that changes the trajectory, for cache validation."""
        return {
            "relativity": self.relativity or "none",
            "earth_j2": self.earth_j2,
            "lunar_figure": self.lunar_figure,
            "tides": self.tides,
            "tidal_lag_s": self.tidal_lag if self.tides else 0.0,
            "earth_k2": EARTH_K2 if self.tides else 0.0,
            "precession": self.precession,
            "pole_interval_s": self.pole_interval,
            "eps_sq": self.eps_sq,
        }

    # ---- Earth's pole ---------------------------------------------------------
    def poleIndex(self, t, h):
        """Index of the pole interval that the step from t to t + h lies in."""
        if self.pole_interval <= 0.0:
            return 0
        return math.floor((t + 0.5 * h) / self.pole_interval)

    def poleForIndex(self, j):
        """Mean pole of date (ECLIPJ2000) at the middle of pole interval j."""
        t_mid = (j + 0.5) * self.pole_interval if self.pole_interval > 0.0 else 0.0
        T = (self.et0 + t_mid) / SECONDS_PER_JULIAN_CENTURY
        return earthPoleEclipJ2000(T, self.psi_coeffs, self.omega_coeffs)

    # ---- Forces ---------------------------------------------------------------
    def positional(self, r, pole):
        """Newtonian gravity + J2 + lunar figure: evaluated five times per step, so kept lean."""
        delta = r[:, None, :] - r[None, :, :]
        dist_sq = np.einsum("ijk,ijk->ij", delta, delta)
        dist_sq += self.eps_sq
        w = dist_sq * np.sqrt(dist_sq)
        np.divide(self.mu, w, out=w)
        np.fill_diagonal(w, 0.0)
        acc = np.matmul(w[:, None, :], delta)[:, 0, :]
        np.negative(acc, out=acc)
        if self.earth_j2:
            self._addEarthJ2(acc, r, pole)
        if self.lunar_figure:
            self._addLunarFigure(acc, r)
        return acc

    def correction(self, r, v, pole):
        """Relativity + tides: velocity dependent, evaluated once per step."""
        out = np.zeros_like(r)
        if self.relativity is not None:
            delta = r[:, None, :] - r[None, :, :]                   # r_i - r_j
            dist_sq = np.einsum("ijk,ijk->ij", delta, delta) + self.eps_sq
            inv_r = 1.0 / np.sqrt(dist_sq)
            np.fill_diagonal(inv_r, 0.0)
            if self.relativity == "eih":
                out += self._eih(v, delta, inv_r)
            else:
                out += self._sunSchwarzschild(v, delta, inv_r)
        if self.tides:
            self._addTides(out, r, v, pole)
        return out

    def _eih(self, v, delta, inv_r):
        # Einstein-Infeld-Hoffmann 1PN, beta = gamma = 1 (Moyer 2000 eq. 4-61):
        # a_i += 1/c^2 * { sum_j mu_j (r_j-r_i)/r_ij^3 * B_ij
        #                + sum_j mu_j/r_ij^3 [(r_i-r_j).(4v_i-3v_j)] (v_i-v_j)
        #                + 7/2 sum_j mu_j a_j / r_ij }
        # B_ij = -4 U_i - U_j + v_i^2 + 2 v_j^2 - 4 v_i.v_j
        #        - 3/2 [(r_i-r_j).v_j / r_ij]^2 + 1/2 (r_j-r_i).a_j
        # with U_i = sum_{k!=i} mu_k / r_ik and a_j the Newtonian acceleration.
        mu = self.mu
        w = inv_r ** 3 * mu
        a_newton = -np.matmul(w[:, None, :], delta)[:, 0, :]
        U = inv_r @ mu
        v_sq = np.einsum("ij,ij->i", v, v)
        vi_vj = v @ v.T
        d_dot_vj = np.einsum("ijk,jk->ij", delta, v)            # (r_i-r_j).v_j
        d_dot_vi = np.einsum("ijk,ik->ij", delta, v)            # (r_i-r_j).v_i
        d_dot_aj = np.einsum("ijk,jk->ij", delta, a_newton)     # (r_i-r_j).a_j

        B = (-4.0 * U[:, None] - U[None, :] + v_sq[:, None] + 2.0 * v_sq[None, :]
             - 4.0 * vi_vj - 1.5 * (d_dot_vj * inv_r) ** 2 - 0.5 * d_dot_aj)
        term1 = -np.matmul((w * B)[:, None, :], delta)[:, 0, :]
        W2 = w * (4.0 * d_dot_vi - 3.0 * d_dot_vj)
        term2 = W2.sum(axis=1)[:, None] * v - W2 @ v
        term3 = 3.5 * ((inv_r * mu) @ a_newton)
        return (term1 + term2 + term3) * self.inv_c2

    def _sunSchwarzschild(self, v, delta, inv_r):
        # da_i = GM/(c^2 r^3) [(4 GM/r - v^2) r_vec + 4 (r_vec.v_vec) v_vec], relative to the Sun.
        s = self.sun_idx
        mu_sun = self.mu[s]
        r_vec = delta[:, s, :]
        inv = inv_r[:, s]                                       # 0 for the Sun itself
        v_vec = v - v[s]
        v_sq = np.einsum("ij,ij->i", v_vec, v_vec)
        r_dot_v = np.einsum("ij,ij->i", r_vec, v_vec)
        gr = (mu_sun * self.inv_c2 * inv ** 3)[:, None] * (
            (4.0 * mu_sun * inv - v_sq)[:, None] * r_vec + (4.0 * r_dot_v)[:, None] * v_vec
        )
        # Equal and opposite reaction on the Sun keeps total momentum conserved.
        gr[s] = -(self.masses @ gr) / self.masses[s]
        return gr

    def _addEarthJ2(self, acc, r, pole):
        # a = -(3/2) J2 GM R^2 / d^5 * [(1 - 5 z^2/d^2) d_vec + 2 z pole]  on the Moon,
        # with the equal and opposite reaction on the Earth.
        e, m = self.earth_idx, self.moon_idx
        px, py, pz = pole.tolist()
        ex, ey, ez = r[e].tolist()
        x, y, zc = r[m].tolist()
        dx, dy, dz = x - ex, y - ey, zc - ez
        d_sq = dx * dx + dy * dy + dz * dz
        z = dx * px + dy * py + dz * pz
        c = -self.j2_coeff * self.mu_list[e] / (d_sq * d_sq * math.sqrt(d_sq))
        radial = c * (1.0 - 5.0 * z * z / d_sq)
        polar = 2.0 * c * z
        f = (radial * dx + polar * px, radial * dy + polar * py, radial * dz + polar * pz)
        for a in range(3):
            acc[m, a] += f[a]
            acc[e, a] -= self.moon_to_earth_mass * f[a]

    def _addLunarFigure(self, acc, r):
        # The Earth sits near the Moon's equator, on its long axis, so the Moon's
        # J2 and C22 terms reduce to an extra radial pull:
        #   a_earth = -(3/2 J2 + 9 C22) GM_moon R^2 / d^5 * d_vec   (d_vec = Earth - Moon),
        # and the reaction on the Moon.  Optical libration (+-8 deg) and the
        # Earth's +-7 deg latitude change this by ~1%, and only periodically.
        e, m = self.earth_idx, self.moon_idx
        dx, dy, dz = (r[e] - r[m]).tolist()
        d_sq = dx * dx + dy * dy + dz * dz
        c = -self.lunar_figure_coeff / (d_sq * d_sq * math.sqrt(d_sq))
        ce, cm = c * self.mu_list[m], -c * self.mu_list[e]
        acc[e, 0] += ce * dx
        acc[e, 1] += ce * dy
        acc[e, 2] += ce * dz
        acc[m, 0] += cm * dx
        acc[m, 1] += cm * dy
        acc[m, 2] += cm * dz

    def _addTides(self, out, r, v, pole):
        # Tide raised on the Earth by the Moon, lagging by a constant time dt:
        # a_moon = -3 k2 GM_moon R^5 / d^8 * [d + dt (2 (d.w)/d^2 d + d x Omega + w)]
        # d, w = Moon relative to Earth; Omega = Earth's spin vector (Mignard 1979).
        e, m = self.earth_idx, self.moon_idx
        dx, dy, dz = (r[m] - r[e]).tolist()
        wx, wy, wz = (v[m] - v[e]).tolist()
        ox, oy, oz = (self.earth_rate * pole).tolist()
        d_sq = dx * dx + dy * dy + dz * dz
        radial = 2.0 * (dx * wx + dy * wy + dz * wz) / d_sq
        lag = self.tidal_lag
        c = -3.0 * self.tide_k2r5 * self.mu_list[m] / (d_sq * d_sq * d_sq * d_sq)
        f = (c * (dx + lag * (radial * dx + (dy * oz - dz * oy) + wx)),
             c * (dy + lag * (radial * dy + (dz * ox - dx * oz) + wy)),
             c * (dz + lag * (radial * dz + (dx * oy - dy * ox) + wz)))
        for a in range(3):
            out[m, a] += f[a]
            out[e, a] -= self.moon_to_earth_mass * f[a]

    def total(self, r, v, t):
        """Total acceleration at time t (for diagnostics; the integrator splits it)."""
        pole = self.poleForIndex(self.poleIndex(t, 0.0))
        return self.positional(r, pole) + self.correction(r, v, pole)


# ---------------------------------------------------------------------------
# Python integrator (reference for helios_core.integrate / propagate)
# ---------------------------------------------------------------------------

class _Workspace:
    """Per-run state: the pole in use and the accelerations at the current (r, v)."""
    __slots__ = ("pole_index", "pole", "a_pos", "corr")

    def __init__(self):
        self.pole_index = None
        self.pole = self.a_pos = self.corr = None

    def copy(self):
        other = _Workspace()
        other.pole_index, other.pole, other.a_pos, other.corr = self.pole_index, self.pole, self.a_pos, self.corr
        return other


def _ensurePole(physics, ws, r, v, t, h):
    # Entering a new pole interval: switch pole and recompute both accelerations
    # at the current state, so the result does not depend on how a run is chunked.
    j = physics.poleIndex(t, h)
    if j != ws.pole_index:
        ws.pole_index = j
        ws.pole = physics.poleForIndex(j)
        ws.a_pos = physics.positional(r, ws.pole)
        ws.corr = physics.correction(r, v, ws.pole)


def _step(physics, ws, r, v, h):
    v = v + (0.5 * h) * ws.corr
    for w in SUZUKI_WEIGHTS:
        s = w * h
        v = v + (0.5 * s) * ws.a_pos
        r = r + s * v
        ws.a_pos = physics.positional(r, ws.pole)
        v = v + (0.5 * s) * ws.a_pos
    ws.corr = physics.correction(r, v, ws.pole)
    v = v + (0.5 * h) * ws.corr
    return r, v


def integratePython(physics, r, v, t0, h, steps, store_every):
    """`steps` steps of signed size h from time t0; returns (r_hist, v_hist, r, v)
    with a sample after every `store_every` steps."""
    r = np.array(r, dtype=np.float64)
    v = np.array(v, dtype=np.float64)
    n_store = steps // store_every
    r_hist = np.empty((n_store,) + r.shape)
    v_hist = np.empty((n_store,) + r.shape)
    ws = _Workspace()
    stored = 0
    for n in range(steps):
        t = t0 + n * h
        _ensurePole(physics, ws, r, v, t, h)
        r, v = _step(physics, ws, r, v, h)
        if (n + 1) % store_every == 0:
            r_hist[stored], v_hist[stored] = r, v
            stored += 1
    return r_hist, v_hist, r, v


def propagatePython(physics, r, v, t0, h_abs, targets):
    """States at `targets` (all on one side of t0, nearest first) from (r, v) at t0.

    Steps of h_abs on the same grid as the build, then one partial step to
    each target; returns (r_out, v_out) shaped (len(targets), n, 3).
    """
    r = np.array(r, dtype=np.float64)
    v = np.array(v, dtype=np.float64)
    targets = [float(x) for x in targets]
    r_out = np.empty((len(targets),) + r.shape)
    v_out = np.empty((len(targets),) + r.shape)
    _checkTargets(t0, targets)
    direction = 1.0 if any(x > t0 for x in targets) else -1.0
    h = direction * h_abs
    ws = _Workspace()
    done = 0
    for idx, target in enumerate(targets):
        n_target = math.floor(abs(target - t0) / h_abs)
        while done < n_target:
            t = t0 + done * h
            _ensurePole(physics, ws, r, v, t, h)
            r, v = _step(physics, ws, r, v, h)
            done += 1
        t_grid = t0 + done * h
        rem = target - t_grid
        if rem != 0.0:
            ws_part = ws.copy()
            _ensurePole(physics, ws_part, r, v, t_grid, rem)
            r_out[idx], v_out[idx] = _step(physics, ws_part, r, v, rem)
        else:
            r_out[idx], v_out[idx] = r, v
    return r_out, v_out


def _checkTargets(t0, targets):
    signs = {(x > t0) - (x < t0) for x in targets} - {0}
    if len(signs) > 1:
        raise ValueError("propagate targets must all lie on one side of t0")
    distances = [abs(x - t0) for x in targets]
    if any(b < a for a, b in zip(distances, distances[1:])):
        raise ValueError("propagate targets must be sorted nearest first")


# ---------------------------------------------------------------------------
# Compiled core (C++ port of the above)
# ---------------------------------------------------------------------------

def loadCompiledCore():
    """Return (helios_core module, None) if the C++ extension is built and at
    least as new as its source, else (None, reason)."""
    from pathlib import Path
    import importlib

    base = Path(__file__).resolve().parent
    source = base / "cpp" / "helios_core.cpp"
    try:
        try:
            core = importlib.import_module(".helios_core", __package__) if __package__ else None
        except ImportError:
            core = None
        if core is None:
            core = importlib.import_module("helios_core")
    except ImportError as exc:
        return None, f"not built ({exc})"
    built = Path(core.__file__)
    if source.exists() and built.stat().st_mtime < source.stat().st_mtime:
        return None, f"{built.name} is older than {source.name}; rebuild it"
    return core, None


def compiledModel(core, physics):
    """The C++ counterpart of a Physics object (every constant passed in from here)."""
    return core.Model(
        mu=physics.mu.tolist(),
        masses=physics.masses.tolist(),
        relativity=physics.relativity or "none",
        sun_idx=physics.sun_idx,
        earth_idx=physics.earth_idx,
        moon_idx=physics.moon_idx,
        earth_j2=physics.earth_j2,
        lunar_figure=physics.lunar_figure,
        tides=physics.tides,
        j2_coeff=physics.j2_coeff,
        lunar_figure_coeff=physics.lunar_figure_coeff,
        tide_k2r5=physics.tide_k2r5,
        tidal_lag=physics.tidal_lag,
        earth_rate=physics.earth_rate,
        eps_sq=physics.eps_sq,
        inv_c2=physics.inv_c2,
        et0=physics.et0,
        pole_interval=physics.pole_interval,
        obliquity0=OBLIQUITY_J2000_RAD,
        psi_coeffs=list(physics.psi_coeffs),
        omega_coeffs=list(physics.omega_coeffs),
        seconds_per_century=SECONDS_PER_JULIAN_CENTURY,
        arcsec_to_rad=ARCSEC_TO_RAD,
        weights=list(SUZUKI_WEIGHTS),
    )
