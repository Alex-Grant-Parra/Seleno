"""Build the offline server bundle: a copy of this server for a telescope's Pi.

A telescope in offline mode with its hotspot on runs this copy, so phones on
the hotspot (no internet) get the interface, star map and album
(app/offline_mode.py). Telescopes download it from
/api/telescope/offline_bundle (controllers/offline_bundle.py).

    venv/bin/python scripts/build_offline_bundle.py

Writes downloads/offline/seleno-offline.tar.gz and version.json. It holds:
* the code, templates, translations and static files (not the videos);
* the planetary models (V3 Helios with its checkpoint cache, not the 19 GB of
  SPICE kernels it was built from, nor the x86 C++ core: the Pi uses the
  Python integrator, or builds the core itself);
* Bootstrap, Font Awesome and the site's fonts, fetched here once (needs the
  internet) and served from static/vendor/, since the hotspot has none;
* a database with only the star catalogue and the featured targets: no
  accounts, logs, payments or photos. The Pi makes its own account.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, "downloads", "offline")
VENDOR_CACHE = os.path.join(OUT_DIR, "vendor-cache")
BUNDLE_NAME = "seleno-offline.tar.gz"
FORMAT = 1  # the bundle's layout, for the client's installer

CODE = ["Server.py", "babel.cfg", "licence.md", "tos.md", "privacy.md", "THIRD_PARTY_NOTICES.md",
        "app", "controllers", "models", "security", "utility", "templates", "translations", "static",
        "astrophysics", "config"]
SKIP_DIRS = {"__pycache__", ".git", "logs", "videos", "legal", "tests", "backups", "data", "e2e"}
SKIP_FILES = re.compile(r"(\.pyc$|\.so$|^routes\.txt$|^blacklist\.txt$|^api_tokens\.json$|^\.env$)")
# V3 Helios: keep its ephemeris_cache/ (the checkpoints) and initial_conditions.json
CATALOGUE_TABLES = ["HDSTARTable", "NGCtable", "IndexTable", "ObjectNamesTable", "ConstellationLinesTable",
                    "ConstellationsTable", "PlanetsTable", "featured_targets", "model_accuracy_samples"]

# CDN files base.html and style.css load -> where they go under static/vendor/
VENDOR = {
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css": "bootstrap/bootstrap.min.css",
    "https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js": "bootstrap/bootstrap.bundle.min.js",
    "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css": "fontawesome/css/all.min.css",
}
FONTAWESOME_BASE = "https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/"
GOOGLE_FONTS = "https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=Sora:wght@500;600;700&display=swap"
# Google serves woff2 to browsers it knows
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


def log(message):
    print(message, flush=True)


def fetch(url):
    """A URL's bytes, cached in downloads/offline/vendor-cache/."""
    os.makedirs(VENDOR_CACHE, exist_ok=True)
    cached = os.path.join(VENDOR_CACHE, hashlib.sha256(url.encode()).hexdigest()[:24])
    if os.path.exists(cached):
        with open(cached, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    with open(cached, "wb") as f:
        f.write(data)
    return data


def copy_code(stage):
    for name in CODE:
        src = os.path.join(REPO, name)
        dst = os.path.join(stage, name)
        if os.path.isfile(src):
            shutil.copy2(src, dst)
            continue
        if not os.path.isdir(src):
            log(f"  (no {name})")
            continue
        for root, dirs, files in os.walk(src):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            rel = os.path.relpath(root, REPO)
            os.makedirs(os.path.join(stage, rel), exist_ok=True)
            for f in files:
                if SKIP_FILES.search(f):
                    continue
                shutil.copy2(os.path.join(root, f), os.path.join(stage, rel, f))
    # Folders the server expects
    for folder in ("camera_photos", "instance", "security/logs", "templates/legal"):
        os.makedirs(os.path.join(stage, folder), exist_ok=True)
    open(os.path.join(stage, "security", "blacklist.txt"), "a").close()


def vendor_assets(stage):
    vendor = os.path.join(stage, "static", "vendor")
    for url, rel in VENDOR.items():
        path = os.path.join(vendor, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(fetch(url))
    # Font Awesome's webfonts, as its CSS names them (../webfonts/...)
    css = fetch(next(u for u in VENDOR if "font-awesome" in u)).decode("utf-8")
    for ref in sorted(set(re.findall(r"url\((\.\./webfonts/[^)?#]+)", css))):
        name = ref.split("/", 2)[-1]
        path = os.path.join(vendor, "fontawesome", "webfonts", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(fetch(FONTAWESOME_BASE + "webfonts/" + name))
    # The site's fonts: Google's CSS with each font file saved beside it
    fonts_css = fetch(GOOGLE_FONTS).decode("utf-8")
    fonts_dir = os.path.join(vendor, "fonts")
    os.makedirs(fonts_dir, exist_ok=True)

    def local(match):
        url = match.group(1)
        name = hashlib.sha256(url.encode()).hexdigest()[:16] + os.path.splitext(url.split("?")[0])[1]
        with open(os.path.join(fonts_dir, name), "wb") as f:
            f.write(fetch(url))
        return f"url({name})"
    with open(os.path.join(fonts_dir, "fonts.css"), "w") as f:
        f.write(re.sub(r"url\((https://[^)]+)\)", local, fonts_css))

    # Point the copies of base.html and style.css at them
    base = os.path.join(stage, "templates", "base.html")
    with open(base) as f:
        text = f.read()
    for url, rel in VENDOR.items():
        if url not in text:
            raise SystemExit(f"base.html no longer loads {url}: update VENDOR in this script")
        text = text.replace(url, f"/static/vendor/{rel}")
    with open(base, "w") as f:
        f.write(text)
    style = os.path.join(stage, "static", "style.css")
    with open(style) as f:
        text = f.read()
    if GOOGLE_FONTS not in text:
        raise SystemExit("style.css no longer imports the fonts this script vendors: update GOOGLE_FONTS")
    with open(style, "w") as f:
        f.write(text.replace(GOOGLE_FONTS, "/static/vendor/fonts/fonts.css"))
    remaining = [u for u in re.findall(r"https://(?:cdn|cdnjs|unpkg|fonts)\.[^\"')\s]+", open(base).read())]
    if remaining:
        log(f"  warning: base.html still loads {remaining}")


def catalogue_db(stage):
    src_path = os.path.join(REPO, "instance", "Data.db")
    dst_path = os.path.join(stage, "instance", "Data.db")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dst_path)
    for table in CATALOGUE_TABLES:
        row = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            log(f"  (no table {table})")
            continue
        dst.execute(row[0])
        columns = [c[1] for c in src.execute(f'PRAGMA table_info("{table}")')]
        quoted = ", ".join(f'"{c}"' for c in columns)
        marks = ", ".join("?" for _ in columns)
        rows = src.execute(f'SELECT {quoted} FROM "{table}"')
        count = 0
        while True:
            batch = rows.fetchmany(5000)
            if not batch:
                break
            dst.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({marks})', batch)
            count += len(batch)
        for (sql,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                                  (table,)):
            dst.execute(sql)
        log(f"  {table}: {count} rows")
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.close()


def version():
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True,
                                text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=REPO, capture_output=True,
                               text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit, dirty = "", ""
    stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    return f"{stamp}-{commit or 'nogit'}{'-dirty' if dirty else ''}"


def requirements_hash(stage):
    with open(os.path.join(stage, "utility", "requirements.txt"), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="seleno-offline-") as tmp:
        stage = os.path.join(tmp, "seleno-offline")
        os.makedirs(stage)
        log("Copying the code...")
        copy_code(stage)
        log("Fetching the CDN files...")
        vendor_assets(stage)
        log("Copying the star catalogue...")
        catalogue_db(stage)
        info = {"version": version(), "format": FORMAT, "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "requirements_sha256": requirements_hash(stage), "python": "3.10+"}
        with open(os.path.join(stage, "bundle.json"), "w") as f:
            json.dump(info, f, indent=2)
        log("Packing...")
        part = os.path.join(OUT_DIR, BUNDLE_NAME + ".part")
        with tarfile.open(part, "w:gz", compresslevel=6) as tar:
            tar.add(stage, arcname="seleno-offline")
        digest = hashlib.sha256()
        with open(part, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        info.update(sha256=digest.hexdigest(), bytes=os.path.getsize(part))
        os.replace(part, os.path.join(OUT_DIR, BUNDLE_NAME))
        with open(os.path.join(OUT_DIR, "version.json"), "w") as f:
            json.dump(info, f, indent=2)
    log(f"Built {os.path.join(OUT_DIR, BUNDLE_NAME)}: {info['bytes'] / 1e6:.0f} MB, version {info['version']}, "
        f"{time.monotonic() - started:.0f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
