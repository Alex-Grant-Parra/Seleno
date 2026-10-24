# Tests

Nothing here touches `instance/Data.db`, `camera_photos/` or the running server: every
test runs the controllers on a stub Flask app (`harness.py`) with a throwaway SQLite
database in a temporary folder, and fake telescopes in place of real sockets.

## Unit and API tests (about 10 s)

```bash
cd tests
../venv/bin/python -m unittest test_commands test_uploads_plans test_liveview test_album_api test_interface_api test_projects_api test_star_search
```

| File | Covers |
|---|---|
| `test_commands.py` | `/interface/command` and the telescope commands behind it |
| `test_uploads_plans.py` | resumable (tus) uploads, capture plans, the album's plan views |
| `test_liveview.py` | the live-view relay |
| `test_album_api.py` | the album's library index, thumbnails, flags, metrics, stacks, downloads |
| `test_interface_api.py` | `app/sky.py`, the sky, targets and latest-captures endpoints, capability checks |
| `test_projects_api.py` | multi-night projects: chunked uploads of the totals, commits, rebuild checkpoints |
| `test_star_search.py` | the star map's name search: typos, other languages, exact-only catalogue numbers, suggestions |

## The processing engine (Node, about 10 s)

The in-browser engine (`static/js/processing/`) is tested in Node against the Python
reference (`app/processing/`) and on synthetic frames. See `processing/README.md`.

```bash
venv/bin/python tests/processing/oracle.py /tmp/oracle <dir with scipy>   # once: Python's answers
ORACLE=/tmp/oracle node --test tests/processing/*.test.mjs
```

## End-to-end, in headless Chrome (about a minute)

`e2e/server.py` serves the real interface and album pages with synthetic photos (star
fields drawn in `e2e/fixtures.py`, nothing real), a fake telescope that "uploads" a photo
when asked to take one, and a fake telescope streaming an H.264 focus sweep into live view.
`e2e/run.py` drives Chrome through the scenarios (`e2e/scenario_*.js`) and saves
screenshots.

```bash
E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/run.py                 # interface and album
E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/run.py --dark          # in the dark theme
E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/run.py --width=390 narrow narrow_album
E2E_CHROME=/path/to/chrome venv/bin/python tests/e2e/run.py engine          # real stacking in the browser (about 30 s)
```

The `engine` scenario stacks the fixture night in Web Workers (with OPFS), measures
photos, opens the live editor, starts a project, and lucky-images a SER video. With
`E2E_CR2=<a Canon CR2>` it also reads a real RAW (the CC0 sample in
`processing/README.md`).

Chrome needs WebCodecs H.264, so use Google Chrome rather than Chromium. If none is
installed, unpack one without installing it:

```bash
curl -O https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
dpkg-deb -x google-chrome-stable_current_amd64.deb chrome-root
E2E_CHROME=$PWD/chrome-root/opt/google/chrome/chrome ...
```

Screenshots go to `tests/e2e/out/` (or `E2E_OUT`). To click around the stub site by hand:
`venv/bin/python tests/e2e/server.py`, then open
<http://127.0.0.1:18090/e2e-login?next=/interface/>.
