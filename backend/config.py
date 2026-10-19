"""
backend/config.py
-----------------
Centralized backend configuration.

Path resolution delegates to ``backend.engine_runner`` so the API, the engine
runner, and the Next.js frontend all agree on the repository root and the
artifact directory (``RESEARCH_REPO_ROOT`` / ``RESEARCH_ARTIFACTS_DIR`` /
``EVIDENCE_DIR`` environment overrides, then ``<repo>/evidence_runs``).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from backend.engine_runner import resolve_artifacts_dir, resolve_repo_root

DEFAULT_FRONTEND_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)

REQUIRED_ENGINE_ENV: tuple[str, ...] = ("MESH_API_KEY", "EXA_API_KEY")
OPTIONAL_ENGINE_ENV: tuple[str, ...] = ("TAVILY_API_KEY",)


@dataclass(frozen=True)
class BackendConfig:
    """Resolved backend configuration (paths and origins only, never secrets)."""

    repo_root: Path
    artifacts_dir: Path
    frontend_origins: tuple[str, ...]


def _parse_frontend_origins(raw: str | None) -> tuple[str, ...]:
    if raw:
        parsed = tuple(part.strip() for part in raw.split(",") if part.strip())
        if parsed:
            return parsed
    return DEFAULT_FRONTEND_ORIGINS


def load_config() -> BackendConfig:
    """Resolve configuration from the current environment."""
    repo_root = resolve_repo_root()
    return BackendConfig(
        repo_root=repo_root,
        artifacts_dir=resolve_artifacts_dir(repo_root),
        frontend_origins=_parse_frontend_origins(os.getenv("FRONTEND_ORIGIN")),
    )


@dataclass(frozen=True)
class JobManagerSettings:
    """Tunables for the subprocess job manager (backend.jobs)."""

    max_concurrent: int = 1
    queue_capacity: int = 16
    timeout_seconds: float = 2700.0
    kill_grace_seconds: float = 10.0
    output_limit_bytes: int = 65536
    python_executable: str | None = None


def _env_int(source: Mapping[str, str], name: str, default: int) -> int:
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be >= 1, got {value}")
    return value


def _env_float(
    source: Mapping[str, str], name: str, default: float, *, allow_zero: bool = False
) -> float:
    raw = source.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc
    if value < 0 or (value == 0 and not allow_zero):
        bound = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be {bound}, got {value}")
    return value


def load_job_settings(env: Mapping[str, str] | None = None) -> JobManagerSettings:
    """Resolve job-manager settings from the environment.

    Recognized variables: ``JOB_MAX_CONCURRENT``, ``JOB_QUEUE_CAPACITY``,
    ``JOB_TIMEOUT_SECONDS``, ``JOB_KILL_GRACE_SECONDS``,
    ``JOB_OUTPUT_LIMIT_BYTES``, ``ENGINE_PYTHON``. Invalid values raise
    ``ValueError`` rather than being silently ignored.
    """
    source = os.environ if env is None else env
    python_raw = source.get("ENGINE_PYTHON")
    python_executable = python_raw.strip() if python_raw and python_raw.strip() else None
    return JobManagerSettings(
        max_concurrent=_env_int(source, "JOB_MAX_CONCURRENT", 1),
        queue_capacity=_env_int(source, "JOB_QUEUE_CAPACITY", 16),
        timeout_seconds=_env_float(source, "JOB_TIMEOUT_SECONDS", 2700.0),
        kill_grace_seconds=_env_float(
            source, "JOB_KILL_GRACE_SECONDS", 10.0, allow_zero=True
        ),
        output_limit_bytes=_env_int(source, "JOB_OUTPUT_LIMIT_BYTES", 65536),
        python_executable=python_executable,
    )
