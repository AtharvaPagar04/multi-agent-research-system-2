"""
Import-isolation check for backend.jobs.
----------------------------------------
Kept in its own module: test_jobs.py forbids the real ``subprocess.Popen``
module-wide, which would also break ``subprocess.run`` used here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from backend import engine_runner

REPO_ROOT_FROM_FILE = Path(engine_runner.__file__).resolve().parents[1]

IMPORT_ISOLATION_CODE = """
import sys
import threading

import backend.jobs

assert threading.active_count() == 1, "importing backend.jobs started a thread"
assert "research_crew" not in sys.modules, "engine imported at import time"
assert "crewai" not in sys.modules, "crewai imported at import time"
print("jobs-import-isolation-ok")
"""


def test_importing_jobs_does_not_start_threads_or_import_engine():
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_ISOLATION_CODE],
        cwd=REPO_ROOT_FROM_FILE,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "jobs-import-isolation-ok" in result.stdout
