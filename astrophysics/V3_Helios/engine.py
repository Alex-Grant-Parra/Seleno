import json
import numpy as np
from pathlib import Path

try:
    from .integrator import (
        ForceModel,
        velocityVerletStep,
        yoshida4Step,
        suzuki4Step,
        splitSuzuki4Step,
        adaptiveVerletStep,
    )
except ImportError:
    from integrator import (ForceModel, velocityVerletStep, yoshida4Step,
                            suzuki4Step, splitSuzuki4Step, adaptiveVerletStep)

_STEP_FUNCS = {
    "verlet": velocityVerletStep,
    "yoshida4": yoshida4Step,
    "suzuki4": suzuki4Step,
    "splitSuzuki4": splitSuzuki4Step,
}


# ============================================================================
# DIAGNOSTICS UTILITIES
# ============================================================================

G = 6.67430e-11


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


def loadInitialConditions(path=None):
    if path is None:
        path = Path(__file__).resolve().parent / "initial_conditions.json"

    with open(path, "r") as f:
        data = json.load(f)

    names = list(data["bodies"].keys())

    r = np.array([data["bodies"][n]["position_m"] for n in names], dtype=np.float64)
    v = np.array([data["bodies"][n]["velocity_m_s"] for n in names], dtype=np.float64)

    return names, r, v


# DE440 gravitational parameters GM (m^3/s^2), from gm_de440.tpc.  GM is known
# to ~10 significant figures, whereas G and the masses in kg are only known to
# ~5, so the simulation is driven by GM.  Mars..Pluto are planet-system values
# (planet + moons) to match the system-barycentre states written by loader.py.
DE440_GM = {
    "sun": 1.3271244004127939e20,
    "mercury": 2.2031868551400003e13,
    "venus": 3.24858592e14,
    "earth": 3.986004355070226e14,
    "moon": 4.902800118457549e12,
    "mars": 4.2828375815756095e13,
    "jupiter": 1.267127641e17,
    "saturn": 3.794058484179999e16,
    "uranus": 5.794556399999998e15,
    "neptune": 6.836527100580398e15,
    "pluto": 9.755e11,
}


def loadGravitationalParameters(names, path=None):
    """Return GM (m^3/s^2) per body: from initial_conditions.json when loader.py
    wrote them there, otherwise from the DE440_GM table."""
    if path is None:
        path = Path(__file__).resolve().parent / "initial_conditions.json"

    bodies = {}
    if Path(path).exists():
        with open(path, "r") as f:
            bodies = json.load(f).get("bodies", {})

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


DEFAULT_DURATION = 3652.5 * 86400.0   # 10 Julian years


def buildForceModel(names, masses, relativity="eih", earth_j2=True):
    """Force model used by the simulation: Newtonian + relativity + Earth J2 on the Moon."""
    has_earth_moon = "earth" in names and "moon" in names
    return ForceModel(
        masses,
        relativity=relativity,
        sun_idx=names.index("sun"),
        oblate_idx=names.index("earth") if (earth_j2 and has_earth_moon) else None,
        oblate_targets=[names.index("moon")] if (earth_j2 and has_earth_moon) else None,
    )


def runSimulation(steps=None, dt=7200, store_every=1, integrator="splitSuzuki4",
                  relativity="eih", earth_j2=True, use_gr=None, adaptive=False,
                  adaptive_tol=1e4, duration=None, initial_state=None,
                  diag_every=100, progress_callback=None):
    """Integrate the solar system forward from initial_conditions.json.

    The run length is `steps` fixed steps if given, otherwise `duration`
    seconds (default 10 years).

    Defaults (checked against DE440 over one year): EIH relativity + Earth
    J2 with Suzuki 4th order at dt=7200 s, the relativistic part evaluated
    once per step.  Planets stay within ~0.01", the Moon ~0.6 km/yr (0.3"/yr).

    relativity : "eih" (full n-body 1PN), "sun" (Sun's Schwarzschild term
        only) or None.  use_gr=False is kept as an alias for relativity=None.
    earth_j2 : include Earth's equatorial bulge acting on the Moon.

    initial_state : dict with "r", "v", "t" (as returned in "finalState")
        Continue a previous run from its final state instead of starting at
        the epoch.  The t=0 sample is only emitted for fresh runs, so the
        histories of consecutive runs can be concatenated directly.
    """
    names, r, v = loadInitialConditions()
    masses = np.asarray(getMasses(names), dtype=np.float64)

    if initial_state is None:
        r = np.asarray(r, dtype=np.float64)
        v = np.asarray(v, dtype=np.float64)
        # Remove net center-of-mass velocity to eliminate bulk drift
        total_mass = np.sum(masses)
        v_cm = np.sum(masses[:, None] * v, axis=0) / total_mass
        v = v - v_cm
        t0 = 0.0
    else:
        r = np.array(initial_state["r"], dtype=np.float64)
        v = np.array(initial_state["v"], dtype=np.float64)
        t0 = float(initial_state["t"])
    total_mass = np.sum(masses)

    initial_momentum = totalMomentum(v, masses)
    initial_energy = totalEnergy(r, v, masses)
    initial_angular_momentum = totalAngularMomentum(r, v, masses)

    if use_gr is False:
        relativity = None
    force = buildForceModel(names, masses, relativity, earth_j2)

    dt = float(dt)
    if steps is None:
        steps = int(np.ceil(float(duration if duration is not None else DEFAULT_DURATION) / dt - 1e-9))
    t_end = float(duration if (duration is not None and adaptive) else steps * dt)

    if integrator == "auto":
        integrator = "verlet" if adaptive else "splitSuzuki4"
    if adaptive:
        integrator = "verlet"
    if integrator not in _STEP_FUNCS:
        raise ValueError(f"Unknown integrator {integrator!r}; choose from {sorted(_STEP_FUNCS)}")
    step_func = _STEP_FUNCS[integrator]
    # splitSuzuki4 carries its own (positional, correction) state; the others
    # carry the total acceleration.
    a = None if step_func is splitSuzuki4Step else force(r, v)
    print(f"Integrator: {integrator}  adaptive={adaptive}  relativity={relativity}  "
          f"J2={force.oblate_idx is not None}  dt={dt:.0f}s  "
          f"t={t0/86400:.1f}..{(t0 + t_end)/86400:.1f} days")

    def diagnostics(r, v):
        return (totalEnergy(r, v, masses) - initial_energy,
                totalMomentum(v, masses) - initial_momentum,
                totalAngularMomentum(r, v, masses) - initial_angular_momentum,
                earthSunDistance(r))

    emit_start = initial_state is None

    if not adaptive:
        n_bodies = len(names)
        n_store = steps // store_every + (1 if emit_start else 0)
        n_diag = (steps - 1) // diag_every + 1 if steps > 0 else 0

        rHistory = np.empty((n_store, n_bodies, 3), dtype=np.float64)
        vHistory = np.empty((n_store, n_bodies, 3), dtype=np.float64)
        tHistory = np.empty(n_store, dtype=np.float64)

        energyLog = np.empty(n_diag, dtype=np.float64)
        momentumLog = np.empty((n_diag, 3), dtype=np.float64)
        angularMomentumLog = np.empty((n_diag, 3), dtype=np.float64)
        earthDistanceLog = np.empty(n_diag, dtype=np.float64)

        store_idx = 0
        diag_idx = 0
        if emit_start:
            rHistory[0], vHistory[0], tHistory[0] = r, v, t0
            store_idx = 1

        report_every = max(1, steps // 10)

        # ---- Fixed-step loop --------------------------------------------------
        for step in range(steps):
            r, v, a = step_func(r, v, a, force, dt)

            if (step + 1) % store_every == 0:
                rHistory[store_idx] = r
                vHistory[store_idx] = v
                tHistory[store_idx] = t0 + (step + 1) * dt
                store_idx += 1

            if step % diag_every == 0:
                (energyLog[diag_idx], momentumLog[diag_idx],
                 angularMomentumLog[diag_idx], earthDistanceLog[diag_idx]) = diagnostics(r, v)
                diag_idx += 1

            if step % report_every == 0:
                p_mag = np.linalg.norm(momentumLog[diag_idx - 1]) if diag_idx > 0 else 0
                e_val = energyLog[diag_idx - 1] if diag_idx > 0 else 0
                baseline = np.linalg.norm(initial_momentum)
                p_drift_pct = (p_mag / baseline) * 100 if baseline > 0 else 0
                percent = 100 * step // steps
                if progress_callback is not None:
                    try:
                        progress_callback(step, steps, percent, e_val, p_mag, p_drift_pct)
                    except Exception:
                        pass
                else:
                    print(f"Step {step:6d}/{steps} ({percent:2d}%)  E={e_val:.3e} J  p={p_mag:.3e} kg*m/s")

        t_sim = t0 + steps * dt

    else:
        # ---- Adaptive-step loop -----------------------------------------------
        rHistory, vHistory, tHistory = [], [], []
        energyLog, momentumLog, angularMomentumLog, earthDistanceLog = [], [], [], []
        if emit_start:
            rHistory.append(r.copy())
            vHistory.append(v.copy())
            tHistory.append(t0)

        t_sim = 0.0
        current_dt = dt
        accepted = 0
        diag_interval = diag_every * dt
        diag_t_next = 0.0
        report_interval = t_end / 10.0
        report_t_next = 0.0

        while t_sim < t_end:
            step_dt = min(current_dt, t_end - t_sim)
            if step_dt <= 0.0:
                break

            r, v, a, dt_used, current_dt = adaptiveVerletStep(
                r, v, a, force, step_dt, adaptive_tol
            )
            t_sim += dt_used
            accepted += 1

            if accepted % store_every == 0:
                rHistory.append(r.copy())
                vHistory.append(v.copy())
                tHistory.append(t0 + t_sim)

            if t_sim >= diag_t_next:
                e_val, p_val, l_val, d_val = diagnostics(r, v)
                energyLog.append(e_val)
                momentumLog.append(p_val)
                angularMomentumLog.append(l_val)
                earthDistanceLog.append(d_val)
                diag_t_next = t_sim + diag_interval

            if t_sim >= report_t_next:
                p_mag = np.linalg.norm(momentumLog[-1]) if momentumLog else 0
                e_val = energyLog[-1] if energyLog else 0
                percent = int(100 * t_sim / t_end)
                if progress_callback is not None:
                    try:
                        progress_callback(accepted, int(t_end / dt), percent, e_val, p_mag, 0.0)
                    except Exception:
                        pass
                else:
                    print(f"t={t_sim:.0f}s ({percent:2d}%)  dt={current_dt:.1f}s  accepted={accepted}  E={e_val:.3e} J")
                report_t_next = t_sim + report_interval

        t_sim = t0 + t_sim
        rHistory = np.array(rHistory, dtype=np.float64)
        vHistory = np.array(vHistory, dtype=np.float64)
        tHistory = np.array(tHistory, dtype=np.float64)
        energyLog = np.array(energyLog, dtype=np.float64)
        momentumLog = np.array(momentumLog, dtype=np.float64)
        angularMomentumLog = np.array(angularMomentumLog, dtype=np.float64)
        earthDistanceLog = np.array(earthDistanceLog, dtype=np.float64)

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