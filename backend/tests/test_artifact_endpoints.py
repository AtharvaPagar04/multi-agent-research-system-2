"""
backend/tests/test_artifact_endpoints.py
----------------------------------------
Tests for artifact API endpoints:
- GET /api/runs/{job_id}/claims
- GET /api/runs/{job_id}/evidence
- GET /api/runs/{job_id}/audit
- GET /api/runs/{job_id}/feedback
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import app as module_app
from backend.app import create_app
from backend.config import JobManagerSettings, load_config
from backend.jobs import JobManager
from backend.tests.test_jobs import make_runner_factory

RUN_ID_VALID = "11112222333344445555666677778888"
RUN_ID_MISSING = "99999999999999999999999999999999"
RUN_ID_HISTORICAL = "de3572bb68b442fd92e4ca8086cbea18"


def _fake_repo_with_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "research_crew").mkdir(parents=True)
    (repo / "research_crew" / "main.py").write_text("# fake\n", encoding="utf-8")
    (repo / "backend").mkdir(parents=True)
    (repo / "backend" / "engine_runner.py").write_text("# fake\n", encoding="utf-8")

    artifacts = repo / "evidence_runs"
    artifacts.mkdir(parents=True)

    # Write sample valid artifacts for RUN_ID_VALID
    claims_data = {
        "total_claims": 5,
        "supported_claims_count": 3,
        "contradicted_claims_count": 0,
        "insufficient_evidence_claims_count": 2,
        "records": [
            {
                "claim_id": "c1",
                "claim_text": "Sample verified claim",
                "verdict": "supported",
                "rationale": "Directly evidenced",
            }
        ],
        "warnings": [],
    }
    (artifacts / f"claim_verifications_{RUN_ID_VALID}.json").write_text(
        json.dumps(claims_data), encoding="utf-8"
    )

    evidence_data = {
        "version": 1,
        "run_id": RUN_ID_VALID,
        "record_count": 1,
        "records": [
            {
                "evidence_id": "e1",
                "source_url": "https://example.com/source",
                "retrieval_status": "full_text",
            }
        ],
    }
    (artifacts / f"evidence_{RUN_ID_VALID}.json").write_text(
        json.dumps(evidence_data), encoding="utf-8"
    )

    feedback_data = {
        "run_id": RUN_ID_VALID,
        "max_rounds": 2,
        "total_rounds_executed": 1,
        "terminated_reason": "budget_exhausted",
        "iterations": [],
    }
    (artifacts / f"feedback_loop_{RUN_ID_VALID}.json").write_text(
        json.dumps(feedback_data), encoding="utf-8"
    )

    report_markdown = (
        "# Sample Research Report\n\n"
        "1. Finding one with evidence.\n\n"
        "---\n"
        "*Citation-level evidence depth: 1/1 cited source URLs verified in store.*\n"
        "*Claim verification: 3/5 factual claims supported by cited source evidence.*\n"
        "*Metrics audit: 2/2 reported values found in captured source text.*\n"
        "*Warning: 2 claim(s) lack supporting evidence.*\n"
    )
    (artifacts / f"research_report_{RUN_ID_VALID}.md").write_text(
        report_markdown, encoding="utf-8"
    )

    return repo, artifacts


def _configure_env(monkeypatch, repo: Path, artifacts: Path):
    monkeypatch.setenv("RESEARCH_REPO_ROOT", str(repo))
    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)
    monkeypatch.setenv("MESH_API_KEY", "fake-mesh")
    monkeypatch.setenv("EXA_API_KEY", "fake-exa")
    monkeypatch.delenv("DATABASE_URL", raising=False)


def _make_app(records: dict):
    def manager_factory() -> JobManager:
        config = load_config()
        settings = JobManagerSettings(
            max_concurrent=1,
            queue_capacity=8,
            timeout_seconds=5.0,
            kill_grace_seconds=0.5,
        )
        return JobManager(
            settings,
            repo_root=config.repo_root,
            artifacts_dir=config.artifacts_dir,
            env={},
            popen_factory=make_runner_factory(records, config.artifacts_dir),
        )

    return create_app(manager_factory=manager_factory)


def test_claims_endpoint_success(monkeypatch, tmp_path):
    repo, artifacts = _fake_repo_with_artifacts(tmp_path)
    _configure_env(monkeypatch, repo, artifacts)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        res = client.get(f"/api/runs/{RUN_ID_VALID}/claims")
        assert res.status_code == 200
        body = res.json()
        assert body["total_claims"] == 5
        assert body["supported_claims_count"] == 3
        assert len(body["records"]) == 1


def test_evidence_endpoint_success(monkeypatch, tmp_path):
    repo, artifacts = _fake_repo_with_artifacts(tmp_path)
    _configure_env(monkeypatch, repo, artifacts)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        res = client.get(f"/api/runs/{RUN_ID_VALID}/evidence")
        assert res.status_code == 200
        body = res.json()
        assert body["version"] == 1
        assert body["run_id"] == RUN_ID_VALID
        assert body["record_count"] == 1


def test_feedback_endpoint_success(monkeypatch, tmp_path):
    repo, artifacts = _fake_repo_with_artifacts(tmp_path)
    _configure_env(monkeypatch, repo, artifacts)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        res = client.get(f"/api/runs/{RUN_ID_VALID}/feedback")
        assert res.status_code == 200
        body = res.json()
        assert body["run_id"] == RUN_ID_VALID
        assert body["total_rounds_executed"] == 1
        assert body["terminated_reason"] == "budget_exhausted"


def test_audit_endpoint_success(monkeypatch, tmp_path):
    repo, artifacts = _fake_repo_with_artifacts(tmp_path)
    _configure_env(monkeypatch, repo, artifacts)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        res = client.get(f"/api/runs/{RUN_ID_VALID}/audit")
        assert res.status_code == 200
        body = res.json()
        assert body["run_id"] == RUN_ID_VALID
        assert body["report_available"] is True
        assert body["claim_audit"]["supported"] == 3
        assert body["claim_audit"]["total"] == 5
        assert body["metric_audit"]["traceable"] == 2
        assert body["metric_audit"]["total"] == 2
        assert any("2 claim(s) lack supporting evidence" in w for w in body["warnings"])


def test_artifact_endpoints_missing_artifacts(monkeypatch, tmp_path):
    repo, artifacts = _fake_repo_with_artifacts(tmp_path)
    _configure_env(monkeypatch, repo, artifacts)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        for ep in ("claims", "evidence", "feedback", "audit"):
            res = client.get(f"/api/runs/{RUN_ID_MISSING}/{ep}")
            assert res.status_code == 404
            assert "not found" in res.json()["detail"].lower()


def test_artifact_endpoints_malformed_run_id(monkeypatch, tmp_path):
    repo, artifacts = _fake_repo_with_artifacts(tmp_path)
    _configure_env(monkeypatch, repo, artifacts)
    records: dict = {}

    with TestClient(_make_app(records)) as client:
        malformed_ids = ["../etc/passwd", "not-a-hex-id", "123", "a" * 31, "g" * 32]
        for bad_id in malformed_ids:
            for ep in ("claims", "evidence", "feedback", "audit", "report"):
                res = client.get(f"/api/runs/{bad_id}/{ep}")
                assert res.status_code == 404


def test_artifact_endpoints_503_without_lifespan():
    client = TestClient(module_app)
    for ep in ("claims", "evidence", "feedback", "audit"):
        assert client.get(f"/api/runs/{RUN_ID_VALID}/{ep}").status_code == 503


def test_historical_run_artifacts_retrieval(monkeypatch):
    """Verify live retrieval of historical artifacts for run de3572bb68b442fd92e4ca8086cbea18."""
    # Use real repo root and evidence_runs
    config = load_config()
    historical_path = config.artifacts_dir / f"claim_verifications_{RUN_ID_HISTORICAL}.json"
    if not historical_path.is_file():
        pytest.skip(f"Historical artifact {RUN_ID_HISTORICAL} not found on disk")

    app = create_app()
    with TestClient(app) as client:
        claims_res = client.get(f"/api/runs/{RUN_ID_HISTORICAL}/claims")
        assert claims_res.status_code == 200
        assert claims_res.json()["total_claims"] == 22

        ev_res = client.get(f"/api/runs/{RUN_ID_HISTORICAL}/evidence")
        assert ev_res.status_code == 200
        assert ev_res.json()["record_count"] == 28

        fb_res = client.get(f"/api/runs/{RUN_ID_HISTORICAL}/feedback")
        assert fb_res.status_code == 200
        assert fb_res.json()["final_claims_supported_count"] == 12

        audit_res = client.get(f"/api/runs/{RUN_ID_HISTORICAL}/audit")
        assert audit_res.status_code == 200
        assert audit_res.json()["report_available"] is True
        assert audit_res.json()["citation_audit"]["matched"] == 14

        rep_res = client.get(f"/api/runs/{RUN_ID_HISTORICAL}/report")
        assert rep_res.status_code == 200
        assert "Loco-Bench" in rep_res.text or "VRAM" in rep_res.text
