"""The processing worker: `python -m app.processing.worker <job folder>`.

Reads <job>/spec.json, runs the job, writes progress to <job>/progress.json as it goes
and the outcome to <job>/result.json. It knows nothing of Flask or the database; the
runner in jobs.py started it and records the result.
"""
import json
import os
import shutil
import sys
import time
import traceback


class Progress:
    """Overall progress 0..1 across weighted stages, written at most twice a second."""

    def __init__(self, path):
        self.path = path
        self.base = 0.0
        self.weight = 0.0
        self.stage_name = ""
        self.last_write = 0.0
        self.value = 0.0
        self.message = "Starting"

    def stage(self, name, weight):
        self.base = min(1.0, self.base + self.weight)
        self.weight = weight
        self.stage_name = name
        self.sub(0.0, self.message)

    def sub(self, fraction, message=None):
        fraction = min(max(float(fraction), 0.0), 1.0)
        self.value = min(0.999, self.base + self.weight * fraction)
        if message:
            self.message = message
        now = time.time()
        if now - self.last_write >= 0.5:
            self.write()

    def child(self, start, span):
        return _Child(self, start, span)

    def write(self, final=False):
        self.last_write = time.time()
        data = {"progress": 1.0 if final else round(self.value, 4), "message": self.message,
                "stage": self.stage_name, "at": self.last_write}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)


class _Child:
    def __init__(self, parent, start, span):
        self.parent, self.start, self.span = parent, start, span

    def sub(self, fraction, message=None):
        self.parent.sub(self.start + self.span * min(max(fraction, 0.0), 1.0), message)


def _write_result(job_dir, result):
    tmp = os.path.join(job_dir, "result.json.tmp")
    with open(tmp, "w") as f:
        json.dump(result, f, default=_json_default)
    os.replace(tmp, os.path.join(job_dir, "result.json"))


def _json_default(value):
    try:
        import numpy as np
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
    except ImportError:
        pass
    raise TypeError(f"Can't save {type(value).__name__} in the result")


def main(job_dir):
    with open(os.path.join(job_dir, "spec.json")) as f:
        spec = json.load(f)
    progress = Progress(os.path.join(job_dir, "progress.json"))
    progress.write()
    started = time.time()
    try:
        from .stack import StackError
        kind = spec["kind"]
        if kind in ("stack", "single"):
            from . import stack
            result = stack.run(spec, progress)
        elif kind == "planetary":
            from . import planetary
            result = planetary.run(spec, progress)
        elif kind == "analyse":
            from . import analysis
            result = analysis.run(spec, progress)
        elif kind == "render":
            from . import render
            result = render.run(spec, progress)
        else:
            raise ValueError(f"Unknown job kind {kind}")
        result["ok"] = True
    except MemoryError:
        result = {"ok": False, "error": "Ran out of memory. Try half-size (super-pixel) debayering or fewer frames."}
    except Exception as e:
        from .stack import StackError
        message = str(e) if isinstance(e, (StackError, ValueError)) else f"Processing failed: {e}"
        result = {"ok": False, "error": message, "trace": traceback.format_exc()[-4000:]}
    result["seconds"] = round(time.time() - started, 1)
    progress.message = "Done" if result.get("ok") else "Failed"
    progress.write(final=True)
    _write_result(job_dir, result)
    tmp_dir = spec.get("tmp_dir")
    if tmp_dir and os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
