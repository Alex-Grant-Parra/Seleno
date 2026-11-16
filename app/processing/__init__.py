"""Image processing for the album: measuring frames, calibrating and stacking them,
lucky imaging for the Moon and planets, and developing the results.

The heavy work runs in a separate, low-priority worker process (worker.py), started
one job at a time by the runner in jobs.py, so stacking never holds the web server's
memory or CPU. Everything in this package except jobs.py is plain numpy/scipy and
can be used without Flask.
"""
