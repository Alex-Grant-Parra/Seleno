"""
Build the helios_core C++ extension next to this file.

    python astrophysics/V3_Helios/build_core.py

Needs g++ (C++17), the Python development headers and pybind11
(pip install pybind11).  If the module is not built, or is older than its
source, Helios falls back to the pure-Python integrator automatically.
"""

import subprocess
import sys
import sysconfig
from pathlib import Path

import pybind11

BASE = Path(__file__).resolve().parent
SOURCE = BASE / "cpp" / "helios_core.cpp"
TARGET = BASE / ("helios_core" + sysconfig.get_config_var("EXT_SUFFIX"))

# -O3 for speed, but no -ffast-math and no fused multiply-add contraction, so
# the arithmetic follows the same IEEE rules as the NumPy reference version.
FLAGS = ["-O3", "-ffp-contract=off", "-std=c++17", "-shared", "-fPIC", "-Wall", "-Wextra"]


def build():
    cmd = [
        "g++", *FLAGS,
        f"-I{pybind11.get_include()}",
        f"-I{sysconfig.get_paths()['include']}",
        str(SOURCE), "-o", str(TARGET),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True)
    print(f"Built {TARGET.name}")


if __name__ == "__main__":
    try:
        build()
    except subprocess.CalledProcessError as exc:
        sys.exit(exc.returncode)
