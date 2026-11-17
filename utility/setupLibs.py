import sys
import os
import subprocess
import json

# Prevent running more than once per process
_ensured = False

# canonicalize name helper (use packaging if available)
try:
    from packaging.utils import canonicalize_name
except Exception:
    def canonicalize_name(n: str) -> str:
        return n.replace("_", "-").lower()

def ensure_requirements():
    global _ensured
    if _ensured:
        return
    _ensured = True

    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "requirements.txt"),
        os.path.join(os.path.dirname(script_dir), "utility", "requirements.txt"),
        os.path.join(os.path.dirname(script_dir), "requirements.txt"),
    ]
    req = next((p for p in candidates if os.path.exists(p)), None)
    if not req:
        print("requirements.txt not found; checked:\n  " + "\n  ".join(candidates))
        return

    # Read requirement lines (skip comments/empty)
    with open(req, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]

    if not lines:
        print("No requirements to process in", req)
        return

    use_packaging = False
    try:
        from packaging.requirements import Requirement as PRequirement
        from packaging.version import Version, InvalidVersion
        use_packaging = True
    except Exception:
        try:
            from pkg_resources import Requirement as PR_Requirement, parse_version
        except Exception:
            PR_Requirement = None
            parse_version = None

    # Get installed packages via pip list --format=json (
    installed = {}
    try:
        out = subprocess.check_output([sys.executable, "-m", "pip", "list", "--format=json"], text=True)
        pip_list = json.loads(out)
        for p in pip_list:
            installed[canonicalize_name(p["name"])] = p["version"]
    except Exception:
        # Fallback: try pkg_resources working_set if pip list fails
        try:
            import pkg_resources
            for dist in pkg_resources.working_set:
                installed[canonicalize_name(dist.project_name)] = str(dist.version)
        except Exception:
            pass

    # Determine which req lines are missing
    to_install = []
    for line in lines:
        parsed_name = None
        spec_ok = True
        try:
            if use_packaging:
                r = PRequirement(line)
                parsed_name = canonicalize_name(r.name)
                installed_ver = installed.get(parsed_name)
                if installed_ver is None:
                    spec_ok = False
                elif r.specifier:
                    try:
                        spec_ok = r.specifier.contains(Version(installed_ver), prereleases=True)
                    except Exception:
                        spec_ok = False
            else:
                # fallback parsing
                if PR_Requirement:
                    r = PR_Requirement.parse(line)
                    raw_name = getattr(r, "project_name", None) or getattr(r, "name", None)
                    parsed_name = canonicalize_name(raw_name) if raw_name else None
                    installed_ver = installed.get(parsed_name) if parsed_name else None
                    if installed_ver is None:
                        spec_ok = False
                    else:
                        # r.specs is list of tuples
                        if getattr(r, "specs", None):
                            for op, ver in r.specs:
                                iv = parse_version(installed_ver)
                                rv = parse_version(ver)
                                if op == "==":
                                    if iv != rv:
                                        spec_ok = False; break
                                elif op == ">=":
                                    if iv < rv:
                                        spec_ok = False; break
                                elif op == "<=":
                                    if iv > rv:
                                        spec_ok = False; break
                                elif op == ">":
                                    if iv <= rv:
                                        spec_ok = False; break
                                elif op == "<":
                                    if iv >= rv:
                                        spec_ok = False; break
                                elif op == "!=":
                                    if iv == rv:
                                        spec_ok = False; break
                                else:
                                    spec_ok = False; break
                else:
                    parsed_name = canonicalize_name(line.split()[0])
                    installed_ver = installed.get(parsed_name)
                    if installed_ver is None:
                        spec_ok = False
        except Exception:
            spec_ok = False

        if not spec_ok:
            to_install.append(line)

    if not to_install:
        print("All requirements satisfied; skipping pip install.")
        return

    # Installing at startup makes boots slow and deploys unpredictable, so it
    # only happens when asked for (AUTO_INSTALL_REQUIREMENTS=True). Otherwise
    # say what's missing and carry on; install it with
    #   venv/bin/pip install -r utility/requirements.txt
    if os.getenv("AUTO_INSTALL_REQUIREMENTS", "False").lower() not in ("1", "true", "yes"):
        print("[WARNING] Requirements not satisfied (not installing; set AUTO_INSTALL_REQUIREMENTS=True to):", to_install)
        return

    print("Installing/upgrading these requirements:", to_install)
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade"] + to_install + ["--disable-pip-version-check"], check=False)
        subprocess.run([sys.executable, "-m", "pip", "check"], check=False)
    except Exception as e:
        print("pip install failed:", e)