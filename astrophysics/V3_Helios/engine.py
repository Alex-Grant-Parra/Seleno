"""
Simulation engine for V3 Helios: initial conditions, masses, and the
Propagator that runs the integrator in C++ (helios_core) or Python.
"""

import json
import os
from pathlib import Path

import numpy as np

try:
    from .constants import G, DE440_GM, SECONDS_PER_DAY, DAYS_PER_JULIAN_YEAR, EARTH_TIDAL_TIME_LAG_S
    from .integrator import Physics, integratePython, propagatePython, loadCompiledCore, compiledModel
except ImportError:
    from constants import G, DE440_GM, SECONDS_PER_DAY, DAYS_PER_JULIAN_YEAR, EARTH_TIDAL_TIME_LAG_S
    from integrator import Physics, integratePython, propagatePython, loadCompiledCore, compiledModel

_BASE = Path(__file__).resolve().parent
IC_PATH = _BASE / "initial_conditions.json"

DEFAULT_DT = 7200.0                               # s; checked against DE440 (see ephemeris.py)
DEFAULT_POLE_INTERVAL = 16 * SECONDS_PER_DAY      # = ephemeris checkpoint spacing
DEFAULT_DURATION = 10 * DAYS_PER_JULIAN_YEAR * SECONDS_PER_DAY


# ============================================================================
# DIAGNOSTICS UTILITIES
# ============================================================================

# G and the GM table come from constants.py.  Masses are derived as GM / G and
# the force model multiplies back by the same G, so the DE440 GM values are
# reproduced exactly whatever G is set to.


def totalEnergy(r, v, m):
    masses = np.asarray(m, dtype=np.float64)
    velocities = np.asarray(v, dtype=np.float64)
    positions = np.asarray(r, dtype=np.float64)

    kinetic = 0.5 * np.sum(masses * np.sum(velocities * velocities, axis=1, dtype=np.float64), dtype=np.float64)

    i, j = np.triu_indices(len(masses), k=1)
    dist = np.linalg.norm(positions[j] - positions[i], axis=1)
    potential = -np.float64(G) * np.sum(masses[i] * masses[j] / dist, dtype=np.float64)

    return np.float64(kinetic + potential)


def totalMomentum(v, m):
    masses = np.asarray(m, dtype=np.float64)
    velocities = np.asarray(v, dtype=np.float64)
    return np.sum(masses[:, None] * velocities, axis=0, dtype=np.float64)


def totalAngularMomentum(r, v, m):
    masses = np.asarray(m, dtype=np.float64)
    positions = np.asarray(r, dtype=np.float64)
    velocities = np.asarray(v, dtype=np.float64)
    return np.sum(masses[:, None] * np.cross(positions, velocities), axis=0, dtype=np.float64)


def earthSunDistance(r, earthIndex=3, sunIndex=0):
    return np.linalg.norm(r[earthIndex] - r[sunIndex])


# ============================================================================
# SIMULATION ENGINE
# ============================================================================


def _readInitialConditions(path=None):
    with open(Path(path) if path else IC_PATH, "r") as f:
        return json.load(f)


def loadInitialConditions(path=None):
    data = _readInitialConditions(path)
    names = list(data["bodies"].keys())
    r = np.array([data["bodies"][n]["position_m"] for n in names], dtype=np.float64)
    v = np.array([data["bodies"][n]["velocity_m_s"] for n in names], dtype=np.float64)
    return names, r, v


def loadEpochEt(path=None):
    """Epoch of the initial conditions in TDB seconds past J2000."""
    return float(_readInitialConditions(path)["epoch_et"])


def loadGravitationalParameters(names, path=None):
    """Return GM (m^3/s^2) per body: from initial_conditions.json when loader.py
    wrote them there, otherwise from the DE440_GM table."""
    bodies = _readInitialConditions(path).get("bodies", {}) if Path(path or IC_PATH).exists() else {}
    gm = []
    missing = []
    for name in names:
        value = bodies.get(name, {}).get("gm_m3_s2", DE440_GM.get(name))
        if value is None:
            missing.append(name)
        gm.append(value)
    if missing:
        raise KeyError(f"Missing GM entries for bodies: {', '.join(missing)}")
    return np.array(gm, dtype=np.float64)


def getMasses(names, path=None):
    """Return masses in kg, derived as GM / G so that G * m reproduces GM exactly."""
    return loadGravitationalParameters(names, path) / G


class Propagator:
    """The solar-system integrator with the full force model, in C++ when available.

    backend : "auto" (C++ helios_core when built and current, else Python),
        "cpp" or "python".  The HELIOS_BACKEND environment variable overrides
        "auto".  Both backends implement the same algorithm; results agree to
        rounding (centimetres after a year).

    The epoch state has the system's net centre-of-mass velocity removed, so
    the barycentre of the eleven bodies stays put.
    """

    def __init__(self, *, dt=DEFAULT_DT, pole_interval=DEFAULT_POLE_INTERVAL, relativity="eih",
                 earth_j2=True, lunar_figure=True, tides=True, precession=True, tidal_lag=None,
                 backend="auto",
                 ic_path=None, quiet=False):
        names, r, v = loadInitialConditions(ic_path)
        masses = getMasses(names, ic_path)
        v_cm = np.sum(masses[:, None] * v, axis=0) / np.sum(masses)
        self.names = names
        self.masses = masses
        self.epoch_r = r
        self.epoch_v = v - v_cm
        self.et0 = loadEpochEt(ic_path)
        self.dt = float(dt)

        def index(name):
            return names.index(name) if name in names else None

        self.physics = Physics(
            masses, names.index("sun"), index("earth"), index("moon"),
            relativity=relativity, earth_j2=earth_j2, lunar_figure=lunar_figure, tides=tides,
            precession=precession,
            et0=self.et0, pole_interval=pole_interval,
            tidal_lag=EARTH_TIDAL_TIME_LAG_S if tidal_lag is None else tidal_lag,
        )

        choice = os.environ.get("HELIOS_BACKEND", backend) if backend == "auto" else backend
        if choice not in ("auto", "cpp", "python"):
            raise ValueError(f"backend must be 'auto', 'cpp' or 'python', not {choice!r}")
        self.core = self.model = None
        if choice in ("auto", "cpp"):
            core, reason = loadCompiledCore()
            if core is None:
                if choice == "cpp":
                    raise RuntimeError(f"C++ backend requested but helios_core is {reason}. "
                                       "Build it with: python astrophysics/V3_Helios/build_core.py")
                if not quiet:
                    print(f"helios_core {reason}; using the Python integrator "
                          "(build with: python astrophysics/V3_Helios/build_core.py)")
            else:
                self.core = core
                self.model = compiledModel(core, self.physics)
        self.backend = "cpp" if self.model is not None else "python"

    def settings(self):
        """Everything that changes the trajectory (used to validate caches)."""
        return {"integrator": "splitSuzuki4", "dt": self.dt, **self.physics.settings()}

    def integrate(self, r, v, t0, steps, store_every=1, direction=1):
        """`steps` steps of dt (backwards if direction < 0) from time t0 (s from epoch).
        Returns (r_hist, v_hist, r, v), a sample after every `store_every` steps."""
        h = self.dt if direction >= 0 else -self.dt
        if self.model is not None:
            return self.core.integrate(self.model, r, v, float(t0), h, int(steps), int(store_every))
        return integratePython(self.physics, r, v, float(t0), h, int(steps), int(store_every))

    def propagate(self, r, v, t0, targets):
        """States at `targets` (s from epoch; one side of t0, nearest first)."""
        if self.model is not None:
            return self.core.propagate(self.model, r, v, float(t0), self.dt, [float(x) for x in targets])
        return propagatePython(self.physics, r, v, float(t0), self.dt, targets)


def runSimulation(duration=None, dt=DEFAULT_DT, store_every=1, relativity="eih", earth_j2=True,
                  lunar_figure=True, tides=True, precession=True, initial_state=None, diag_every=100,
                  progress_callback=None, backend="auto", steps=None):
    """Integrate from initial_conditions.json and return the sampled trajectory.

    duration : seconds to run, default 10 years; negative runs backwards.
        `steps` (fixed steps of dt) overrides it.
    initial_state : dict with "r", "v", "t" (as returned in "finalState")
        Continue a previous run from its final state instead of starting at
        the epoch.  The t=0 sample is only emitted for fresh runs, so the
        histories of consecutive runs can be concatenated directly.
    backend : see Propagator.

    For positions at arbitrary dates use ephemeris.py, which caches
    checkpoints; this is for experiments and diagnostics.
    """
    prop = Propagator(dt=dt, relativity=relativity, earth_j2=earth_j2, lunar_figure=lunar_figure,
                      tides=tides, precession=precession, backend=backend)
    names, masses = prop.names, prop.masses
    if initial_state is None:
        r, v, t0 = prop.epoch_r.copy(), prop.epoch_v.copy(), 0.0
    else:
        r = np.array(initial_state["r"], dtype=np.float64)
        v = np.array(initial_state["v"], dtype=np.float64)
        t0 = float(initial_state["t"])
    total_mass = np.sum(masses)

    duration = DEFAULT_DURATION if duration is None else float(duration)
    direction = -1 if duration < 0 else 1
    if steps is None:
        steps = int(np.ceil(abs(duration) / prop.dt - 1e-9))
    h = direction * prop.dt

    initial_momentum = totalMomentum(v, masses)
    initial_energy = totalEnergy(r, v, masses)
    initial_angular_momentum = totalAngularMomentum(r, v, masses)

    print(f"Backend: {'C++ helios_core' if prop.backend == 'cpp' else 'Python'}")
    print(f"Integrator: splitSuzuki4  relativity={relativity}  J2={prop.physics.earth_j2}  "
          f"lunar figure={prop.physics.lunar_figure}  tides={prop.physics.tides}  precession={precession}  dt={h:.0f}s  "
          f"t={t0/SECONDS_PER_DAY:.1f}..{(t0 + steps * h)/SECONDS_PER_DAY:.1f} days")

    def diagnostics(r, v):
        return (totalEnergy(r, v, masses) - initial_energy,
                totalMomentum(v, masses) - initial_momentum,
                totalAngularMomentum(r, v, masses) - initial_angular_momentum,
                earthSunDistance(r, names.index("earth"), names.index("sun")))

    emit_start = initial_state is None
    n_bodies = len(names)
    n_store = steps // store_every + (1 if emit_start else 0)
    rHistory = np.empty((n_store, n_bodies, 3), dtype=np.float64)
    vHistory = np.empty((n_store, n_bodies, 3), dtype=np.float64)
    tHistory = np.empty(n_store, dtype=np.float64)
    store_idx = 0
    if emit_start:
        rHistory[0], vHistory[0], tHistory[0] = r, v, t0
        store_idx = 1

    # Run in ~10 chunks for progress reporting (chunking does not change the result).
    chunk = max(store_every, (steps // 10) // store_every * store_every)
    done = 0
    while done < steps:
        n = min(chunk, steps - done)
        rh, vh, r, v = prop.integrate(r, v, t0 + done * h, n, store_every, direction)
        k = len(rh)
        rHistory[store_idx:store_idx + k] = rh
        vHistory[store_idx:store_idx + k] = vh
        tHistory[store_idx:store_idx + k] = t0 + (done + store_every * np.arange(1, k + 1)) * h
        store_idx += k
        done += n
        percent = 100 * done // steps
        if progress_callback is not None:
            try:
                progress_callback(done, steps, percent, 0.0, 0.0, 0.0)
            except Exception:
                pass
        else:
            print(f"Step {done:6d}/{steps} ({percent:3d}%)")

    # Diagnostics from the stored samples, about every diag_every steps.
    stride = max(1, diag_every // store_every)
    diag = [diagnostics(rHistory[i], vHistory[i]) for i in range(0, n_store, stride)]
    energyLog = np.array([d[0] for d in diag], dtype=np.float64)
    momentumLog = np.array([d[1] for d in diag], dtype=np.float64).reshape(-1, 3)
    angularMomentumLog = np.array([d[2] for d in diag], dtype=np.float64).reshape(-1, 3)
    earthDistanceLog = np.array([d[3] for d in diag], dtype=np.float64)
    t_sim = t0 + steps * h

    # Final diagnostics for conservation laws
    final_momentum = totalMomentum(v, masses)
    final_energy = totalEnergy(r, v, masses)
    final_angular_momentum = totalAngularMomentum(r, v, masses)
    
    print(f"\n{'='*60}")
    print(f"CONSERVATION LAW DIAGNOSTICS")
    print(f"{'='*60}")
    print(f"Initial momentum magnitude:  {np.linalg.norm(initial_momentum):.3e} kg*m/s")
    print(f"Final momentum magnitude:    {np.linalg.norm(final_momentum):.3e} kg*m/s")
    print(f"Initial energy: {initial_energy:.6e} J")
    print(f"Final energy:   {final_energy:.6e} J")
    energy_drift = abs(final_energy - initial_energy) / abs(initial_energy) if initial_energy != 0 else 0
    print(f"Energy drift:   {energy_drift*100:.6f}%")
    momentum_drift = np.linalg.norm(final_momentum - initial_momentum)
    print(f"Momentum drift: {momentum_drift:.6e} kg*m/s")
    angular_drift = np.linalg.norm(final_angular_momentum - initial_angular_momentum)
    print(f"Angular momentum drift: {angular_drift:.6e} kg*m^2/s")
    print(f"{'='*60}\n")

    return {
        "names": names,
        "rHistory": rHistory,
        "vHistory": vHistory,
        "tHistory": tHistory,
        "energy": energyLog,
        "momentum": momentumLog,
        "angularMomentum": angularMomentumLog,
        "earthDistance": earthDistanceLog,
        "initialEnergy": initial_energy,
        "initialMomentum": initial_momentum,
        "totalMass": total_mass,
        "finalState": {"r": r.copy(), "v": v.copy(), "t": float(t_sim)},
    }


if __name__ == "__main__":
    results = runSimulation()

    print("\nSimulation complete")

    print("Bodies:", results["names"])
    print("Trajectory shape:", results["rHistory"].shape)

    print("\nDiagnostics:")
    print("Energy samples:", len(results["energy"]))
    print("Momentum samples:", len(results["momentum"]))
    print("Earth distance samples:", len(results["earthDistance"]))