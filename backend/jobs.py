"""
backend/jobs.py
---------------
Subprocess job manager for research runs.

Supervises ``backend/engine_runner.py`` child processes with a FIFO queue,
bounded concurrency, per-run timeouts, graceful cancellation, and bounded
stdout/stderr capture. The module is independent of FastAPI and of the
engine internals: children are launched as argument arrays (never through a
shell) and the first stdout line must be the runner's
``__RUN_ID__<32-hex>`` marker. Exit code zero is necessary but not
sufficient — the run ID and the report file contract are validated too.

Environment propagation: the parent process environment is copied to the
child, with ``PYTHONPATH`` extended to the repository root (so the runner can
import ``research_crew``) and ``RESEARCH_ARTIFACTS_DIR`` pinned to the
configured artifact directory. Credentials from the repository's ``.env``
reach the child through the engine's own ``load_dotenv()`` call, which
resolves relative to the child working directory (the repository root); the
job manager neither reads nor rewrites ``.env``. Sensitive environment
values (``MESH_API_KEY``, ``EXA_API_KEY``, ``TAVILY_API_KEY``,
``OPENAI_API_KEY``) are never logged and are redacted from every job
snapshot, including captured child output.

Nothing is launched at import time; a manager must be constructed and
``start()`` called explicitly.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from backend.config import (
    OPTIONAL_ENGINE_ENV,
    REQUIRED_ENGINE_ENV,
    JobManagerSettings,
)
from backend.engine_runner import RUN_ID_MARKER_PREFIX

_RUN_ID_MARKER_RE = re.compile(rf"^{re.escape(RUN_ID_MARKER_PREFIX)}([0-9a-f]{{32}})$")
_SENSITIVE_ENV_NAMES = tuple(
    dict.fromkeys(REQUIRED_ENGINE_ENV + OPTIONAL_ENGINE_ENV + ("OPENAI_API_KEY",))
)
_REPORT_FILE_TEMPLATE = "research_report_{run_id}.md"
_POLL_INTERVAL_SECONDS = 0.05
_READ_CHUNK_BYTES = 4096
_READER_JOIN_SECONDS = 2.0

_STAGE_MARKERS: tuple[tuple[str, str], ...] = (
    ("[1/5]", "initialising"),
    ("[2/5]", "initialising"),
    ("[3/5]", "initialising"),
    ("[4/5]", "initialising"),
    ("[5/5]", "initialising"),
    ("Kicking off CrewAI pipeline", "pipeline"),
    ("DIAGNOSTIC | Task: Planning", "planning"),
    ("DIAGNOSTIC | Task: Search", "searching"),
    ("DIAGNOSTIC | Task: Validation", "validating"),
    ("DIAGNOSTIC | Task: Extraction", "extracting"),
    ("DIAGNOSTIC | Task: Summary", "synthesizing"),
    ("Pipeline completed", "synthesized"),
    ("Executing bounded verification-to-research feedback loop", "verifying"),
    ("Claim verification completed", "verified"),
    ("Evidence citation audit", "auditing"),
    ("Persisted evidence store", "finalizing"),
)


class JobState(str, Enum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


_TERMINAL_STATES = {
    JobState.COMPLETED,
    JobState.FAILED,
    JobState.CANCELLED,
    JobState.INTERRUPTED,
}


class QueueFullError(RuntimeError):
    """Raised when submitting would exceed the configured queue capacity."""


class ManagerNotStartedError(RuntimeError):
    """Raised when submit() is called before start()."""


class ManagerShutdownError(RuntimeError):
    """Raised when submitting to or starting a shut-down manager."""


class _BoundedBuffer:
    """Byte buffer that keeps only the most recent ``limit`` bytes."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self._buffer += chunk
        if len(self._buffer) > self._limit:
            del self._buffer[: len(self._buffer) - self._limit]
            self.truncated = True

    def text(self) -> str:
        body = bytes(self._buffer).decode("utf-8", "replace")
        if self.truncated:
            return f"[output truncated: showing last {self._limit} bytes]\n{body}"
        return body


class _SecretRedactor:
    """Replaces sensitive environment values inside arbitrary text."""

    def __init__(self, env: Mapping[str, str]) -> None:
        values = {
            str(env[name]) for name in _SENSITIVE_ENV_NAMES if env.get(name)
        }
        self._values = tuple(sorted(values, key=len, reverse=True))

    def redact(self, text: str) -> str:
        for value in self._values:
            if value in text:
                text = text.replace(value, "[REDACTED]")
        return text


@dataclass
class _Job:
    job_id: str
    topic: str
    stdout: _BoundedBuffer
    stderr: _BoundedBuffer
    state: JobState = JobState.QUEUED
    stage: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    exit_code: Optional[int] = None
    run_id: Optional[str] = None
    error: Optional[str] = None
    failure_kind: Optional[str] = None
    marker_checked: bool = False
    marker_invalid: bool = False
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    shutdown_requested: threading.Event = field(default_factory=threading.Event)
    process: Optional[Any] = None
    deadline: Optional[float] = None

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_STATES


def _prepend_path(value: str, existing: Optional[str]) -> str:
    if existing:
        return value + os.pathsep + existing
    return value


class JobManager:
    """FIFO queue and supervisor for engine-runner subprocesses.

    All shared state is guarded by a single condition lock. Dispatching a job
    happens inside the lock (process spawn is fast); the per-job watcher
    thread performs waiting, timeout/cancel handling, termination, and output
    draining outside the lock and only finalizes state under it.
    """

    def __init__(
        self,
        settings: JobManagerSettings,
        *,
        repo_root: Path,
        artifacts_dir: Path,
        env: Optional[Mapping[str, str]] = None,
        popen_factory: Optional[Callable[..., Any]] = None,
        monotonic: Optional[Callable[[], float]] = None,
    ) -> None:
        self._settings = settings
        self._repo_root = Path(repo_root).resolve()
        self._artifacts_dir = Path(artifacts_dir).resolve()
        self._base_env = dict(os.environ if env is None else env)
        self._popen = popen_factory if popen_factory is not None else subprocess.Popen
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._lock = threading.Condition()
        self._jobs: dict[str, _Job] = {}
        self._order: list[str] = []
        self._queue: deque[str] = deque()
        self._running_count = 0
        self._threads: set[threading.Thread] = set()
        self._started = False
        self._shutdown = False
        self._redactor = _SecretRedactor(self._base_env)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Enable job submission. Idempotent."""
        with self._lock:
            if self._shutdown:
                raise ManagerShutdownError("job manager is shut down")
            self._started = True

    def shutdown(self, timeout: Optional[float] = None) -> None:
        """Stop accepting work and terminate all queued and active jobs.

        Queued jobs are cancelled; active jobs are interrupted after their
        children are terminated (graceful TERM, then KILL after the
        configured grace period). Idempotent.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            now = time.time()
            for job_id in list(self._queue):
                job = self._jobs[job_id]
                job.state = JobState.CANCELLED
                job.failure_kind = "shutdown"
                job.stage = "cancelled"
                job.error = "manager shut down before the job started"
                job.finished_at = now
            self._queue.clear()
            for job in self._jobs.values():
                if job.state in (JobState.STARTING, JobState.RUNNING):
                    job.shutdown_requested.set()
            threads = list(self._threads)
            self._lock.notify_all()

        wait_seconds = (
            timeout if timeout is not None else self._settings.kill_grace_seconds + 5.0
        )
        deadline = self._monotonic() + max(0.0, wait_seconds)
        for thread in threads:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)

    # ------------------------------------------------------------------
    # Public job API
    # ------------------------------------------------------------------

    def submit(self, topic: str) -> str:
        """Queue a research run for *topic* and return its stable job ID.

        Raises ``ManagerNotStartedError`` before ``start()``,
        ``ManagerShutdownError`` after shutdown, ``QueueFullError`` when the
        queue is at capacity, and ``ValueError`` for a blank topic.
        """
        topic = (topic or "").strip()
        if not topic:
            raise ValueError("topic must be a non-empty string")
        with self._lock:
            if self._shutdown:
                raise ManagerShutdownError("job manager is shut down")
            if not self._started:
                raise ManagerNotStartedError(
                    "job manager must be started before submitting jobs"
                )
            if len(self._queue) >= self._settings.queue_capacity:
                raise QueueFullError(
                    f"job queue is full ({self._settings.queue_capacity} queued)"
                )
            limit = self._settings.output_limit_bytes
            job = _Job(
                job_id=uuid.uuid4().hex,
                topic=topic,
                stdout=_BoundedBuffer(limit),
                stderr=_BoundedBuffer(limit),
            )
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)
            self._queue.append(job.job_id)
            self._dispatch_locked()
            return job.job_id

    def get(self, job_id: str) -> Optional[dict]:
        """Return a redacted snapshot of one job, or None if unknown."""
        with self._lock:
            job = self._jobs.get(job_id)
            return None if job is None else self._view_locked(job)

    def list_jobs(self) -> list[dict]:
        """Return redacted snapshots of all jobs in submission order."""
        with self._lock:
            return [self._view_locked(self._jobs[job_id]) for job_id in self._order]

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns False for unknown/terminal jobs."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.is_terminal():
                return False
            if job.state == JobState.QUEUED:
                self._queue.remove(job_id)
                job.state = JobState.CANCELLED
                job.failure_kind = "cancelled"
                job.stage = "cancelled"
                job.error = "cancelled before start"
                job.finished_at = time.time()
                self._lock.notify_all()
                return True
            job.cancel_requested.set()
            return True

    def stats(self) -> dict:
        """Small synchronous summary for the API layer and tests."""
        with self._lock:
            return {
                "submitted": len(self._order),
                "queued": len(self._queue),
                "running": self._running_count,
                "active_threads": sum(1 for t in self._threads if t.is_alive()),
                "started": self._started,
                "shutdown": self._shutdown,
            }

    # ------------------------------------------------------------------
    # Dispatch / supervision internals
    # ------------------------------------------------------------------

    def _dispatch_locked(self) -> None:
        while (
            self._queue
            and self._running_count < self._settings.max_concurrent
            and not self._shutdown
        ):
            job = self._jobs[self._queue.popleft()]
            job.state = JobState.STARTING
            job.stage = "starting"
            self._running_count += 1

            interpreter = self._settings.python_executable or sys.executable
            command = [
                interpreter,
                str(self._repo_root / "backend" / "engine_runner.py"),
                "--topic",
                job.topic,
            ]
            child_env = dict(self._base_env)
            child_env["PYTHONPATH"] = _prepend_path(
                str(self._repo_root), child_env.get("PYTHONPATH")
            )
            child_env["RESEARCH_ARTIFACTS_DIR"] = str(self._artifacts_dir)

            try:
                process = self._popen(
                    command,
                    cwd=str(self._repo_root),
                    env=child_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
            except Exception as exc:
                job.state = JobState.FAILED
                job.failure_kind = "spawn_error"
                job.stage = "failed"
                job.error = f"failed to start runner: {type(exc).__name__}: {exc}"
                job.finished_at = time.time()
                self._running_count -= 1
                continue

            job.process = process
            job.started_at = time.time()
            job.deadline = self._monotonic() + self._settings.timeout_seconds
            job.state = JobState.RUNNING
            job.stage = "running"

            watcher = threading.Thread(
                target=self._watch,
                args=(job,),
                name=f"job-watch-{job.job_id[:8]}",
                daemon=True,
            )
            self._threads.add(watcher)
            watcher.start()

    def _watch(self, job: _Job) -> None:
        current = threading.current_thread()
        process = job.process
        if process is None:
            with self._lock:
                self._threads.discard(current)
            self._finalize(job, "internal", "job launched without a process handle")
            return
        stop_reason: Optional[str] = None
        supervision_error: Optional[str] = None
        readers: list[threading.Thread] = []
        try:
            for stream, buffer, is_stdout, suffix in (
                (process.stdout, job.stdout, True, "out"),
                (process.stderr, job.stderr, False, "err"),
            ):
                if stream is None:
                    continue
                reader = threading.Thread(
                    target=self._pump_stream,
                    args=(job, stream, buffer, is_stdout),
                    name=f"job-read-{job.job_id[:8]}-{suffix}",
                    daemon=True,
                )
                readers.append(reader)
            for reader in readers:
                reader.start()

            stop_reason = self._await_process(job, process)
            if stop_reason is not None:
                self._stop_process(process)

            deadline = time.monotonic() + _READER_JOIN_SECONDS
            for reader in readers:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    reader.join(remaining)
        except Exception as exc:
            stop_reason = "internal"
            supervision_error = (
                f"job supervision error: {type(exc).__name__}: {exc}"
            )
            try:
                self._stop_process(process)
            except Exception:
                pass
        finally:
            self._finalize(job, stop_reason, supervision_error)
            with self._lock:
                self._threads.discard(current)

    def _await_process(self, job: _Job, process: Any) -> Optional[str]:
        assert job.deadline is not None
        while True:
            remaining = job.deadline - self._monotonic()
            if remaining <= 0:
                return "timeout"
            if job.cancel_requested.is_set():
                return "cancelled"
            if job.shutdown_requested.is_set():
                return "shutdown"
            try:
                process.wait(timeout=min(_POLL_INTERVAL_SECONDS, remaining))
                return None
            except subprocess.TimeoutExpired:
                continue

    def _stop_process(self, process: Any) -> None:
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=self._settings.kill_grace_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            return
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=max(self._settings.kill_grace_seconds, 1.0))
        except Exception:
            pass

    def _pump_stream(
        self, job: _Job, stream: Any, buffer: _BoundedBuffer, is_stdout: bool
    ) -> None:
        current = threading.current_thread()
        with self._lock:
            self._threads.add(current)
        pending = bytearray()
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_BYTES)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                buffer.append(chunk)
                pending += chunk
                while True:
                    index = pending.find(b"\n")
                    if index < 0:
                        break
                    line = bytes(pending[:index])
                    del pending[: index + 1]
                    self._handle_line(job, line, is_stdout)
            if pending:
                self._handle_line(job, bytes(pending), is_stdout)
        except Exception:
            return
        finally:
            with self._lock:
                self._threads.discard(current)

    def _handle_line(self, job: _Job, raw_line: bytes, is_stdout: bool) -> None:
        text = raw_line.decode("utf-8", "replace").strip()
        with self._lock:
            if is_stdout and not job.marker_checked:
                job.marker_checked = True
                match = _RUN_ID_MARKER_RE.match(text)
                if match:
                    job.run_id = match.group(1)
                else:
                    job.marker_invalid = True
            if is_stdout and job.state == JobState.RUNNING:
                for marker, stage in _STAGE_MARKERS:
                    if marker in text:
                        job.stage = stage
                        break

    def _finalize(
        self, job: _Job, stop_reason: Optional[str], supervision_error: Optional[str]
    ) -> None:
        with self._lock:
            job.finished_at = time.time()
            if job.process is not None:
                job.exit_code = job.process.returncode
            if stop_reason == "cancelled":
                self._set_terminal(
                    job,
                    JobState.CANCELLED,
                    "cancelled",
                    "cancelled",
                    supervision_error or "cancelled by request",
                )
            elif stop_reason == "timeout":
                self._set_terminal(
                    job,
                    JobState.FAILED,
                    "timeout",
                    "timeout",
                    supervision_error
                    or f"timed out after {self._settings.timeout_seconds:g}s",
                )
            elif stop_reason == "shutdown":
                self._set_terminal(
                    job,
                    JobState.INTERRUPTED,
                    "shutdown",
                    "interrupted",
                    supervision_error
                    or "manager shut down while the job was active",
                )
            elif stop_reason == "internal":
                self._set_terminal(
                    job,
                    JobState.FAILED,
                    "internal_error",
                    "failed",
                    supervision_error or "internal supervision error",
                )
            else:
                self._classify_exit(job, supervision_error)
            self._running_count = max(0, self._running_count - 1)
            self._lock.notify_all()
            self._dispatch_locked()

    def _classify_exit(self, job: _Job, supervision_error: Optional[str]) -> None:
        returncode = job.exit_code
        if returncode is None:
            job.exit_code = returncode = -1
        if returncode < 0:
            self._set_terminal(
                job,
                JobState.INTERRUPTED,
                "signal",
                "interrupted",
                supervision_error
                or f"runner terminated by signal {abs(returncode)}",
            )
        elif returncode > 0:
            self._set_terminal(
                job,
                JobState.FAILED,
                "nonzero_exit",
                "failed",
                supervision_error or f"runner exited with code {returncode}",
            )
        elif job.run_id is None:
            kind = "invalid_run_id" if job.marker_invalid else "missing_run_id"
            self._set_terminal(
                job,
                JobState.FAILED,
                kind,
                "failed",
                supervision_error
                or "runner exited 0 without a valid __RUN_ID__ first-line marker",
            )
        else:
            report_path = self._artifacts_dir / _REPORT_FILE_TEMPLATE.format(
                run_id=job.run_id
            )
            if not report_path.is_file():
                self._set_terminal(
                    job,
                    JobState.FAILED,
                    "contract_violation",
                    "failed",
                    supervision_error
                    or f"runner exited 0 but {report_path.name} is missing",
                )
            else:
                job.state = JobState.COMPLETED
                job.failure_kind = None
                job.error = None
                job.stage = "completed"

    def _set_terminal(
        self,
        job: _Job,
        state: JobState,
        failure_kind: str,
        stage: str,
        error: str,
    ) -> None:
        job.state = state
        job.failure_kind = failure_kind
        job.stage = stage
        job.error = error

    def _view_locked(self, job: _Job) -> dict:
        if job.started_at is None:
            elapsed = 0.0
        elif job.finished_at is not None:
            elapsed = job.finished_at - job.started_at
        else:
            elapsed = time.time() - job.started_at
        redact = self._redactor.redact
        return {
            "job_id": job.job_id,
            "topic": job.topic,
            "state": job.state.value,
            "stage": job.stage,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "elapsed_seconds": elapsed,
            "exit_code": job.exit_code,
            "run_id": job.run_id,
            "error": redact(job.error) if job.error else None,
            "failure_kind": job.failure_kind,
            "stdout": redact(job.stdout.text()),
            "stderr": redact(job.stderr.text()),
            "stdout_truncated": job.stdout.truncated,
            "stderr_truncated": job.stderr.truncated,
        }
