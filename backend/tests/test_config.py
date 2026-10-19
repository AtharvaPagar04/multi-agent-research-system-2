"""
Offline tests for backend.config.
---------------------------------
All path resolution is exercised through environment overrides and pytest
``tmp_path``; the real artifact directory is never written to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import engine_runner
from backend.config import (
    DEFAULT_FRONTEND_ORIGINS,
    JobManagerSettings,
    load_config,
    load_job_settings,
)

REPO_ROOT_FROM_FILE = Path(engine_runner.__file__).resolve().parents[1]


def _clear_path_env(monkeypatch):
    for var in ("RESEARCH_REPO_ROOT", "RESEARCH_ARTIFACTS_DIR", "EVIDENCE_DIR"):
        monkeypatch.delenv(var, raising=False)


def test_default_repo_root_is_the_repository_root(monkeypatch):
    _clear_path_env(monkeypatch)
    assert load_config().repo_root == REPO_ROOT_FROM_FILE


def test_path_env_overrides_are_honored(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    artifacts = tmp_path / "artifacts"
    monkeypatch.setenv("RESEARCH_REPO_ROOT", str(repo))
    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)

    config = load_config()

    assert config.repo_root == repo.resolve()
    assert config.artifacts_dir == artifacts.resolve()


def test_artifacts_dir_defaults_to_repo_evidence_runs(monkeypatch):
    _clear_path_env(monkeypatch)
    config = load_config()
    assert config.artifacts_dir == (config.repo_root / "evidence_runs").resolve()


def test_artifacts_dir_falls_back_to_evidence_dir(monkeypatch, tmp_path):
    _clear_path_env(monkeypatch)
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "engine-side"))
    config = load_config()
    assert config.artifacts_dir == (tmp_path / "engine-side").resolve()


def test_research_artifacts_dir_takes_precedence_over_evidence_dir(
    monkeypatch, tmp_path
):
    _clear_path_env(monkeypatch)
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "engine-side"))
    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", str(tmp_path / "frontend-side"))
    config = load_config()
    assert config.artifacts_dir == (tmp_path / "frontend-side").resolve()


def test_frontend_origins_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("FRONTEND_ORIGIN", raising=False)
    assert load_config().frontend_origins == DEFAULT_FRONTEND_ORIGINS


def test_frontend_origins_parsed_from_comma_separated_env(monkeypatch):
    monkeypatch.setenv(
        "FRONTEND_ORIGIN", " http://localhost:4000 , http://127.0.0.1:4000 "
    )
    assert load_config().frontend_origins == (
        "http://localhost:4000",
        "http://127.0.0.1:4000",
    )


def test_frontend_origins_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("FRONTEND_ORIGIN", "  ,  ")
    assert load_config().frontend_origins == DEFAULT_FRONTEND_ORIGINS


def test_config_paths_match_engine_runner_resolution(monkeypatch, tmp_path):
    _clear_path_env(monkeypatch)
    monkeypatch.setenv("RESEARCH_REPO_ROOT", str(tmp_path / "repo"))

    config = load_config()

    assert config.repo_root == engine_runner.resolve_repo_root()
    assert config.artifacts_dir == engine_runner.resolve_artifacts_dir(
        config.repo_root
    )


_JOB_ENV_VARS = (
    "JOB_MAX_CONCURRENT",
    "JOB_QUEUE_CAPACITY",
    "JOB_TIMEOUT_SECONDS",
    "JOB_KILL_GRACE_SECONDS",
    "JOB_OUTPUT_LIMIT_BYTES",
    "ENGINE_PYTHON",
)


def _clear_job_env(monkeypatch):
    for var in _JOB_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_job_settings_defaults(monkeypatch):
    _clear_job_env(monkeypatch)

    settings = load_job_settings()

    assert settings == JobManagerSettings(
        max_concurrent=1,
        queue_capacity=16,
        timeout_seconds=2700.0,
        kill_grace_seconds=10.0,
        output_limit_bytes=65536,
        python_executable=None,
    )


def test_job_settings_read_from_environment(monkeypatch):
    _clear_job_env(monkeypatch)
    monkeypatch.setenv("JOB_MAX_CONCURRENT", " 2 ")
    monkeypatch.setenv("JOB_QUEUE_CAPACITY", "5")
    monkeypatch.setenv("JOB_TIMEOUT_SECONDS", "90.5")
    monkeypatch.setenv("JOB_KILL_GRACE_SECONDS", "0")
    monkeypatch.setenv("JOB_OUTPUT_LIMIT_BYTES", "1024")
    monkeypatch.setenv("ENGINE_PYTHON", "/opt/python/bin/python3")

    settings = load_job_settings()

    assert settings == JobManagerSettings(
        max_concurrent=2,
        queue_capacity=5,
        timeout_seconds=90.5,
        kill_grace_seconds=0.0,
        output_limit_bytes=1024,
        python_executable="/opt/python/bin/python3",
    )


def test_job_settings_reject_invalid_values(monkeypatch):
    _clear_job_env(monkeypatch)

    monkeypatch.setenv("JOB_MAX_CONCURRENT", "0")
    with pytest.raises(ValueError, match="JOB_MAX_CONCURRENT"):
        load_job_settings()

    monkeypatch.setenv("JOB_MAX_CONCURRENT", "abc")
    with pytest.raises(ValueError, match="JOB_MAX_CONCURRENT"):
        load_job_settings()

    monkeypatch.setenv("JOB_MAX_CONCURRENT", "1")
    monkeypatch.setenv("JOB_KILL_GRACE_SECONDS", "-1")
    with pytest.raises(ValueError, match="JOB_KILL_GRACE_SECONDS"):
        load_job_settings()
