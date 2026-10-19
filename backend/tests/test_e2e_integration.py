"""
Controlled backend end-to-end integration test.
-----------------------------------------------
Exercises the complete flow with the application's *normal* lifespan and a
real OS subprocess:

    POST /api/runs -> JobManager -> real subprocess.Popen -> fake runner
      -> __RUN_ID__ contract -> report artifact -> artifact discovery
      -> GET /api/runs/{job_id} -> GET /api/runs/{job_id}/report

The subprocess is a purpose-built offline fake runner written into a
temporary repository (``RESEARCH_REPO_ROOT`` override), so
``backend/engine_runner.py`` is never executed against the real repository
and ``research_crew`` / CrewAI / Mesh / Exa / Tavily are never touched. The
artifacts directory is an isolated temporary directory; the real
``evidence_runs/`` is never written to.

Polling is bounded (explicit deadlines) and every scenario asserts on a
terminal state rather than sleeping for a fixed duration.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app

TEST_SECRET = "mesh-integration-secret-9f3a"
RUN_ID_SUCCESS = "cafecafecafecafecafecafecafecafe"
RUN_ID_SLOW = "deadbeefdeadbeefdeadbeefdeadbeef"

REPORT_MARKDOWN = (
    "# Controlled Integration Report\n"
    "\n"
    "- flow: API -> job manager -> subprocess -> runner -> artifacts\n"
    "- runner: offline fake (no research engine, no network)\n"
    "\n"
    "## Findings\n"
    "\n"
    "End-to-end artifact and report retrieval verified.\n"
)

_FAKE_RUNNER_TEMPLATE = '''\
"""Purpose-built offline fake engine runner (never the real engine)."""

import os
import sys
import time

RUN_ID_SUCCESS = "cafecafecafecafecafecafecafecafe"
RUN_ID_SLOW = "deadbeefdeadbeefdeadbeefdeadbeef"
REPORT_MARKDOWN = __REPORT_REPR__


def _topic():
    argv = sys.argv[1:]
    if "--topic" not in argv:
        return ""
    return argv[argv.index("--topic") + 1]


def _artifacts_dir():
    path = os.environ.get("RESEARCH_ARTIFACTS_DIR", "")
    os.makedirs(path, exist_ok=True)
    return path


def main():
    topic = _topic()
    artifacts = _artifacts_dir()
    if topic == "controlled integration topic":
        print("__RUN_ID__" + RUN_ID_SUCCESS, flush=True)
        print("[5/5] Initialising Synthesizer Agent ...", flush=True)
        print("DIAGNOSTIC | Task: Summary | Type: str | Chars: 42", flush=True)
        print("Persisted evidence store to artifacts directory", flush=True)
        report_path = os.path.join(
            artifacts, "research_report_" + RUN_ID_SUCCESS + ".md"
        )
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(REPORT_MARKDOWN)
        return 0
    if topic == "fail controlled":
        print("__RUN_ID__" + RUN_ID_SLOW, flush=True)
        secret = os.environ.get("MESH_API_KEY", "")
        print("debug key: " + secret, file=sys.stderr, flush=True)
        print(
            "simulated engine failure: upstream pipeline aborted",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if topic == "slow run":
        print("__RUN_ID__" + RUN_ID_SLOW, flush=True)
        pid_path = os.path.join(artifacts, "slow_child.pid")
        with open(pid_path, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        time.sleep(300)
        return 0
    print("unsupported topic for the offline fake runner", file=sys.stderr, flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
'''


def _fake_runner_source() -> str:
    return _FAKE_RUNNER_TEMPLATE.replace("__REPORT_REPR__", repr(REPORT_MARKDOWN))


def _setup(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "engine_runner.py").write_text(
        _fake_runner_source(), encoding="utf-8"
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    monkeypatch.setenv("RESEARCH_REPO_ROOT", str(repo))
    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)
    monkeypatch.setenv("MESH_API_KEY", TEST_SECRET)
    monkeypatch.setenv("EXA_API_KEY", "exa-fake-key")
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("JOB_KILL_GRACE_SECONDS", "2")
    monkeypatch.delenv("ENGINE_PYTHON", raising=False)
    return repo, artifacts


def _poll_job(client: TestClient, job_id: str, states: set[str], deadline=15.0):
    limit = time.monotonic() + deadline
    last = None
    while time.monotonic() < limit:
        response = client.get(f"/api/runs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] in states:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"job {job_id} did not reach {states} within {deadline}s; last={last}"
    )


def _wait_for_path(path: Path, deadline=10.0) -> None:
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        if path.exists():
            return
        time.sleep(0.02)
    raise AssertionError(f"{path} was not created within {deadline}s")


def _wait_process_gone(pid: int, deadline=10.0) -> None:
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"child process {pid} is still alive")


def test_end_to_end_success_flow(monkeypatch, tmp_path):
    repo, artifacts = _setup(monkeypatch, tmp_path)
    app = create_app()

    with TestClient(app) as client:
        manager = app.state.job_manager

        response = client.post(
            "/api/runs", json={"topic": "controlled integration topic"}
        )
        assert response.status_code == 202, response.text
        submitted = response.json()
        job_id = submitted["job_id"]
        assert len(job_id) == 32
        assert submitted["origin"] == "manager"

        snapshot = _poll_job(client, job_id, {"completed"})
        assert snapshot["state"] == "completed"
        assert snapshot["stage"] == "completed"
        assert re.fullmatch(r"[0-9a-f]{32}", snapshot["run_id"])
        assert snapshot["run_id"] == RUN_ID_SUCCESS
        assert snapshot["failure_kind"] is None
        assert snapshot["exit_code"] == 0
        assert snapshot["artifacts"]["report_available"] is True

        report_path = artifacts / f"research_report_{snapshot['run_id']}.md"
        assert report_path.is_file()
        assert report_path.read_text(encoding="utf-8") == REPORT_MARKDOWN

        served = client.get(f"/api/runs/{job_id}/report")
        assert served.status_code == 200
        assert served.headers["content-type"].startswith("text/markdown")
        assert served.text == REPORT_MARKDOWN

        listing = client.get("/api/runs").json()
        assert [job["job_id"] for job in listing["jobs"]] == [job_id]
        assert listing["jobs"][0]["run_id"] == RUN_ID_SUCCESS
        historical = {run["run_id"] for run in listing["historical_runs"]}
        assert RUN_ID_SUCCESS not in historical
        assert historical == set()

    assert manager.stats()["shutdown"] is True
    assert manager.get(job_id)["state"] == "completed"


def test_end_to_end_failure_reported_without_leaks(monkeypatch, tmp_path):
    repo, artifacts = _setup(monkeypatch, tmp_path)
    app = create_app()

    with TestClient(app) as client:
        manager = app.state.job_manager

        response = client.post("/api/runs", json={"topic": "fail controlled"})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert TEST_SECRET not in response.text

        snapshot = _poll_job(client, job_id, {"failed"})
        assert snapshot["state"] == "failed"
        assert snapshot["failure_kind"] == "nonzero_exit"
        assert snapshot["exit_code"] == 1
        assert "code 1" in snapshot["error"]
        assert snapshot["run_id"] == RUN_ID_SLOW
        assert "[REDACTED]" in snapshot["stderr"]
        assert (
            "simulated engine failure: upstream pipeline aborted"
            in snapshot["stderr"]
        )

        detail_text = client.get(f"/api/runs/{job_id}").text
        list_text = client.get("/api/runs").text
        for text in (detail_text, list_text):
            assert TEST_SECRET not in text
            assert "Traceback (most recent call last)" not in text

        assert client.get(f"/api/runs/{job_id}/report").status_code == 404
        assert not (artifacts / f"research_report_{RUN_ID_SLOW}.md").exists()

    assert manager.stats()["shutdown"] is True


def test_shutdown_terminates_active_child_and_leaves_none_behind(
    monkeypatch, tmp_path
):
    repo, artifacts = _setup(monkeypatch, tmp_path)
    pid_path = artifacts / "slow_child.pid"
    app = create_app()

    with TestClient(app) as client:
        manager = app.state.job_manager

        response = client.post("/api/runs", json={"topic": "slow run"})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        _wait_for_path(pid_path)
        child_pid = int(pid_path.read_text(encoding="utf-8").strip())
        os.kill(child_pid, 0)

        running = _poll_job(client, job_id, {"running"})
        assert running["state"] == "running"

    assert manager.stats()["shutdown"] is True
    final = manager.get(job_id)
    assert final["state"] == "interrupted"
    assert final["failure_kind"] == "shutdown"

    _wait_process_gone(child_pid)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
