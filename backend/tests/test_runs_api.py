"""
Offline tests for the REST API in backend.app.
----------------------------------------------
Every test drives the real JobManager through the app's lifespan with an
injected fake subprocess boundary (``make_runner_factory`` from
``backend.tests.test_jobs``); ``backend/engine_runner.py`` is never executed
against the real engine and no network calls are made. Request-scoped
artifact directories live under pytest ``tmp_path``.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from backend import engine_runner
from backend.app import app as module_app
from backend.app import create_app
from backend.config import JobManagerSettings, load_config
from backend.jobs import JobManager
from backend.tests.test_jobs import make_runner_factory

REPO_ROOT_FROM_FILE = Path(engine_runner.__file__).resolve().parents[1]
FAKE_INTERPRETER = "/usr/bin/fake-python"
MESH_SECRET = "mesh-secret-value-123"

TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}

EXPECTED_SNAPSHOT_KEYS = {
    "origin",
    "job_id",
    "topic",
    "state",
    "stage",
    "created_at",
    "started_at",
    "finished_at",
    "elapsed_seconds",
    "exit_code",
    "run_id",
    "error",
    "failure_kind",
    "stdout",
    "stderr",
    "stdout_truncated",
    "stderr_truncated",
    "artifacts",
}

EXPECTED_ARTIFACT_KEYS = {
    "report_available",
    "evidence_available",
    "claim_verifications_available",
    "feedback_loop_available",
}


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "research_crew").mkdir(parents=True)
    (repo / "research_crew" / "main.py").write_text(
        "# synthetic engine module\n", encoding="utf-8"
    )
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "engine_runner.py").write_text(
        "# synthetic runner\n", encoding="utf-8"
    )
    (repo / "evidence_runs").mkdir(parents=True)
    return repo


def _configure_env(monkeypatch, repo: Path) -> None:
    monkeypatch.setenv("RESEARCH_REPO_ROOT", str(repo))
    monkeypatch.delenv("RESEARCH_ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)
    monkeypatch.setenv("MESH_API_KEY", MESH_SECRET)
    monkeypatch.setenv("EXA_API_KEY", "exa-fake-key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)


def _artifacts_dir(repo: Path) -> Path:
    return repo / "evidence_runs"


def _make_app(
    records: dict,
    behavior: dict | None = None,
    *,
    max_concurrent: int = 1,
    capacity: int = 16,
    timeout: float = 5.0,
    grace: float = 0.5,
):
    def manager_factory() -> JobManager:
        config = load_config()
        settings = JobManagerSettings(
            max_concurrent=max_concurrent,
            queue_capacity=capacity,
            timeout_seconds=timeout,
            kill_grace_seconds=grace,
            output_limit_bytes=65536,
            python_executable=FAKE_INTERPRETER,
        )
        return JobManager(
            settings,
            repo_root=config.repo_root,
            artifacts_dir=config.artifacts_dir,
            env={"PATH": "/usr/bin", "MESH_API_KEY": MESH_SECRET},
            popen_factory=make_runner_factory(
                records, config.artifacts_dir, behavior
            ),
        )

    return create_app(manager_factory=manager_factory)


def _wait_for_state(client: TestClient, job_id: str, states: set[str], timeout=5.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{job_id}")
        assert response.status_code == 200, response.text
        last = response.json()
        if last["state"] in states:
            return last
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} never reached {states}; last={last}")


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

def test_submit_valid_topic_returns_202_with_snapshot(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        response = client.post("/api/runs", json={"topic": "state space models"})

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["origin"] == "manager"
        assert len(body["job_id"]) == 32
        assert body["state"] in {"queued", "running"}
        assert body["topic"] == "state space models"

        final = _wait_for_state(client, body["job_id"], {"completed"})
        assert final["run_id"] == records["run_ids"]["state space models"]
        assert final["failure_kind"] is None
        assert final["exit_code"] == 0
        assert final["artifacts"]["report_available"] is True
        assert final["artifacts"]["evidence_available"] is False


def test_submit_trims_topic_whitespace(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        response = client.post("/api/runs", json={"topic": "   padded topic   "})
        assert response.status_code == 202
        job_id = response.json()["job_id"]

        final = _wait_for_state(client, job_id, {"completed"})
        assert final["topic"] == "padded topic"
        assert "padded topic" in records["run_ids"]


def test_submit_invalid_topics_rejected(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        assert client.post("/api/runs", json={}).status_code == 422
        assert client.post("/api/runs", json={"topic": ""}).status_code == 422
        assert client.post("/api/runs", json={"topic": "   "}).status_code == 422
        assert client.post("/api/runs", json={"topic": 123}).status_code == 422
        assert (
            client.post("/api/runs", json={"topic": "x" * 501}).status_code == 422
        )
        not_json = client.post(
            "/api/runs",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert not_json.status_code == 422

        at_limit = client.post("/api/runs", json={"topic": "x" * 500})
        assert at_limit.status_code == 202

        assert records["started"] == ["x" * 500]


def test_queue_full_returns_429(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    slow = {"slow a": {"delay": 30.0}, "slow b": {"delay": 30.0}}

    with TestClient(_make_app(records, slow, capacity=1, timeout=30.0)) as client:
        first = client.post("/api/runs", json={"topic": "slow a"})
        second = client.post("/api/runs", json={"topic": "slow b"})
        third = client.post("/api/runs", json={"topic": "slow c"})

        assert first.status_code == 202
        assert second.status_code == 202
        assert third.status_code == 429
        assert "full" in third.json()["detail"]

        queued = client.get(f"/api/runs/{second.json()['job_id']}").json()
        assert queued["state"] == "queued"

        assert client.post(
            f"/api/runs/{second.json()['job_id']}/cancel"
        ).json()["accepted"] is True
        assert client.post(
            f"/api/runs/{first.json()['job_id']}/cancel"
        ).json()["accepted"] is True
        _wait_for_state(client, first.json()["job_id"], TERMINAL_STATES)


# ---------------------------------------------------------------------------
# Listing, detail, historical discovery
# ---------------------------------------------------------------------------

def test_list_empty_then_manager_jobs_in_order(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        empty = client.get("/api/runs")
        assert empty.status_code == 200
        assert empty.json() == {"jobs": [], "historical_runs": []}

        alpha = client.post("/api/runs", json={"topic": "alpha"}).json()["job_id"]
        beta = client.post("/api/runs", json={"topic": "beta"}).json()["job_id"]
        _wait_for_state(client, alpha, TERMINAL_STATES)
        _wait_for_state(client, beta, TERMINAL_STATES)

        body = client.get("/api/runs").json()
        assert [job["job_id"] for job in body["jobs"]] == [alpha, beta]
        assert all(job["origin"] == "manager" for job in body["jobs"])
        assert {job["state"] for job in body["jobs"]} == {"completed"}

        owned = {job["run_id"] for job in body["jobs"]}
        assert owned == {records["run_ids"]["alpha"], records["run_ids"]["beta"]}
        historical_ids = {run["run_id"] for run in body["historical_runs"]}
        assert owned.isdisjoint(historical_ids)


def test_historical_discovery_strict_and_without_invented_fields(
    monkeypatch, tmp_path
):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    artifacts = _artifacts_dir(repo)
    run_a, run_b, run_c, run_d = "a" * 32, "b" * 32, "c" * 32, "d" * 32
    (artifacts / f"evidence_{run_a}.json").write_text("{}", encoding="utf-8")
    (artifacts / f"claim_verifications_{run_b}.json").write_text(
        "{}", encoding="utf-8"
    )
    (artifacts / f"feedback_loop_{run_c}.json").write_text("{}", encoding="utf-8")
    (artifacts / f"research_report_{run_d}.md").write_text(
        "# report", encoding="utf-8"
    )
    (artifacts / "evidence_NOTHEX.json").write_text("{}", encoding="utf-8")
    (artifacts / f"evidence_{'a' * 31}.json").write_text("{}", encoding="utf-8")
    (artifacts / f"evidence_{run_a}.json.bak").write_text("{}", encoding="utf-8")
    nested = artifacts / "nested"
    nested.mkdir()
    (nested / f"evidence_{'9' * 32}.json").write_text("{}", encoding="utf-8")

    with TestClient(_make_app({})) as client:
        body = client.get("/api/runs").json()

    assert body["jobs"] == []
    historical = {run["run_id"]: run for run in body["historical_runs"]}
    assert set(historical) == {run_a, run_b, run_c, run_d}

    assert set(historical[run_a]) == {"origin", "run_id", "artifacts"}
    assert set(historical[run_a]["artifacts"]) == EXPECTED_ARTIFACT_KEYS
    assert historical[run_a]["origin"] == "artifact"
    assert historical[run_a]["artifacts"] == {
        "report_available": False,
        "evidence_available": True,
        "claim_verifications_available": False,
        "feedback_loop_available": False,
    }
    assert historical[run_b]["artifacts"]["claim_verifications_available"] is True
    assert historical[run_c]["artifacts"]["feedback_loop_available"] is True
    assert historical[run_d]["artifacts"]["report_available"] is True


def test_get_unknown_job_returns_404(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)

    with TestClient(_make_app({})) as client:
        assert client.get(f"/api/runs/{'0' * 32}").status_code == 404
        assert client.get("/api/runs/not-a-job-id").status_code == 404
        assert client.get("/api/runs/12345").status_code == 404


def test_get_artifact_only_run_detail(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    run_x = "e" * 32
    (_artifacts_dir(repo) / f"evidence_{run_x}.json").write_text(
        "{}", encoding="utf-8"
    )

    with TestClient(_make_app({})) as client:
        response = client.get(f"/api/runs/{run_x}")

    assert response.status_code == 200
    body = response.json()
    assert body["origin"] == "artifact"
    assert body["job_id"] == run_x
    assert body["run_id"] == run_x
    assert body["artifacts"]["evidence_available"] is True
    assert body["artifacts"]["report_available"] is False
    assert body["state"] is None
    assert body["stage"] is None
    assert body["created_at"] is None
    assert body["started_at"] is None
    assert body["finished_at"] is None
    assert body["elapsed_seconds"] is None
    assert body["exit_code"] is None
    assert body["topic"] is None


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_queued_job(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    slow = {"slow a": {"delay": 30.0}}

    with TestClient(_make_app(records, slow, capacity=2, timeout=30.0)) as client:
        first = client.post("/api/runs", json={"topic": "slow a"}).json()["job_id"]
        second = client.post("/api/runs", json={"topic": "slow b"}).json()["job_id"]
        assert client.get(f"/api/runs/{second}").json()["state"] == "queued"

        cancelled = client.post(f"/api/runs/{second}/cancel")
        assert cancelled.status_code == 200
        body = cancelled.json()
        assert body["accepted"] is True
        assert body["job"]["state"] == "cancelled"
        assert body["job"]["failure_kind"] == "cancelled"

        again = client.post(f"/api/runs/{second}/cancel").json()
        assert again["accepted"] is False
        assert again["job"]["state"] == "cancelled"

        assert "slow b" not in records["started"]

        assert client.post(f"/api/runs/{first}/cancel").json()["accepted"] is True
        _wait_for_state(client, first, TERMINAL_STATES)


def test_cancel_running_job(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    slow = {"slow run": {"delay": 30.0}}

    with TestClient(_make_app(records, slow, timeout=30.0)) as client:
        job_id = client.post("/api/runs", json={"topic": "slow run"}).json()[
            "job_id"
        ]
        _wait_for_state(client, job_id, {"running"})

        response = client.post(f"/api/runs/{job_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is True
        assert body["job"]["state"] in {"running", "cancelled"}

        final = _wait_for_state(client, job_id, {"cancelled"})
        assert final["failure_kind"] == "cancelled"
        assert final["exit_code"] == -15


def test_cancel_terminal_and_unknown_job(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        job_id = client.post("/api/runs", json={"topic": "done"}).json()["job_id"]
        _wait_for_state(client, job_id, {"completed"})

        response = client.post(f"/api/runs/{job_id}/cancel")
        assert response.status_code == 200
        body = response.json()
        assert body["accepted"] is False
        assert body["job"]["state"] == "completed"

        assert client.post(f"/api/runs/{'0' * 32}/cancel").status_code == 404
        assert client.post("/api/runs/not-a-job-id/cancel").status_code == 404


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def test_report_for_completed_job(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        job_id = client.post("/api/runs", json={"topic": "report me"}).json()[
            "job_id"
        ]
        _wait_for_state(client, job_id, {"completed"})

        response = client.get(f"/api/runs/{job_id}/report")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text == "synthetic report"


def test_report_unavailable_for_running_and_failed_jobs(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    behavior = {"slow": {"delay": 30.0}, "bad": {"exit_code": 3}}

    with TestClient(_make_app(records, behavior, timeout=30.0)) as client:
        running = client.post("/api/runs", json={"topic": "slow"}).json()["job_id"]
        assert client.get(f"/api/runs/{running}/report").status_code == 404
        client.post(f"/api/runs/{running}/cancel")

        bad = client.post("/api/runs", json={"topic": "bad"}).json()["job_id"]
        failed = _wait_for_state(client, bad, {"failed"})
        assert failed["failure_kind"] == "nonzero_exit"

        report_path = (
            _artifacts_dir(repo)
            / f"research_report_{records['run_ids']['bad']}.md"
        )
        assert report_path.is_file()
        assert client.get(f"/api/runs/{bad}/report").status_code == 404


def test_report_artifact_only_and_invalid_run_ids(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    artifacts = _artifacts_dir(repo)
    run_r = "7" * 32
    run_e = "e" * 32
    (artifacts / f"research_report_{run_r}.md").write_text(
        "artifact-only report", encoding="utf-8"
    )
    (artifacts / f"evidence_{run_e}.json").write_text("{}", encoding="utf-8")

    with TestClient(_make_app({})) as client:
        served = client.get(f"/api/runs/{run_r}/report")
        assert served.status_code == 200
        assert served.text == "artifact-only report"

        assert client.get(f"/api/runs/{'0' * 32}/report").status_code == 404
        assert client.get("/api/runs/not-a-job-id/report").status_code == 404
        assert client.get(f"/api/runs/{run_e}/report").status_code == 404


def test_report_scoped_to_validated_run_id(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    behavior = {"mine": {"delay": 0.0}}

    with TestClient(_make_app(records, behavior)) as client:
        job_id = client.post("/api/runs", json={"topic": "mine"}).json()["job_id"]
        _wait_for_state(client, job_id, {"completed"})

        run_id = records["run_ids"]["mine"]
        (
            _artifacts_dir(repo) / f"research_report_{run_id}.md"
        ).unlink()
        other_run = "f" * 32
        (
            _artifacts_dir(repo) / f"research_report_{other_run}.md"
        ).write_text("other run report", encoding="utf-8")

        assert client.get(f"/api/runs/{job_id}/report").status_code == 404
        other = client.get(f"/api/runs/{other_run}/report")
        assert other.status_code == 200
        assert other.text == "other run report"


# ---------------------------------------------------------------------------
# Serialization, redaction, lifecycle, CORS
# ---------------------------------------------------------------------------

def test_snapshot_serialization_and_secret_redaction(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    behavior = {
        "leaky": {
            "stdout_extra": f"token={MESH_SECRET}\n".encode("utf-8"),
        }
    }

    with TestClient(_make_app(records, behavior)) as client:
        job_id = client.post("/api/runs", json={"topic": "leaky"}).json()["job_id"]
        final = _wait_for_state(client, job_id, {"completed"})

        assert set(final) == EXPECTED_SNAPSHOT_KEYS
        assert set(final["artifacts"]) == EXPECTED_ARTIFACT_KEYS
        assert MESH_SECRET not in final["stdout"]
        assert "[REDACTED]" in final["stdout"]

        detail_text = client.get(f"/api/runs/{job_id}").text
        list_text = client.get("/api/runs").text
        assert MESH_SECRET not in detail_text
        assert MESH_SECRET not in list_text


def test_lifespan_starts_single_manager_and_shuts_it_down(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}
    instances: list[JobManager] = []

    def factory() -> JobManager:
        config = load_config()
        manager = JobManager(
            JobManagerSettings(
                timeout_seconds=5.0,
                kill_grace_seconds=0.5,
                python_executable=FAKE_INTERPRETER,
            ),
            repo_root=config.repo_root,
            artifacts_dir=config.artifacts_dir,
            env={},
            popen_factory=make_runner_factory(records, config.artifacts_dir),
        )
        instances.append(manager)
        return manager

    app = create_app(manager_factory=factory)

    with TestClient(app) as client:
        assert len(instances) == 1
        assert instances[0].stats()["started"] is True
        assert instances[0].stats()["shutdown"] is False
        assert client.get("/api/health").status_code == 200

        job_id = client.post("/api/runs", json={"topic": "lifecycle"}).json()[
            "job_id"
        ]
        _wait_for_state(client, job_id, TERMINAL_STATES)

    assert instances[0].stats()["shutdown"] is True

    with TestClient(app) as client:
        assert len(instances) == 2
        assert client.get("/api/runs").status_code == 200

    assert instances[1].stats()["shutdown"] is True


def test_runs_endpoints_return_503_without_lifespan():
    client = TestClient(module_app)

    assert client.get("/api/runs").status_code == 503
    assert client.post("/api/runs", json={"topic": "x"}).status_code == 503
    assert client.get("/api/runs/abc").status_code == 503
    assert client.post("/api/runs/abc/cancel").status_code == 503
    assert client.get("/api/runs/abc/report").status_code == 503


def test_health_and_cors_unchanged_for_lifespan_app(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["engine"]["configured"] is True

        allowed = client.get(
            "/api/runs", headers={"Origin": "http://localhost:3000"}
        )
        assert allowed.headers.get("access-control-allow-origin") == (
            "http://localhost:3000"
        )

        blocked = client.get(
            "/api/runs", headers={"Origin": "https://evil.example"}
        )
        assert "access-control-allow-origin" not in blocked.headers

        preflight = client.options(
            "/api/runs",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert preflight.status_code == 200
        assert "POST" in preflight.headers.get(
            "access-control-allow-methods", ""
        )

        blocked_preflight = client.options(
            "/api/runs",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in blocked_preflight.headers
