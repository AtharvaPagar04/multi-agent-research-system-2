"""
Offline tests for backend.jobs.
-------------------------------
All children are deterministic fakes injected through ``popen_factory``; the
module-level autouse fixture additionally forbids the real
``subprocess.Popen`` so no test can accidentally launch a process. Artifact
directories are pytest ``tmp_path`` values; the real ``evidence_runs/`` is
only read to prove it is untouched.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from backend import engine_runner
from backend.config import JobManagerSettings
from backend.jobs import (
    JobManager,
    JobState,
    ManagerNotStartedError,
    ManagerShutdownError,
    QueueFullError,
)

REPO_ROOT_FROM_FILE = Path(engine_runner.__file__).resolve().parents[1]
REAL_ARTIFACTS_DIR = REPO_ROOT_FROM_FILE / "evidence_runs"

FAKE_INTERPRETER = "/usr/bin/fake-python"


@pytest.fixture(autouse=True)
def _forbid_real_popen(monkeypatch):
    def _forbidden(*args, **kwargs):
        raise AssertionError("tests must inject a fake popen_factory")

    monkeypatch.setattr(subprocess, "Popen", _forbidden)


class FakeProcess:
    """Minimal Popen-compatible process used to drive the manager."""

    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        delay: float = 0.0,
        ignore_terminate: bool = False,
        on_exit=None,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = None
        self.pid = 99999
        self.terminate_calls = 0
        self.kill_calls = 0
        self._exit_code = exit_code
        self._ready_at = time.monotonic() + delay
        self._ignore_terminate = ignore_terminate
        self._on_exit = on_exit
        self._exit_lock = threading.Lock()

    def _set_returncode(self, code: int) -> None:
        with self._exit_lock:
            if self.returncode is None:
                self.returncode = code
                if self._on_exit is not None:
                    self._on_exit()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.returncode is None:
            if time.monotonic() >= self._ready_at:
                self._set_returncode(self._exit_code)
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    cmd="fake-process", timeout=timeout or 0.0
                )
            time.sleep(0.002)
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        if self.returncode is None and not self._ignore_terminate:
            self._set_returncode(-15)

    def kill(self) -> None:
        self.kill_calls += 1
        if self.returncode is None:
            self._set_returncode(-9)


def make_runner_factory(records: dict, artifacts_dir: Path, behavior=None):
    """Build a fake popen_factory that writes reports and records calls."""
    default = {
        "exit_code": 0,
        "delay": 0.0,
        "stdout_extra": b"",
        "stderr_extra": b"",
        "raw_stdout": None,
        "write_report": True,
        "ignore_terminate": False,
        "error": None,
        "on_exit": None,
    }

    def factory(command, cwd=None, env=None, stdout=None, stderr=None, stdin=None):
        topic = command[command.index("--topic") + 1]
        spec = dict(default)
        if behavior and topic in behavior:
            spec.update(behavior[topic])
        records.setdefault("commands", []).append(list(command))
        records.setdefault("cwds", []).append(cwd)
        records.setdefault("envs", []).append(dict(env) if env is not None else {})
        records.setdefault("started", []).append(topic)
        if spec["error"] is not None:
            raise spec["error"]
        run_id = uuid.uuid4().hex
        records.setdefault("run_ids", {})[topic] = run_id
        if spec["write_report"]:
            report_path = Path(artifacts_dir) / f"research_report_{run_id}.md"
            report_path.write_text("synthetic report", encoding="utf-8")
        raw = spec["raw_stdout"]
        if raw is None:
            raw = (
                f"__RUN_ID__{run_id}\n".encode("utf-8") + spec["stdout_extra"]
            )
        process = FakeProcess(
            stdout=raw,
            stderr=spec["stderr_extra"],
            exit_code=spec["exit_code"],
            delay=spec["delay"],
            ignore_terminate=spec["ignore_terminate"],
            on_exit=spec["on_exit"],
        )
        records.setdefault("processes", {})[topic] = process
        return process

    return factory


def make_manager(
    tmp_path: Path,
    *,
    popen_factory=None,
    env=None,
    max_concurrent: int = 1,
    capacity: int = 16,
    timeout: float = 5.0,
    grace: float = 1.0,
    output_limit: int = 65536,
    python_executable: str | None = FAKE_INTERPRETER,
) -> JobManager:
    settings = JobManagerSettings(
        max_concurrent=max_concurrent,
        queue_capacity=capacity,
        timeout_seconds=timeout,
        kill_grace_seconds=grace,
        output_limit_bytes=output_limit,
        python_executable=python_executable,
    )
    manager = JobManager(
        settings,
        repo_root=REPO_ROOT_FROM_FILE,
        artifacts_dir=tmp_path,
        env=env if env is not None else {},
        popen_factory=popen_factory,
    )
    manager.start()
    return manager


def wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


def wait_for_terminal(manager: JobManager, *job_ids: str, timeout: float = 5.0):
    terminal = {
        JobState.COMPLETED.value,
        JobState.FAILED.value,
        JobState.CANCELLED.value,
        JobState.INTERRUPTED.value,
    }

    def _all_terminal():
        snapshots = [manager.get(job_id) for job_id in job_ids]
        return all(s is not None and s["state"] in terminal for s in snapshots)

    wait_for(_all_terminal, timeout=timeout)


# ---------------------------------------------------------------------------
# Lifecycle, queueing, concurrency
# ---------------------------------------------------------------------------

def test_submit_requires_start_and_valid_topic(tmp_path):
    manager = JobManager(
        JobManagerSettings(),
        repo_root=REPO_ROOT_FROM_FILE,
        artifacts_dir=tmp_path,
        env={},
        popen_factory=make_runner_factory({}, tmp_path),
    )

    with pytest.raises(ManagerNotStartedError):
        manager.submit("anything")

    manager.start()
    with pytest.raises(ValueError):
        manager.submit("   ")

    assert manager.get("missing-job") is None


def test_fifo_order_with_single_concurrency(tmp_path):
    records: dict = {}
    behavior = {"a": {"delay": 0.15}, "b": {"delay": 0.0}, "c": {"delay": 0.0}}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, behavior),
    )

    job_ids = [manager.submit(topic) for topic in ("a", "b", "c")]
    wait_for_terminal(manager, *job_ids)

    assert records["started"] == ["a", "b", "c"]
    assert [manager.get(job_id)["state"] for job_id in job_ids] == [
        "completed",
        "completed",
        "completed",
    ]


def test_concurrency_limit_enforced(tmp_path):
    records: dict = {}
    state = {"active": 0, "max": 0, "finished": 0, "finished_at_c_start": None}

    def on_exit():
        state["finished"] += 1
        state["active"] -= 1

    behavior = {
        topic: {"delay": 0.2, "on_exit": on_exit} for topic in ("a", "b", "c")
    }
    base_factory = make_runner_factory(records, tmp_path, behavior)

    def factory(command, **kwargs):
        topic = command[command.index("--topic") + 1]
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        if topic == "c":
            state["finished_at_c_start"] = state["finished"]
        return base_factory(command, **kwargs)

    manager = make_manager(tmp_path, popen_factory=factory, max_concurrent=2)
    job_ids = [manager.submit(topic) for topic in ("a", "b", "c")]
    wait_for_terminal(manager, *job_ids, timeout=10.0)

    assert state["max"] == 2
    assert state["finished_at_c_start"] >= 1
    assert all(manager.get(job_id)["state"] == "completed" for job_id in job_ids)


def test_queue_full_rejected_explicitly_and_preserved(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"a": {"delay": 0.3}}),
        capacity=2,
    )

    first = manager.submit("a")
    second = manager.submit("b")
    third = manager.submit("c")
    wait_for(lambda: manager.get(second)["state"] == "queued")

    assert manager.stats()["queued"] == 2
    with pytest.raises(QueueFullError, match="full"):
        manager.submit("d")
    assert manager.stats()["queued"] == 2

    wait_for_terminal(manager, first, second, third)
    assert records["started"] == ["a", "b", "c"]
    assert all(
        manager.get(job_id)["state"] == "completed"
        for job_id in (first, second, third)
    )


# ---------------------------------------------------------------------------
# State transitions, run-id and output contract
# ---------------------------------------------------------------------------

def test_state_transitions_and_run_id_parsing(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"t": {"delay": 0.2}}),
    )

    job_id = manager.submit("t")
    snapshot = manager.get(job_id)
    assert snapshot["state"] == "running"
    assert snapshot["started_at"] is not None

    wait_for(lambda: manager.get(job_id)["run_id"] is not None)
    assert manager.get(job_id)["run_id"] == records["run_ids"]["t"]

    wait_for_terminal(manager, job_id)
    final = manager.get(job_id)
    assert final["state"] == "completed"
    assert final["exit_code"] == 0
    assert final["stage"] == "completed"
    assert final["finished_at"] is not None
    assert final["elapsed_seconds"] >= 0.0
    assert final["failure_kind"] is None
    assert final["error"] is None


def test_success_requires_report_contract(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records, tmp_path, {"t": {"write_report": False}}
        ),
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    assert final["state"] == "failed"
    assert final["failure_kind"] == "contract_violation"
    assert "research_report_" in final["error"]


def test_nonzero_exit_fails_with_exit_code(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"t": {"exit_code": 3}}),
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    assert final["state"] == "failed"
    assert final["failure_kind"] == "nonzero_exit"
    assert final["exit_code"] == 3
    assert final["run_id"] == records["run_ids"]["t"]


def test_missing_marker_fails_even_on_zero_exit(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records, tmp_path, {"t": {"raw_stdout": b""}}
        ),
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    assert final["state"] == "failed"
    assert final["failure_kind"] == "missing_run_id"
    assert final["run_id"] is None


def test_invalid_marker_format_fails(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records, tmp_path, {"t": {"raw_stdout": b"__RUN_ID__NOT-A-RUN-ID\n"}}
        ),
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    assert final["state"] == "failed"
    assert final["failure_kind"] == "invalid_run_id"


# ---------------------------------------------------------------------------
# Timeout, cancellation, shutdown
# ---------------------------------------------------------------------------

def test_timeout_terminates_gracefully(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"t": {"delay": 5.0}}),
        timeout=0.15,
        grace=1.0,
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    process = records["processes"]["t"]
    assert final["state"] == "failed"
    assert final["failure_kind"] == "timeout"
    assert final["exit_code"] == -15
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert final["elapsed_seconds"] < 2.0


def test_timeout_forced_kill_after_grace(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records,
            tmp_path,
            {"t": {"delay": 5.0, "ignore_terminate": True}},
        ),
        timeout=0.1,
        grace=0.05,
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    process = records["processes"]["t"]
    assert final["state"] == "failed"
    assert final["failure_kind"] == "timeout"
    assert final["exit_code"] == -9
    assert process.terminate_calls == 1
    assert process.kill_calls == 1


def test_cancel_running_job(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"t": {"delay": 5.0}}),
        timeout=10.0,
        grace=0.5,
    )

    job_id = manager.submit("t")
    wait_for(lambda: manager.get(job_id)["state"] == "running")

    assert manager.cancel(job_id) is True
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    process = records["processes"]["t"]
    assert final["state"] == "cancelled"
    assert final["failure_kind"] == "cancelled"
    assert process.terminate_calls == 1
    assert process.kill_calls == 0


def test_cancel_queued_job_removed_from_queue(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"a": {"delay": 0.3}}),
    )

    first = manager.submit("a")
    second = manager.submit("b")
    wait_for(lambda: manager.get(second)["state"] == "queued")

    assert manager.cancel(second) is True
    assert manager.get(second)["state"] == "cancelled"
    assert manager.stats()["queued"] == 0

    wait_for_terminal(manager, first, second)
    assert manager.get(first)["state"] == "completed"
    assert "b" not in records["started"]


def test_cancel_unknown_or_terminal_returns_false(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path),
    )

    assert manager.cancel("does-not-exist") is False

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)
    assert manager.get(job_id)["state"] == "completed"
    assert manager.cancel(job_id) is False


# ---------------------------------------------------------------------------
# Spawn failures, signals, stages, output, redaction, environment
# ---------------------------------------------------------------------------

def test_spawn_error_marks_failed_and_continues(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records,
            tmp_path,
            {"bad": {"error": OSError("cannot exec interpreter")}},
        ),
    )

    bad_job = manager.submit("bad")
    wait_for_terminal(manager, bad_job)
    bad_final = manager.get(bad_job)
    assert bad_final["state"] == "failed"
    assert bad_final["failure_kind"] == "spawn_error"
    assert "cannot exec interpreter" in bad_final["error"]
    assert bad_final["started_at"] is None

    good_job = manager.submit("good")
    wait_for_terminal(manager, good_job)
    assert manager.get(good_job)["state"] == "completed"
    assert manager.stats()["running"] == 0


def test_unexpected_signal_termination_is_interrupted(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"t": {"exit_code": -9}}),
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    assert final["state"] == "interrupted"
    assert final["failure_kind"] == "signal"
    assert final["exit_code"] == -9
    assert "signal 9" in final["error"]


def test_stage_mapping_from_engine_log_lines(tmp_path):
    records: dict = {}
    stdout_extra = (
        "[5/5] Initialising Synthesizer Agent …\n".encode("utf-8")
        + b"DIAGNOSTIC | Task: Validation | Type: str | Chars: 20 | "
        + b"Validated sources: 2 | Upstream sources: 2\n"
    )
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records, tmp_path, {"t": {"delay": 0.2, "stdout_extra": stdout_extra}}
        ),
    )

    job_id = manager.submit("t")
    wait_for(lambda: manager.get(job_id)["stage"] == "validating")
    wait_for_terminal(manager, job_id)
    assert manager.get(job_id)["stage"] == "completed"


def test_output_truncation_and_stream_separation(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records,
            tmp_path,
            {
                "t": {
                    "stdout_extra": b"o" * 500 + b"\n",
                    "stderr_extra": b"e" * 300,
                }
            },
        ),
        output_limit=128,
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    final = manager.get(job_id)
    assert final["state"] == "completed"
    assert final["run_id"] == records["run_ids"]["t"]
    assert final["stdout_truncated"] is True
    assert final["stderr_truncated"] is True
    assert final["stdout"].startswith("[output truncated")
    stdout_body = final["stdout"].split("\n", 1)[1]
    stderr_body = final["stderr"].split("\n", 1)[1]
    assert set(stdout_body.strip()) == {"o"}
    assert set(stderr_body) == {"e"}


def test_credentials_redacted_in_snapshots(tmp_path):
    records: dict = {}
    secrets = {
        "MESH_API_KEY": "mesh-secret-value-123",
        "EXA_API_KEY": "exa-secret-value-456",
    }
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(
            records,
            tmp_path,
            {
                "t": {
                    "stdout_extra": b"key=mesh-secret-value-123\n",
                    "stderr_extra": b"exa-secret-value-456 leaked\n",
                }
            },
        ),
        env={"PATH": "/usr/bin", **secrets},
    )

    job_id = manager.submit("t")
    wait_for_terminal(manager, job_id)

    snapshot = manager.get(job_id)
    serialized = json.dumps(snapshot)
    assert "mesh-secret-value-123" not in serialized
    assert "exa-secret-value-456" not in serialized
    assert "[REDACTED]" in snapshot["stdout"]
    assert "[REDACTED]" in snapshot["stderr"]
    assert "mesh-secret-value-123" not in json.dumps(manager.list_jobs())


def test_environment_and_command_construction(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path),
        env={"PATH": "/usr/bin", "CUSTOM_VAR": "1"},
    )

    job_id = manager.submit("topic one")
    wait_for_terminal(manager, job_id)

    command = records["commands"][0]
    assert command == [
        FAKE_INTERPRETER,
        str(REPO_ROOT_FROM_FILE / "backend" / "engine_runner.py"),
        "--topic",
        "topic one",
    ]
    assert records["cwds"][0] == str(REPO_ROOT_FROM_FILE)
    child_env = records["envs"][0]
    assert child_env["PATH"] == "/usr/bin"
    assert child_env["CUSTOM_VAR"] == "1"
    assert child_env["RESEARCH_ARTIFACTS_DIR"] == str(tmp_path)
    assert child_env["PYTHONPATH"].split(os.pathsep)[0] == str(REPO_ROOT_FROM_FILE)


def test_default_python_interpreter_used_when_unset(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path),
        python_executable=None,
    )

    job_id = manager.submit("topic")
    wait_for_terminal(manager, job_id)

    assert records["commands"][0][0] == sys.executable


# ---------------------------------------------------------------------------
# Shutdown and isolation
# ---------------------------------------------------------------------------

def test_shutdown_stops_children_and_drains_queue(tmp_path):
    records: dict = {}
    manager = make_manager(
        tmp_path,
        popen_factory=make_runner_factory(records, tmp_path, {"a": {"delay": 5.0}}),
        timeout=10.0,
        grace=0.5,
    )

    active = manager.submit("a")
    queued = manager.submit("b")
    wait_for(lambda: manager.get(queued)["state"] == "queued")

    manager.shutdown(timeout=2.0)

    active_final = manager.get(active)
    queued_final = manager.get(queued)
    assert active_final["state"] == "interrupted"
    assert active_final["failure_kind"] == "shutdown"
    assert queued_final["state"] == "cancelled"
    assert queued_final["failure_kind"] == "shutdown"
    assert records["processes"]["a"].terminate_calls == 1
    stats = manager.stats()
    assert stats["queued"] == 0
    assert stats["running"] == 0
    assert stats["active_threads"] == 0

    with pytest.raises(ManagerShutdownError):
        manager.submit("c")
    with pytest.raises(ManagerShutdownError):
        manager.start()

    manager.shutdown(timeout=1.0)


def test_manager_never_writes_to_real_evidence_runs(tmp_path):
    if not REAL_ARTIFACTS_DIR.is_dir():
        pytest.skip("real evidence_runs/ directory not present")
    before = sorted(p.name for p in REAL_ARTIFACTS_DIR.iterdir())
    records: dict = {}
    manager = make_manager(
        tmp_path, popen_factory=make_runner_factory(records, tmp_path)
    )

    job_id = manager.submit("isolation topic")
    wait_for_terminal(manager, job_id)

    after = sorted(p.name for p in REAL_ARTIFACTS_DIR.iterdir())
    assert manager.get(job_id)["state"] == "completed"
    assert after == before
