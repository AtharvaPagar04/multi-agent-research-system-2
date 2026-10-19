"""
Offline tests for backend.app and its health endpoint.
------------------------------------------------------
The app is exercised through FastAPI's TestClient against a synthetic
repository tree in ``tmp_path`` and mocked environment variables. The
research engine is never imported (verified explicitly by a subprocess
import-isolation test), and no external network calls are made.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from backend import engine_runner
from backend.app import app

REPO_ROOT_FROM_FILE = Path(engine_runner.__file__).resolve().parents[1]

IMPORT_ISOLATION_CODE = """
import sys

from fastapi.testclient import TestClient

import backend.app as app_module

assert "research_crew" not in sys.modules, "engine imported at app import time"
assert "crewai" not in sys.modules, "crewai imported at app import time"

client = TestClient(app_module.app)
response = client.get("/api/health")
assert response.status_code == 200, response.text

assert "research_crew" not in sys.modules, "engine imported during health request"
assert "crewai" not in sys.modules, "crewai imported during health request"
print("import-isolation-ok")
"""

client = TestClient(app)


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


def _configure_env(
    monkeypatch,
    repo: Path,
    *,
    mesh: str | None = "fake-mesh-key",
    exa: str | None = "fake-exa-key",
    tavily: str | None = None,
) -> None:
    monkeypatch.setenv("RESEARCH_REPO_ROOT", str(repo))
    monkeypatch.delenv("RESEARCH_ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)
    for name, value in (
        ("MESH_API_KEY", mesh),
        ("EXA_API_KEY", exa),
        ("TAVILY_API_KEY", tavily),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_health_returns_expected_boolean_readiness(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    engine = body["engine"]
    assert engine["configured"] is True
    assert engine["checks"] == {
        "repo_root_found": True,
        "engine_module_found": True,
        "runner_found": True,
        "artifacts_dir_found": True,
    }
    assert engine["required_env_present"] == {
        "MESH_API_KEY": True,
        "EXA_API_KEY": True,
    }
    assert engine["optional_env_present"] == {"TAVILY_API_KEY": False}


def test_health_never_exposes_secret_values(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(
        monkeypatch,
        repo,
        mesh="mesh-secret-abc123",
        exa="exa-secret-def456",
        tavily="tavily-secret-ghi789",
    )

    response = client.get("/api/health")

    assert response.status_code == 200
    assert "mesh-secret-abc123" not in response.text
    assert "exa-secret-def456" not in response.text
    assert "tavily-secret-ghi789" not in response.text
    body = response.json()
    assert body["engine"]["required_env_present"] == {
        "MESH_API_KEY": True,
        "EXA_API_KEY": True,
    }
    assert body["engine"]["optional_env_present"] == {"TAVILY_API_KEY": True}


def test_missing_optional_credentials_do_not_break_health(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo, tavily=None)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["engine"]["optional_env_present"]["TAVILY_API_KEY"] is False
    assert body["engine"]["configured"] is True


def test_missing_required_credentials_reported_honestly(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo, mesh=None, exa=None)

    response = client.get("/api/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["engine"]["configured"] is False
    assert body["engine"]["required_env_present"] == {
        "MESH_API_KEY": False,
        "EXA_API_KEY": False,
    }


def test_missing_engine_module_reports_not_configured(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    (repo / "research_crew" / "main.py").unlink()
    _configure_env(monkeypatch, repo)

    body = client.get("/api/health").json()

    assert body["engine"]["checks"]["engine_module_found"] is False
    assert body["engine"]["configured"] is False


def test_missing_artifacts_dir_is_reported_but_not_gating(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    (repo / "evidence_runs").rmdir()
    _configure_env(monkeypatch, repo)

    body = client.get("/api/health").json()

    assert body["engine"]["checks"]["artifacts_dir_found"] is False
    assert body["engine"]["configured"] is True


def test_cors_allows_configured_frontend_origin_only(monkeypatch, tmp_path):
    repo = _fake_repo(tmp_path)
    _configure_env(monkeypatch, repo)

    allowed = client.get(
        "/api/health", headers={"Origin": "http://localhost:3000"}
    )
    blocked = client.get(
        "/api/health", headers={"Origin": "https://evil.example"}
    )

    assert allowed.headers.get("access-control-allow-origin") == (
        "http://localhost:3000"
    )
    assert "access-control-allow-origin" not in blocked.headers


def test_importing_app_does_not_import_research_engine():
    result = subprocess.run(
        [sys.executable, "-c", IMPORT_ISOLATION_CODE],
        cwd=REPO_ROOT_FROM_FILE,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "import-isolation-ok" in result.stdout
