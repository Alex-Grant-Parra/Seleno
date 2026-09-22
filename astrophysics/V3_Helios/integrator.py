import math

import numpy as np

try:
    from .constants import G, C_LIGHT, EARTH_J2, EARTH_RADIUS_M, EARTH_POLE_ECLIPJ2000
except ImportError:
    from constants import G, C_LIGHT, EARTH_J2, EARTH_RADIUS_M, EARTH_POLE_ECLIPJ2000


def computeAccelerations(positions, masses, eps=1e-6):
    """Compute gravitational accelerations using vectorised NumPy broadcasting.

    Replaces the Python pairwise loop with a fully vectorised broadcast that
    runs in NumPy's C backend (~10-50x faster for small N).  Newton's third
    law is enforced by the antisymmetry of the delta tensor.
    """
    pos = np.asarray(positions, dtype=np.float64)
    m = np.asarray(masses, dtype=np.float64)

    # delta[i,j] = pos[i] - pos[j],  shape (N, N, 3)
    delta = pos[:, None, :] - pos[None, :, :]

    # Squared pairwise distances with softening,  shape (N, N)
    dist_sq = np.einsum("ijk,ijk->ij", delta, delta) + eps * eps

    inv_d3 = dist_sq ** -1.5
    np.fill_diagonal(inv_d3, 0.0)          # no self-acceleration

    # accel[i] = G * sum_j( m[j] * (r_j - r_i) / r_ij^3 )
    #           = -G * sum_j( m[j] * delta[i,j] * inv_d3[i,j] )
    return -G * np.einsum("ij,j,ijk->ik", inv_d3, m, delta)


def computeGRCorrections(positions, velocities, masses, sun_idx=0):
    """First-order post-Newtonian (Schwarzschild) correction from the Sun.

    The 1PN acceleration on body i in the Sun's field is:

        da_i = (GM_sun / c^2 r^3) * [(4*GM_sun/r - v^2)*r_vec + 4*(r_vec.v_vec)*v_vec]

    where r_vec and v_vec are position and velocity relative to the Sun.
    Standalone utility; the integrator uses ForceModel.
    """
    pos = np.asarray(positions, dtype=np.float64)
    vel = np.asarray(velocities, dtype=np.float64)

    GM_sun = G * float(masses[sun_idx])
    r_vec = pos - pos[sun_idx]
    v_vec = vel - vel[sun_idx]
    r = np.sqrt(np.einsum("ij,ij->i", r_vec, r_vec))
    r[sun_idx] = 1.0                                  # avoid 0/0; row zeroed below
    v_sq = np.einsum("ij,ij->i", v_vec, v_vec)
    r_dot_v = np.einsum("ij,ij->i", r_vec, v_vec)

    prefactor = GM_sun / (C_LIGHT * C_LIGHT * r ** 3)
    corrections = prefactor[:, None] * (
        (4.0 * GM_sun / r - v_sq)[:, None] * r_vec + 4.0 * r_dot_v[:, None] * v_vec
    )
    corrections[sun_idx] = 0.0
    return corrections


class ForceModel:
    """Total acceleration on every body: force(r, v) -> (N, 3) array.

    relativity
        None   : Newtonian only.
        "sun"  : 1PN Schwarzschild term of the Sun on each body (+ reaction).
        "eih"  : full Einstein-Infeld-Hoffmann 1PN n-body equations (beta =
                 gamma = 1), as used for the JPL DE ephemerides.  Besides the
                 Sun's field this includes how the other bodies' potentials
                 and velocities modify each pairwise pull, which matters for
                 the Earth-Moon orbit.
    oblate_idx
        Index of the Earth to include its J2 (equatorial bulge), with the
        reaction on the Earth.  None disables it.
    oblate_targets
        Bodies the J2 term acts on (default: all others).  Only the Moon is
        close enough to matter (<1 m over 10 years for anything else), and a
        short list is evaluated with scalar maths, which is much cheaper.

    Everything is vectorised over the N x N pairs; at N~11 NumPy call
    overhead dominates, so the number of calls is what sets the run time.

    force(r, v) is the total.  positional(r) (Newtonian + J2) and
    correction(r, v) (relativity) are also available separately so that
    splitSuzuki4Step can evaluate the tiny, slowly varying relativistic part
    once per step instead of once per substep.
    """

    def __init__(self, masses, relativity="eih", sun_idx=0, oblate_idx=None,
                 oblate_targets=None, j2=EARTH_J2, radius=EARTH_RADIUS_M,
                 pole=EARTH_POLE_ECLIPJ2000, eps=1e-6):
        self.masses = np.asarray(masses, dtype=np.float64)
        self.mu = G * self.masses
        if relativity not in (None, "sun", "eih"):
            raise ValueError(f"Unknown relativity model: {relativity!r}")
        self.relativity = relativity
        self.sun_idx = sun_idx
        self.oblate_idx = oblate_idx
        if oblate_targets is None and oblate_idx is not None:
            oblate_targets = [i for i in range(len(self.masses)) if i != oblate_idx]
        self.oblate_targets = list(oblate_targets or [])
        self.mu_list = self.mu.tolist()
        if oblate_idx is not None:
            self.mass_ratio = (self.masses / self.masses[oblate_idx]).tolist()
        self.j2_coeff = 1.5 * j2 * radius * radius
        self.pole = np.asarray(pole, dtype=np.float64)
        self.eps_sq = eps * eps
        self.inv_c2 = 1.0 / (C_LIGHT * C_LIGHT)

    @property
    def velocityDependent(self):
        return self.relativity is not None

    def _newtonian(self, r):
        delta = r[:, None, :] - r[None, :, :]                  # r_i - r_j
        dist_sq = np.einsum("ijk,ijk->ij", delta, delta) + self.eps_sq
        inv_r = 1.0 / np.sqrt(dist_sq)
        np.fill_diagonal(inv_r, 0.0)
        inv_r3 = inv_r * inv_r * inv_r
        w = inv_r3 * self.mu
        acc = -np.matmul(w[:, None, :], delta)[:, 0, :]
        return acc, delta, inv_r, inv_r3, w

    def _relativistic(self, v, acc_newton, delta, inv_r, inv_r3, w):
        if self.relativity == "eih":
            return self._eih(v, delta, inv_r, inv_r3, w, acc_newton)
        if self.relativity == "sun":
            return self._sunSchwarzschild(v, delta, inv_r)
        return np.zeros_like(acc_newton)

    def positional(self, r):
        """Velocity-independent part: Newtonian gravity + J2.

        This runs 5x per step, so it avoids the intermediates EIH needs.
        """
        delta = r[:, None, :] - r[None, :, :]
        dist_sq = np.einsum("ijk,ijk->ij", delta, delta)
        dist_sq += self.eps_sq
        w = dist_sq * np.sqrt(dist_sq)
        np.divide(self.mu, w, out=w)
        np.fill_diagonal(w, 0.0)
        acc = np.matmul(w[:, None, :], delta)[:, 0, :]
        np.negative(acc, out=acc)
        if self.oblate_idx is not None:
            self._addOblateness(acc, r)
        return acc

    def correction(self, r, v):
        """Relativistic (velocity-dependent) part only."""
        acc, delta, inv_r, inv_r3, w = self._newtonian(r)
        return self._relativistic(v, acc, delta, inv_r, inv_r3, w)

    def __call__(self, r, v):
        acc, delta, inv_r, inv_r3, w = self._newtonian(r)
        if self.relativity is not None:
            acc = acc + self._relativistic(v, acc, delta, inv_r, inv_r3, w)
        if self.oblate_idx is not None:
            self._addOblateness(acc, r)
        return acc

    def _eih(self, v, delta, inv_r, inv_r3, w, a_newton):
        # a_i += 1/c^2 * { sum_j mu_j (r_j-r_i)/r_ij^3 * B_ij
        #                + sum_j mu_j/r_ij^3 [(r_i-r_j).(4v_i-3v_j)] (v_i-v_j)
        #                + 7/2 sum_j mu_j a_j / r_ij }
        # B_ij = -4 U_i - U_j + v_i^2 + 2 v_j^2 - 4 v_i.v_j
        #        - 3/2 [(r_i-r_j).v_j / r_ij]^2 + 1/2 (r_j-r_i).a_j
        # with U_i = sum_k mu_k / r_ik and a_j the Newtonian acceleration.
        mu = self.mu
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

    def _addOblateness(self, acc, r):
        # a = -(3/2) J2 GM R^2 / d^5 * [(1 - 5 z^2/d^2) d_vec + 2 z pole],
        # added in place, with the equal and opposite reaction on the Earth.
        e = self.oblate_idx
        px, py, pz = self.pole.tolist()
        ex, ey, ez = r[e].tolist()
        k = -self.j2_coeff * self.mu_list[e]
        rx = ry = rz = 0.0
        for i in self.oblate_targets:
            x, y, zc = r[i].tolist()
            dx, dy, dz = x - ex, y - ey, zc - ez
            d_sq = dx * dx + dy * dy + dz * dz
            z = dx * px + dy * py + dz * pz
            c = k / (d_sq * d_sq * math.sqrt(d_sq))
            radial = c * (1.0 - 5.0 * z * z / d_sq)
            polar = 2.0 * c * z
            fx, fy, fz = radial * dx + polar * px, radial * dy + polar * py, radial * dz + polar * pz
            acc[i, 0] += fx
            acc[i, 1] += fy
            acc[i, 2] += fz
            ratio = self.mass_ratio[i]
            rx -= ratio * fx
            ry -= ratio * fy
            rz -= ratio * fz
        acc[e, 0] += rx
        acc[e, 1] += ry
        acc[e, 2] += rz


def velocityVerletStep(r, v, a, force, dt):
    """Velocity Verlet integrator step.

    A time-reversible, symplectic integrator with O(dt^2) local accuracy.

    For velocity-dependent (relativistic) forces, the force at the new
    position uses the predicted velocity v_half + dt/2 * a_old, which is
    O(dt^2) accurate.
    """
    v_half = v + 0.5 * dt * a
    r_new = r + dt * v_half

    v_pred = v_half + 0.5 * dt * a
    a_new = force(r_new, v_pred)

    v_new = v_half + 0.5 * dt * a_new
    return r_new, v_new, a_new


def _compositionStep(r, v, a, force, dt, weights):
    # Symmetric composition of Verlet substeps; each substep reuses the
    # previous acceleration, so the cost is one force evaluation per weight.
    for w in weights:
        r, v, a = velocityVerletStep(r, v, a, force, w * dt)
    return r, v, a


_CBRT2 = 2.0 ** (1.0 / 3.0)
_YOSHIDA_W1 = 1.0 / (2.0 - _CBRT2)
_YOSHIDA_W0 = 1.0 - 2.0 * _YOSHIDA_W1
_YOSHIDA_WEIGHTS = (_YOSHIDA_W1, _YOSHIDA_W0, _YOSHIDA_W1)

_SUZUKI_P = 1.0 / (4.0 - 4.0 ** (1.0 / 3.0))
_SUZUKI_WEIGHTS = (_SUZUKI_P, _SUZUKI_P, 1.0 - 4.0 * _SUZUKI_P, _SUZUKI_P, _SUZUKI_P)


def yoshida4Step(r, v, a, force, dt):
    """4th-order Yoshida symplectic integrator (triple jump, 3 force evaluations).

    Its middle substep is -1.70*dt, which gives it a large error constant.
    """
    return _compositionStep(r, v, a, force, dt, _YOSHIDA_WEIGHTS)


def suzuki4Step(r, v, a, force, dt):
    """4th-order Suzuki fractal composition (5 force evaluations).

    Substeps are 0.41, 0.41, -0.66, 0.41, 0.41 of dt.  Its error constant is
    far smaller than Yoshida's, so at the same cost per simulated day it can
    take a larger step and still be more accurate.
    """
    return _compositionStep(r, v, a, force, dt, _SUZUKI_WEIGHTS)


def splitSuzuki4Step(r, v, state, force, dt):
    """Suzuki 4th order for the positional force, with the relativistic
    correction applied as half-kicks at both ends of the step (Strang split).

    The correction is ~1e-8 of the total force and varies on orbital
    timescales, so evaluating it once per step (reused as the next step's
    opening half-kick) costs one evaluation instead of five, while the
    symmetric split keeps its error second order in an already tiny term.

    `state` is what the previous call returned, or None on the first step.
    It is (positional acceleration, correction) at the current (r, v).
    """
    if state is None:
        state = (force.positional(r), force.correction(r, v))
    a_pos, corr = state

    v = v + (0.5 * dt) * corr
    for w in _SUZUKI_WEIGHTS:
        h = w * dt
        v = v + (0.5 * h) * a_pos
        r = r + h * v
        a_pos = force.positional(r)
        v = v + (0.5 * h) * a_pos
    corr = force.correction(r, v)
    v = v + (0.5 * dt) * corr
    return r, v, (a_pos, corr)


def adaptiveVerletStep(r, v, a, force, dt, tol, dt_min=10.0, dt_max=7200.0):
    """Velocity Verlet with step-doubling adaptive error control (M4).

    Takes one full step of size dt and two half-steps of size dt/2.  The
    difference in final positions gives a Richardson-extrapolated error:

        err ~ |r_half2 - r_full| / 3      (3 = 2^2 - 1 for a 2nd-order method)

    The two-half-step result is returned as it is one order more accurate
    (Richardson local extrapolation).

    Parameters
    ----------
    tol     : position error tolerance in metres (worst body, per step)
    dt_min  : hard floor on dt (seconds) -- step is force-accepted at this size
    dt_max  : hard ceiling on dt (seconds)

    Returns
    -------
    r_new, v_new, a_new, dt_used, dt_next
    """
    MAX_HALVINGS = 12

    for _ in range(MAX_HALVINGS):
        # Full step
        r1, v1, a1 = velocityVerletStep(r, v, a, force, dt)

        # Two half-steps
        dt2 = dt / 2.0
        r_m, v_m, a_m = velocityVerletStep(r, v, a, force, dt2)
        r2, v2, a2 = velocityVerletStep(r_m, v_m, a_m, force, dt2)

        # Richardson error estimate in metres (worst body)
        err = float(np.max(np.linalg.norm(r2 - r1, axis=1))) / 3.0

        if err <= tol or dt <= dt_min:
            if err > 0.0:
                scale = 0.9 * (tol / err) ** (1.0 / 3.0)
                dt_next = float(np.clip(dt * scale, dt_min, dt_max))
            else:
                dt_next = min(dt * 2.0, dt_max)
            return r2, v2, a2, dt, dt_next

        # Reject and halve
        dt = max(dt / 2.0, dt_min)

    # Safety fallback after max halvings
    return r2, v2, a2, dt, dt
