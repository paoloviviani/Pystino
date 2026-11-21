"""Prepare the directory prometheus_client's multiprocess mode needs.

``prometheus_client`` reads ``PROMETHEUS_MULTIPROC_DIR`` when it is first
imported and never again. The parent of the worker processes only has to export
it before it starts them: uvicorn starts each worker as a fresh (spawned)
interpreter, which imports ``prometheus_client`` for the first time with the
variable already set. The parent itself, which serves nothing, may already have
imported it, and that does no harm. This module imports nothing from the
package, so the CLI can use it without pulling the application in.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def prepare_multiprocess(configured_dir: str) -> str:
    """Create (or empty) the directory, export the variable, and return the path.

    Emptied on every start: a worker's files describe a process that no longer
    exists, and merging last run's counters into this run's would show requests
    that never happened here. The directory is therefore one nothing else may
    share. With no path configured, the operator's ``PROMETHEUS_MULTIPROC_DIR``
    is used, and failing that a fresh temporary directory.
    """
    path = configured_dir or os.environ.get("PROMETHEUS_MULTIPROC_DIR") or ""
    if not path:
        path = tempfile.mkdtemp(prefix="pystino-metrics-")
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    for entry in directory.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(directory)
    return str(directory)
