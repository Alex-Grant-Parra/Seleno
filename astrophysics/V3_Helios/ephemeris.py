"""
Checkpointed ephemeris for V3 Helios.

Positions for any time from SPAN_YEARS_BACKWARD before the epoch in
initial_conditions.json to SPAN_YEARS_FORWARD after it.

The N-body state (position and velocity of every body) is stored every
CHECKPOINT_DAYS, in one file per ~century (BLOCK_CHECKPOINTS checkpoints)
under ephemeris_cache/.  A request integrates from the nearest checkpoint to
the requested time: at most CHECKPOINT_DAYS / 2, ~100 steps, ~0.6 ms with the
C++ core (~30 ms in Python).  Answers therefore come straight from the
integrator; nothing is interpolated.

Build everything in advance (~2-3 minutes with the C++ core):

    python astrophysics/V3_Helios/ephemeris.py

Otherwise missing blocks are built on demand, outwards from the epoch, the
first time a date in them is requested.  Blocks are rebuilt automatically if
the initial conditions or any setting that changes the trajectory changes.
"""

import argparse
import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

try:
    from .constants import DAYS_PER_JULIAN_YEAR, SECONDS_PER_DAY
    from .engine import DEFAULT_DT, IC_PATH, Propagator
except ImportError:
    from constants import DAYS_PER_JULIAN_YEAR, SECONDS_PER_DAY
    from engine import DEFAULT_DT, IC_PATH, Propagator

CHECKPOINT_DAYS = 16
BLOCK_CHECKPOINTS = 2283            # 2283 x 16 days = 36,528 days, ~100 years per file
SPAN_YEARS_BACKWARD = 2000
SPAN_YEARS_FORWARD = 2000
CACHE_DIR = Path(__file__).resolve().parent / "ephemeris_cache"
MEMORY_BLOCKS = 12                  # blocks kept in memory (~1.2 MB each)

_SCHEMA = 1
CHECKPOINT_SECONDS = CHECKPOINT_DAYS * SECONDS_PER_DAY
_STEPS_PER_CHECKPOINT = int(round(CHECKPOINT_SECONDS / DEFAULT_DT))
if _STEPS_PER_CHECKPOINT * DEFAULT_DT != CHECKPOINT_SECONDS:
    raise ValueError("CHECKPOINT_DAYS must be a whole number of integrator steps")
T_MIN = -SPAN_YEARS_BACKWARD * DAYS_PER_JULIAN_YEAR * SECONDS_PER_DAY
T_MAX = SPAN_YEARS_FORWARD * DAYS_PER_JULIAN_YEAR * SECONDS_PER_DAY

_lock = threading.RLock()           # guards everything below
_propagator = None
_icKey = None
_expectedMetadata = None
_blocks = OrderedDict()             # block index -> (r, v) arrays, LRU order
_epochCache = None


# ---------------------------------------------------------------------------
# Setup and validation
# ---------------------------------------------------------------------------

def _currentIcKey():
    stat = IC_PATH.stat()
    return stat.st_mtime_ns, stat.st_size


def _refresh():
    """(Re)create the propagator and caches when initial_conditions.json changes."""
    global _propagator, _icKey, _expectedMetadata, _epochCache
    key = _currentIcKey()
    if _propagator is not None and key == _icKey:
        return
    # The pole interval must equal the checkpoint spacing so that integrating
    # from a checkpoint towards a request stays inside one pole interval.
    _propagator = Propagator(pole_interval=CHECKPOINT_SECONDS)
    _expectedMetadata = json.dumps({
        "schema": _SCHEMA,
        "ic_sha256": hashlib.sha256(IC_PATH.read_bytes()).hexdigest(),
        "names": _propagator.names,
        "checkpoint_days": CHECKPOINT_DAYS,
        "block_checkpoints": BLOCK_CHECKPOINTS,
        **_propagator.settings(),
    }, sort_keys=True)
    _blocks.clear()
    _epochCache = None
    _icKey = key


def propagator():
    with _lock:
        _refresh()
        return _propagator


def _blockPath(b):
    return CACHE_DIR / f"block_{b:+04d}.npz"


def _blockOf(k):
    return k // BLOCK_CHECKPOINTS


def _blockRange():
    kLo = int(np.floor(T_MIN / CHECKPOINT_SECONDS + 0.5))
    kHi = int(np.floor(T_MAX / CHECKPOINT_SECONDS + 0.5))
    return _blockOf(kLo), _blockOf(kHi)


def _readBlock(b):
    path = _blockPath(b)
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            if str(data["metadata_json"]) != _expectedMetadata or int(data["block"]) != b:
                return None
            return data["r"], data["v"]
    except (OSError, ValueError, KeyError) as exc:
        print(f"Ephemeris block {path.name} unreadable ({exc}); rebuilding it")
        return None


def _writeBlock(b, r, v):
    CACHE_DIR.mkdir(exist_ok=True)
    path = _blockPath(b)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with open(tmp, "wb") as handle:
        np.savez(handle, block=np.array(b), r=r, v=v, metadata_json=np.array(_expectedMetadata))
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _yearsFromEpoch(k):
    return k * CHECKPOINT_DAYS / DAYS_PER_JULIAN_YEAR


def _buildBlock(b):
    """Integrate block b from its neighbour towards the epoch (or the epoch itself)."""
    prop = _propagator
    K, S = BLOCK_CHECKPOINTS, _STEPS_PER_CHECKPOINT
    k0 = b * K
    started = time.perf_counter()
    print(f"Building ephemeris block {b:+d} ({_yearsFromEpoch(k0):+.0f} to "
          f"{_yearsFromEpoch(k0 + K):+.0f} years from epoch, {prop.backend}) …", flush=True)
    if b == 0:
        rh, vh, _, _ = prop.integrate(prop.epoch_r, prop.epoch_v, 0.0, (K - 1) * S, S, +1)
        r = np.concatenate([prop.epoch_r[None], rh])
        v = np.concatenate([prop.epoch_v[None], vh])
    elif b > 0:
        prevR, prevV = _block(b - 1)
        rh, vh, _, _ = prop.integrate(prevR[-1], prevV[-1], (k0 - 1) * CHECKPOINT_SECONDS, K * S, S, +1)
        r, v = rh, vh
    else:
        if b == -1:
            startR, startV = prop.epoch_r, prop.epoch_v
        else:
            nextR, nextV = _block(b + 1)
            startR, startV = nextR[0], nextV[0]
        rh, vh, _, _ = prop.integrate(startR, startV, (k0 + K) * CHECKPOINT_SECONDS, K * S, S, -1)
        r, v = rh[::-1].copy(), vh[::-1].copy()
    _writeBlock(b, r, v)
    print(f"  done in {time.perf_counter() - started:.1f} s", flush=True)
    return r, v


def _block(b):
    """Checkpoints of block b, from memory, disk, or built (with its predecessors)."""
    with _lock:
        _refresh()
        if b in _blocks:
            _blocks.move_to_end(b)
            return _blocks[b]
        lo, hi = _blockRange()
        if not lo <= b <= hi:
            raise ValueError(f"block {b} is outside the configured span")
        data = _readBlock(b)
        if data is None:
            data = _buildBlock(b)
        _blocks[b] = data
        while len(_blocks) > MEMORY_BLOCKS:
            _blocks.popitem(last=False)
        return data


def buildAll():
    """Build every block in the span (outwards from the epoch in both directions)."""
    lo, hi = _blockRange()
    started = time.perf_counter()
    for b in list(range(0, hi + 1)) + list(range(-1, lo - 1, -1)):
        _block(b)
    print(f"Ephemeris ready: {lo:+d}..{hi:+d} blocks, "
          f"{time.perf_counter() - started:.0f} s, cache in {CACHE_DIR}")


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def getEpochEt():
    """Epoch of the initial conditions, TDB seconds past J2000."""
    return propagator().et0


def getEpochUTC():
    """Epoch of the initial conditions as a UTC datetime."""
    global _epochCache
    with _lock:
        _refresh()
        if _epochCache is None:
            text = json.loads(IC_PATH.read_text())["epoch_utc"].replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            _epochCache = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return _epochCache


def coverage():
    """(earliest, latest) supported time, in seconds from the epoch."""
    return T_MIN, T_MAX


def statesAt(t_seconds):
    """Positions and velocities of all bodies at TDB seconds from the epoch.

    Returns (names, r, v) with r and v shaped (len(t_seconds), n_bodies, 3).
    """
    t = np.asarray(t_seconds, dtype=np.float64).reshape(-1)
    prop = propagator()
    n = len(prop.names)
    rOut = np.empty((t.size, n, 3))
    vOut = np.empty((t.size, n, 3))
    bad = ~((t >= T_MIN) & (t <= T_MAX))
    if np.any(bad):
        first = float(t[bad][0])
        epoch = getEpochUTC()
        raise ValueError(
            f"t={first:.0f} s ({first / (DAYS_PER_JULIAN_YEAR * SECONDS_PER_DAY):+.1f} years from the "
            f"{epoch:%Y-%m-%d} epoch) is outside the ephemeris span of -{SPAN_YEARS_BACKWARD} to "
            f"+{SPAN_YEARS_FORWARD} years (SPAN_YEARS_* in ephemeris.py)."
        )
    k = np.floor(t / CHECKPOINT_SECONDS + 0.5).astype(np.int64)
    for kk in np.unique(k):
        b = _blockOf(int(kk))
        blockR, blockV = _block(b)
        r0 = blockR[int(kk) - b * BLOCK_CHECKPOINTS]
        v0 = blockV[int(kk) - b * BLOCK_CHECKPOINTS]
        t0 = float(kk) * CHECKPOINT_SECONDS
        idx = np.nonzero(k == kk)[0]
        forward = idx[t[idx] >= t0]
        backward = idx[t[idx] < t0]
        for group in (forward[np.argsort(t[forward])], backward[np.argsort(-t[backward])]):
            if group.size:
                rOut[group], vOut[group] = prop.propagate(r0, v0, t0, t[group])
    return prop.names, rOut, vOut


def evaluateAtBatch(t_seconds):
    """Positions (m, ECLIPJ2000, barycentric) of every body at TDB seconds from the epoch.

    Returns {name: array (len(t_seconds), 3)}.
    """
    names, r, _ = statesAt(t_seconds)
    return {name: r[:, i, :] for i, name in enumerate(names)}


def evaluateAt(t_sec):
    """Positions (m, ECLIPJ2000, barycentric) at TDB seconds from the epoch: {name: (3,)}."""
    return {name: series[0] for name, series in evaluateAtBatch([float(t_sec)]).items()}


def main():
    parser = argparse.ArgumentParser(description="Build the V3 Helios ephemeris cache.")
    parser.parse_args()
    epoch = getEpochUTC()
    first = epoch + timedelta(seconds=T_MIN)
    last = epoch + timedelta(seconds=T_MAX)
    print(f"Epoch {epoch:%Y-%m-%d}; span {first:%Y-%m-%d} to {last:%Y-%m-%d} "
          f"(backend: {propagator().backend})")
    buildAll()


if __name__ == "__main__":
    main()
