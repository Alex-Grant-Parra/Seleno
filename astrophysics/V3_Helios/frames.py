"""
Reference-frame helpers for V3 Helios.

The simulation works in ECLIPJ2000 (the J2000 ecliptic and equinox, as SPICE
defines it).  This module converts to the J2000 equator and applies IAU 2006
precession (Capitaine et al. 2003) to get the Earth's pole and the mean
equator and equinox of date.
"""

import numpy as np

try:
    from .constants import (ARCSEC_TO_RAD, OBLIQUITY_J2000_RAD, PRECESSION_CHI_A_ARCSEC,
                            PRECESSION_OMEGA_A_ARCSEC, PRECESSION_PSI_A_ARCSEC)
except ImportError:
    from constants import (ARCSEC_TO_RAD, OBLIQUITY_J2000_RAD, PRECESSION_CHI_A_ARCSEC,
                           PRECESSION_OMEGA_A_ARCSEC, PRECESSION_PSI_A_ARCSEC)


def _rot1(angle):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, s], [0.0, -s, c]])


def _rot3(angle):
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])


# ECLIPJ2000 -> J2000 equatorial is a rotation by -obliquity about the x axis.
_ECLIPTIC_TO_EQUATORIAL = _rot1(-OBLIQUITY_J2000_RAD)


def eclipticToEquatorial(vec):
    """ECLIPJ2000 -> J2000 equatorial, for a (3,) vector or an (n, 3) array."""
    return np.asarray(vec) @ _ECLIPTIC_TO_EQUATORIAL.T


def polynomial(coeffs, T):
    """Evaluate sum(coeffs[k] * T**k) by Horner's rule (same order as the C++ core)."""
    value = 0.0
    for c in reversed(coeffs):
        value = value * T + c
    return value


def precessionAngles(T):
    """IAU 2006 psi_A, omega_A, chi_A in radians, T in Julian centuries TT from J2000."""
    psi = polynomial(PRECESSION_PSI_A_ARCSEC, T) * ARCSEC_TO_RAD
    omega = OBLIQUITY_J2000_RAD + polynomial(PRECESSION_OMEGA_A_ARCSEC, T) * ARCSEC_TO_RAD
    chi = polynomial(PRECESSION_CHI_A_ARCSEC, T) * ARCSEC_TO_RAD
    return psi, omega, chi


def earthPoleEclipJ2000(T, psi_coeffs=PRECESSION_PSI_A_ARCSEC, omega_coeffs=PRECESSION_OMEGA_A_ARCSEC):
    """Unit vector of the Earth's mean pole of date, in ECLIPJ2000.

    The pole sits at angle omega_A from the J2000 ecliptic pole, and its
    longitude falls behind by psi_A as the equinox precesses westwards.
    """
    psi = polynomial(psi_coeffs, T) * ARCSEC_TO_RAD
    omega = OBLIQUITY_J2000_RAD + polynomial(omega_coeffs, T) * ARCSEC_TO_RAD
    return np.array([np.sin(omega) * np.sin(psi), np.sin(omega) * np.cos(psi), np.cos(omega)])


def precessionMatrix(T):
    """Rotation from J2000 equatorial to the mean equator and equinox of date.

    P = R3(chi_A) R1(-omega_A) R3(-psi_A) R1(eps0)  (the IAU 2006 four-rotation form).
    """
    psi, omega, chi = precessionAngles(T)
    return _rot3(chi) @ _rot1(-omega) @ _rot3(-psi) @ _rot1(OBLIQUITY_J2000_RAD)
