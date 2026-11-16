"""
Calibrate the Earth's tidal time lag against the measured tidal acceleration
of the Moon.

Runs the model with and without tides and fits the difference in the Moon's
geocentric ecliptic longitude with a + b*T + (n_dot / 2) * T^2 (T in
centuries).  n_dot is proportional to the lag, so the lag that reproduces
LUNAR_TIDAL_ACCELERATION_ARCSEC_CY2 follows directly.

    python astrophysics/V3_Helios/calibrate_tides.py [--years 200]

About 12 s with the C++ core.  Put the printed lag into
EARTH_TIDAL_TIME_LAG_S in constants.py.
"""

import argparse

import numpy as np

try:
    from .constants import (ARCSEC_TO_RAD, EARTH_TIDAL_TIME_LAG_S, LUNAR_TIDAL_ACCELERATION_ARCSEC_CY2,
                            SECONDS_PER_JULIAN_CENTURY, DAYS_PER_JULIAN_YEAR, SECONDS_PER_DAY)
    from .engine import Propagator
except ImportError:
    from constants import (ARCSEC_TO_RAD, EARTH_TIDAL_TIME_LAG_S, LUNAR_TIDAL_ACCELERATION_ARCSEC_CY2,
                           SECONDS_PER_JULIAN_CENTURY, DAYS_PER_JULIAN_YEAR, SECONDS_PER_DAY)
    from engine import Propagator

SAMPLE_STEPS = 192          # one sample every 16 days


def moonLongitudes(prop, years, direction):
    steps = int(round(years * DAYS_PER_JULIAN_YEAR * SECONDS_PER_DAY / prop.dt))
    steps -= steps % SAMPLE_STEPS
    r_hist, _, _, _ = prop.integrate(prop.epoch_r, prop.epoch_v, 0.0, steps, SAMPLE_STEPS, direction)
    e, m = prop.names.index("earth"), prop.names.index("moon")
    geo = r_hist[:, m, :] - r_hist[:, e, :]
    t = direction * prop.dt * SAMPLE_STEPS * np.arange(1, len(r_hist) + 1)
    return t, np.arctan2(geo[:, 1], geo[:, 0])


def measureNDot(lag, years, direction=1):
    """n_dot (arcsec/cy^2) produced by the tides with the given lag."""
    with_tides = Propagator(tides=True, tidal_lag=lag, quiet=True)
    without = Propagator(tides=False, quiet=True)
    t, lam_tides = moonLongitudes(with_tides, years, direction)
    _, lam_free = moonLongitudes(without, years, direction)
    diff = np.angle(np.exp(1j * (lam_tides - lam_free))) / ARCSEC_TO_RAD
    T = t / SECONDS_PER_JULIAN_CENTURY
    c2, c1, c0 = np.polyfit(T, diff, 2)
    residual = diff - np.polyval([c2, c1, c0], T)
    return 2.0 * c2, float(np.std(residual)), with_tides.backend


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--years", type=float, default=200.0)
    parser.add_argument("--lag", type=float, default=EARTH_TIDAL_TIME_LAG_S, help="trial lag (s)")
    args = parser.parse_args()

    n_dot, scatter, backend = measureNDot(args.lag, args.years)
    target = LUNAR_TIDAL_ACCELERATION_ARCSEC_CY2
    best = args.lag * target / n_dot
    print(f"backend {backend}, {args.years:.0f} years")
    print(f"lag {args.lag:.2f} s gives n_dot = {n_dot:.3f} \"/cy^2 (fit scatter {scatter:.2f}\")")
    print(f"target n_dot {target} \"/cy^2  ->  EARTH_TIDAL_TIME_LAG_S = {best:.1f}")


if __name__ == "__main__":
    main()
