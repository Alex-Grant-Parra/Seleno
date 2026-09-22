"""
Chebyshev polynomial ephemeris cache for V3 Helios.

Workflow
--------
Build once (~15 s for the default 10-year span):

    python astrophysics/V3_Helios/chebyshev.py

This will:
  1. Run the N-body engine for SPAN_DAYS and cache the raw trajectory, plus
     the final state, to rHistory.npz.  If rHistory.npz already exists for
     the same initial conditions and settings, the simulation is skipped;
     if it is shorter than SPAN_DAYS it is extended from its final state
     rather than re-run from the epoch.
  2. Fit degree-DEGREE Chebyshev polynomials over SEGMENT_DAYS-day segments
     for every body and axis.
  3. Write cheb_table.npz.

After that, every call to evaluateAt() is a microsecond polynomial lookup
with no simulation involved.

Rebuild whenever initial_conditions.json changes (i.e. after running
loader.py for a new epoch):

    python astrophysics/V3_Helios/chebyshev.py
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

try:
    from .constants import SECONDS_PER_DAY, DAYS_PER_JULIAN_YEAR
except ImportError:
    from constants import SECONDS_PER_DAY, DAYS_PER_JULIAN_YEAR

_BASE = Path(__file__).resolve().parent
_IC_PATH = _BASE / "initial_conditions.json"
_RHISTORY_PATH = _BASE / "rHistory.npz"
_CHEB_TABLE_PATH = _BASE / "cheb_table.npz"

_TABLE_CACHE = None
_TABLE_CACHE_KEY = None
_EPOCH_CACHE = None
_EPOCH_CACHE_KEY = None
_METADATA_SCHEMA = 3

# ---------------------------------------------------------------------------
# Fitting parameters.
# 4-day segments resolve the 27.3-day lunar orbit (~6-7 segments/orbit) while
# remaining more than sufficient for the slowly-moving outer planets.
# Degree 16 gives sub-km accuracy for all bodies over 4-day windows.
# ---------------------------------------------------------------------------
SEGMENT_DAYS = 4
DEGREE = 16

# How far past the epoch the table reaches.  Rounded up to a whole number of
# segments so the last segment is as well-sampled as the rest.
SPAN_DAYS = 10 * DAYS_PER_JULIAN_YEAR
_SPAN_SECONDS = float(np.ceil(SPAN_DAYS / SEGMENT_DAYS) * SEGMENT_DAYS * SECONDS_PER_DAY)

# Simulation settings; any change invalidates rHistory.npz.
_SIM_DT = 7200.0         # integrator timestep (seconds)
_SIM_STORE_EVERY = 1     # trajectory stored every N steps (48 samples/segment)
_SIM_INTEGRATOR = "splitSuzuki4"
_SIM_RELATIVITY = "eih"
_SIM_EARTH_J2 = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _isNewer(path: Path, reference: Path) -> bool:
    """Return True if *path* exists and is at least as new as *reference*."""
    return path.exists() and path.stat().st_mtime >= reference.stat().st_mtime


def _fileSha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tableCacheKey() -> tuple:
    return (
        str(_CHEB_TABLE_PATH),
        _CHEB_TABLE_PATH.stat().st_mtime_ns,
        _IC_PATH.stat().st_mtime_ns,
    )


def _expectedMetadata(icSha256: str) -> dict:
    return {
        "schema": _METADATA_SCHEMA,
        "segment_days": SEGMENT_DAYS,
        "degree": DEGREE,
        "span_seconds": _SPAN_SECONDS,
        "sim_dt": _SIM_DT,
        "sim_store_every": _SIM_STORE_EVERY,
        "sim_integrator": _SIM_INTEGRATOR,
        "sim_relativity": _SIM_RELATIVITY,
        "sim_earth_j2": _SIM_EARTH_J2,
        "ic_sha256": icSha256,
    }


def _expectedRhistoryMetadata(icSha256: str) -> dict:
    return {
        "schema": _METADATA_SCHEMA,
        "sim_dt": _SIM_DT,
        "sim_store_every": _SIM_STORE_EVERY,
        "sim_integrator": _SIM_INTEGRATOR,
        "sim_relativity": _SIM_RELATIVITY,
        "sim_earth_j2": _SIM_EARTH_J2,
        "ic_sha256": icSha256,
    }


def _parseMetadata(table: dict) -> dict:
    raw = table.get("metadata_json")
    if raw is None:
        return {}
    if isinstance(raw, np.ndarray):
        raw = raw.item()
    return json.loads(str(raw))


def _validateMetadata(table: dict) -> None:
    metadata = _parseMetadata(table)
    expected = _expectedMetadata(_fileSha256(_IC_PATH))

    missing_keys = [k for k in expected if k not in metadata]
    if missing_keys:
        raise RuntimeError(
            "Chebyshev table metadata is incomplete. "
            f"Missing keys: {', '.join(missing_keys)}. Rebuild with:\n"
            "    python astrophysics/V3_Helios/chebyshev.py"
        )

    mismatched = [k for k, value in expected.items() if metadata.get(k) != value]
    if mismatched:
        raise RuntimeError(
            "Chebyshev table metadata does not match current settings "
            f"for: {', '.join(mismatched)}. Rebuild with:\n"
            "    python astrophysics/V3_Helios/chebyshev.py"
        )


def _validateRhistoryMetadata(data: dict) -> None:
    metadata = _parseMetadata(data)
    expected = _expectedRhistoryMetadata(_fileSha256(_IC_PATH))
    missing_keys = [k for k in expected if k not in metadata]
    if missing_keys:
        raise RuntimeError(
            "rHistory cache metadata is incomplete. "
            f"Missing keys: {', '.join(missing_keys)}. Rebuild with:\n"
            "    python astrophysics/V3_Helios/chebyshev.py"
        )

    mismatched = [k for k, value in expected.items() if metadata.get(k) != value]
    if mismatched:
        raise RuntimeError(
            "rHistory cache metadata does not match current settings "
            f"for: {', '.join(mismatched)}. Rebuild with:\n"
            "    python astrophysics/V3_Helios/chebyshev.py"
        )


def _runSimulation(durationSeconds, initialState=None):
    """Run the N-body engine; return (names, rHistory, tHistory, finalState)."""
    try:
        from .engine import runSimulation
    except ImportError:
        from engine import runSimulation

    results = runSimulation(
        duration=durationSeconds,
        dt=_SIM_DT,
        store_every=_SIM_STORE_EVERY,
        integrator=_SIM_INTEGRATOR,
        relativity=_SIM_RELATIVITY,
        earth_j2=_SIM_EARTH_J2,
        initial_state=initialState,
    )
    return (list(results["names"]), np.asarray(results["rHistory"]),
            np.asarray(results["tHistory"], dtype=np.float64), results["finalState"])


def _loadCachedRhistory():
    """Return the cached trajectory dict if it matches the current settings, else None."""
    if not _isNewer(_RHISTORY_PATH, _IC_PATH):
        return None
    try:
        with np.load(_RHISTORY_PATH, allow_pickle=True) as npz:
            data = {key: npz[key] for key in npz.files}
        _validateRhistoryMetadata(data)
        for key in ("tHistory", "final_r", "final_v", "final_t"):
            if key not in data:
                raise RuntimeError(f"rHistory cache is missing '{key}'.")
        return data
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Cached trajectory rejected: {exc}")
        return None


def _getRhistory():
    """Return (names, rHistory, tHistory) covering [0, _SPAN_SECONDS].

    Reuses rHistory.npz when it matches, extends it from its saved final state
    when it is too short, and only runs from the epoch when there is no usable
    cache.
    """
    cached = _loadCachedRhistory()

    if cached is not None and float(cached["final_t"]) >= _SPAN_SECONDS - 1e-6:
        print(f"Loading cached trajectory from {_RHISTORY_PATH.name} …")
        keep = cached["tHistory"] <= _SPAN_SECONDS + 1e-6
        return list(cached["names"]), cached["rHistory"][keep], cached["tHistory"][keep]

    if cached is not None:
        tDone = float(cached["final_t"])
        print(f"Extending cached trajectory from {tDone / SECONDS_PER_DAY:.1f} to "
              f"{_SPAN_SECONDS / SECONDS_PER_DAY:.1f} days …")
        names, rNew, tNew, final = _runSimulation(
            _SPAN_SECONDS - tDone,
            initialState={"r": cached["final_r"], "v": cached["final_v"], "t": tDone},
        )
        names = list(cached["names"])
        rHistory = np.concatenate([cached["rHistory"], rNew])
        tHistory = np.concatenate([cached["tHistory"], tNew])
    else:
        print(f"Running N-body simulation for {_SPAN_SECONDS / SECONDS_PER_DAY:.0f} days (this happens once) …")
        names, rHistory, tHistory, final = _runSimulation(_SPAN_SECONDS)

    metadata_json = json.dumps(
        _expectedRhistoryMetadata(_fileSha256(_IC_PATH)),
        sort_keys=True,
    )
    np.savez_compressed(
        _RHISTORY_PATH,
        names=np.array(names),
        rHistory=rHistory,
        tHistory=tHistory,
        final_r=final["r"],
        final_v=final["v"],
        final_t=np.array(final["t"]),
        metadata_json=np.array(metadata_json),
    )
    print(f"Trajectory cached \u2192 {_RHISTORY_PATH.name}")
    return names, rHistory, tHistory


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def buildTable() -> None:
    """
    Fit Chebyshev polynomials to the simulated trajectory and save
    cheb_table.npz.  Safe to re-run; reuses rHistory.npz when fresh.
    """
    names, rHistory, tHistory = _getRhistory()

    ic_text = _IC_PATH.read_text()
    ic = json.loads(ic_text)
    epoch_utc = ic["epoch_utc"]
    metadata_json = json.dumps(
        _expectedMetadata(hashlib.sha256(ic_text.encode("utf-8")).hexdigest()),
        sort_keys=True,
    )

    n_samples, n_bodies, _ = rHistory.shape
    t_uniform = tHistory                       # actual sample timestamps (s from epoch)
    total_seconds = min(float(t_uniform[-1]), _SPAN_SECONDS)

    segment_seconds = SEGMENT_DAYS * SECONDS_PER_DAY
    n_segments = int(np.ceil(total_seconds / segment_seconds))

    # Shape: (n_bodies, 3 axes, n_segments, DEGREE+1 coefficients)
    coefficients = np.zeros((n_bodies, 3, n_segments, DEGREE + 1), dtype=np.float64)
    seg_t_a = np.zeros(n_segments, dtype=np.float64)
    seg_t_b = np.zeros(n_segments, dtype=np.float64)

    for seg in range(n_segments):
        t_a = seg * segment_seconds
        t_b = min(t_a + segment_seconds, total_seconds)
        seg_t_a[seg] = t_a
        seg_t_b[seg] = t_b

        # Samples that fall within this segment (inclusive on both ends)
        mask = (t_uniform >= t_a) & (t_uniform <= t_b)

        # Guard: ensure enough points to fit the polynomial
        if mask.sum() < DEGREE + 1:
            idx_end = int(np.searchsorted(t_uniform, t_b, side="right"))
            idx_start = max(0, idx_end - (DEGREE + 2))
            mask = np.zeros(n_samples, dtype=bool)
            mask[idx_start:idx_end] = True

        # Map sample times to τ ∈ [-1, 1] over this segment
        tau = (2.0 * t_uniform[mask] - (t_a + t_b)) / (t_b - t_a)

        # Fit every body/axis in one least-squares solve: y is (samples, bodies*3).
        vals = rHistory[mask].reshape(-1, n_bodies * 3).astype(np.float64)
        coef = np.polynomial.chebyshev.chebfit(tau, vals, DEGREE)     # (DEGREE+1, bodies*3)
        coefficients[:, :, seg, :] = coef.T.reshape(n_bodies, 3, DEGREE + 1)

    np.savez_compressed(
        _CHEB_TABLE_PATH,
        names=np.array(names),
        epoch_utc=np.array(epoch_utc),
        segment_seconds=np.array(segment_seconds),
        seg_t_a=seg_t_a,
        seg_t_b=seg_t_b,
        coefficients=coefficients,
        total_seconds=np.array(total_seconds),
        metadata_json=np.array(metadata_json),
    )
    print(
        f"Chebyshev table saved → {_CHEB_TABLE_PATH.name}\n"
        f"  Bodies: {n_bodies}   Segments: {n_segments}   "
        f"Degree: {DEGREE}   Span: {total_seconds / SECONDS_PER_DAY:.1f} days"
    )


def _loadTable(forceReload: bool = False):
    """Load and validate the Chebyshev table from disk."""
    global _TABLE_CACHE, _TABLE_CACHE_KEY

    if not _CHEB_TABLE_PATH.exists():
        print("Chebyshev table not found; building it now …")
        buildTable()
        clearTableCache()
    elif not _isNewer(_CHEB_TABLE_PATH, _IC_PATH):
        print("Chebyshev table is stale; rebuilding it now …")
        buildTable()
        clearTableCache()

    cacheKey = _tableCacheKey()
    if not forceReload and _TABLE_CACHE is not None and _TABLE_CACHE_KEY == cacheKey:
        return _TABLE_CACHE

    with np.load(_CHEB_TABLE_PATH, allow_pickle=True) as data:
        table = {key: data[key] for key in data.files}

    try:
        _validateMetadata(table)
    except RuntimeError:
        print("Chebyshev table metadata is out of date; rebuilding it now …")
        buildTable()
        clearTableCache()
        with np.load(_CHEB_TABLE_PATH, allow_pickle=True) as data:
            table = {key: data[key] for key in data.files}
        _validateMetadata(table)
    _TABLE_CACHE = table
    _TABLE_CACHE_KEY = cacheKey
    return table


def clearTableCache() -> None:
    """Clear the in-memory Chebyshev table cache."""
    global _TABLE_CACHE, _TABLE_CACHE_KEY
    _TABLE_CACHE = None
    _TABLE_CACHE_KEY = None


def getEpochUTC(forceReload: bool = False) -> datetime:
    """Return epoch_utc from initial_conditions.json with mtime-based caching."""
    global _EPOCH_CACHE, _EPOCH_CACHE_KEY

    epochKey = (_IC_PATH.stat().st_mtime_ns,)
    if not forceReload and _EPOCH_CACHE is not None and _EPOCH_CACHE_KEY == epochKey:
        return _EPOCH_CACHE

    data = json.loads(_IC_PATH.read_text())
    epoch = str(data["epoch_utc"])
    if epoch.endswith("Z"):
        epoch = epoch.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(epoch)
    parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)

    _EPOCH_CACHE = parsed
    _EPOCH_CACHE_KEY = epochKey
    return parsed


def _evaluateBatchArrays(tValues: np.ndarray, table: dict) -> dict:
    names = list(table["names"])
    coefficients = table["coefficients"]
    seg_t_a = table["seg_t_a"]
    seg_t_b = table["seg_t_b"]
    total_seconds = float(table["total_seconds"])
    segment_seconds = float(table["segment_seconds"])

    if tValues.size == 0:
        return {name: np.empty((0, 3), dtype=np.float64) for name in names}

    tFlat = np.asarray(tValues, dtype=np.float64).reshape(-1)
    if np.any((tFlat < 0.0) | (tFlat > total_seconds)):
        bad = float(tFlat[((tFlat < 0.0) | (tFlat > total_seconds))][0])
        raise ValueError(
            f"t_sec={bad:.0f} s is outside the simulated range "
            f"[0, {total_seconds:.0f}] s  ({total_seconds / SECONDS_PER_DAY:.1f} days from epoch). "
            "Increase SPAN_DAYS in chebyshev.py (the cached trajectory is extended, "
            "not re-run) or move the epoch with loader.py --epoch."
        )

    segIdx = np.minimum((tFlat / segment_seconds).astype(np.int64), len(seg_t_a) - 1)
    output = {name: np.empty((tFlat.size, 3), dtype=np.float64) for name in names}

    for seg in np.unique(segIdx):
        mask = segIdx == seg
        tSeg = tFlat[mask]
        tA = float(seg_t_a[seg])
        tB = float(seg_t_b[seg])
        tau = np.clip((2.0 * tSeg - (tA + tB)) / (tB - tA), -1.0, 1.0)

        # Coefficients moved to (DEGREE+1, bodies, 3) -> values (bodies, 3, len(tau)).
        values = np.polynomial.chebyshev.chebval(
            tau, np.moveaxis(coefficients[:, :, seg, :], -1, 0)
        )
        for body_idx, name in enumerate(names):
            output[name][mask] = values[body_idx].T

    return output


def evaluateAtBatch(t_seconds) -> dict:
    """Evaluate all bodies at multiple times.

    Parameters
    ----------
    t_seconds : array-like
        1-D sequence of seconds elapsed since simulation epoch.

    Returns
    -------
    dict
        Maps body name -> np.ndarray with shape (len(t_seconds), 3).
    """
    table = _loadTable()
    tValues = np.asarray(t_seconds, dtype=np.float64)
    if tValues.ndim != 1:
        raise ValueError("t_seconds must be a 1-D array-like sequence")
    return _evaluateBatchArrays(tValues, table)


def evaluateAt(t_sec: float) -> dict:
    """
    Return Cartesian positions (metres, ECLIPJ2000 frame) for all bodies
    at *t_sec* seconds after the simulation epoch.

    Parameters
    ----------
    t_sec : float
        Seconds elapsed since the epoch stored in initial_conditions.json.

    Returns
    -------
    dict
        Maps body name (str) → np.ndarray shape (3,) with [x, y, z] in metres.

    Raises
    ------
    ValueError
        If t_sec is outside the simulated time span.
    FileNotFoundError / RuntimeError
        If the Chebyshev table is missing or stale.
    """
    table = _loadTable()
    batch = _evaluateBatchArrays(np.array([float(t_sec)], dtype=np.float64), table)
    return {name: values[0] for name, values in batch.items()}


if __name__ == "__main__":
    buildTable()
