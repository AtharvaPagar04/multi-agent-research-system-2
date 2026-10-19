"""
backend/app.py
--------------
Local-only FastAPI application for the research engine backend.

Local development (loopback only):

    .venv313/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000

Endpoints:

* ``GET  /api/health``                — boolean configuration/liveness report.
* ``POST /api/runs``                  — enqueue a research run (202 Accepted;
                                        429 when the queue is full; 422 for
                                        invalid input).
* ``GET  /api/runs``                  — manager job snapshots plus artifact-only
                                        historical runs (strict filename match).
* ``GET  /api/runs/{job_id}``         — one job snapshot, or an artifact-only run
                                        record discovered from the artifacts
                                        directory (never with invented state or
                                        timestamps).
* ``POST /api/runs/{job_id}/cancel``  — request cancellation via the manager.
* ``GET  /api/runs/{job_id}/report``  — Markdown report for a completed,
                                        validated run ID.

Security: this service is for local development only. It binds to 127.0.0.1,
keeps the restrictive CORS allow-list, never reads credentials from request
input, never returns environment values or tracebacks, and only serves files
whose names are derived from validated 32-hex run IDs (never from arbitrary
user strings). It has no authentication and must not be exposed publicly.

Lifecycle: the job manager is created and started by the FastAPI lifespan
handler — never at import time — and is shut down when the application
stops. Requests received while no manager is running (e.g. TestClient used
without a context manager) receive ``503``.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.config import (
    OPTIONAL_ENGINE_ENV,
    REQUIRED_ENGINE_ENV,
    BackendConfig,
    JobManagerSettings,
    load_config,
    load_job_settings,
)
from backend.jobs import (
    JobManager,
    JobState,
    ManagerNotStartedError,
    ManagerShutdownError,
    QueueFullError,
)

MAX_TOPIC_LENGTH = 500

_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_REPORT_FILE_TEMPLATE = "research_report_{run_id}.md"
_EVIDENCE_FILE_TEMPLATE = "evidence_{run_id}.json"
_CLAIMS_FILE_TEMPLATE = "claim_verifications_{run_id}.json"
_FEEDBACK_FILE_TEMPLATE = "feedback_loop_{run_id}.json"
_HISTORICAL_NAME_RES = (
    re.compile(r"^evidence_([0-9a-f]{32})\.json$"),
    re.compile(r"^claim_verifications_([0-9a-f]{32})\.json$"),
    re.compile(r"^feedback_loop_([0-9a-f]{32})\.json$"),
    re.compile(r"^research_report_([0-9a-f]{32})\.md$"),
)


class ArtifactAvailability(BaseModel):
    """File-name-derived artifact availability for one validated run ID."""

    report_available: bool = False
    evidence_available: bool = False
    claim_verifications_available: bool = False
    feedback_loop_available: bool = False

    def any_available(self) -> bool:
        return (
            self.report_available
            or self.evidence_available
            or self.claim_verifications_available
            or self.feedback_loop_available
        )


class RunSnapshot(BaseModel):
    """One run as seen through the API.

    ``origin`` distinguishes live manager jobs (``"manager"``) from
    artifact-only historical runs (``"artifact"``). Artifact-origin records
    carry no invented state or timestamps: those fields are ``None`` because
    the artifacts directory cannot prove them.
    """

    origin: Literal["manager", "artifact"]
    job_id: str
    topic: Optional[str] = None
    state: Optional[str] = None
    stage: Optional[str] = None
    created_at: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    exit_code: Optional[int] = None
    run_id: Optional[str] = None
    error: Optional[str] = None
    failure_kind: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    stdout_truncated: Optional[bool] = None
    stderr_truncated: Optional[bool] = None
    artifacts: Optional[ArtifactAvailability] = None


class HistoricalRun(BaseModel):
    """An artifact-only run discovered from the artifacts directory.

    Contains only facts derived from strict file-name matches; it carries no
    state, timestamps, or other inferred metadata.
    """

    origin: Literal["artifact"] = "artifact"
    run_id: str
    artifacts: ArtifactAvailability


class RunListResponse(BaseModel):
    jobs: list[RunSnapshot]
    historical_runs: list[HistoricalRun]


class RunCreateRequest(BaseModel):
    topic: str


class CancelResponse(BaseModel):
    accepted: bool
    job: RunSnapshot


def build_health_report(config: BackendConfig) -> dict[str, Any]:
    """Build the health payload from path checks and env-var presence.

    All readiness fields are booleans. ``engine.configured`` means "the
    engine module, the runner, and the required API credentials are present".
    It is a configuration check only — not a liveness probe of the engine or
    of Mesh/Exa connectivity.

    Limitation: env presence is judged from the API process environment only.
    The engine's ``.env`` file loading happens inside research child
    processes (``load_dotenv`` resolved from the repository working
    directory) and is not independently validated here; a variable being
    present in this response does not prove the child sees a valid value.
    """
    checks = {
        "repo_root_found": config.repo_root.is_dir(),
        "engine_module_found": (
            config.repo_root / "research_crew" / "main.py"
        ).is_file(),
        "runner_found": (
            config.repo_root / "backend" / "engine_runner.py"
        ).is_file(),
        "artifacts_dir_found": config.artifacts_dir.is_dir(),
    }
    required_env_present = {name: bool(os.getenv(name)) for name in REQUIRED_ENGINE_ENV}
    optional_env_present = {name: bool(os.getenv(name)) for name in OPTIONAL_ENGINE_ENV}
    configured = (
        checks["repo_root_found"]
        and checks["engine_module_found"]
        and checks["runner_found"]
        and all(required_env_present.values())
    )
    return {
        "status": "ok",
        "engine": {
            "configured": configured,
            "checks": checks,
            "required_env_present": required_env_present,
            "optional_env_present": optional_env_present,
        },
    }


def _require_manager(request: Request) -> JobManager:
    manager = getattr(request.app.state, "job_manager", None)
    if manager is None:
        raise HTTPException(status_code=503, detail="job manager is not running")
    return manager


def _artifact_availability(artifacts_dir: Path, run_id: str) -> ArtifactAvailability:
    return ArtifactAvailability(
        report_available=(
            artifacts_dir / _REPORT_FILE_TEMPLATE.format(run_id=run_id)
        ).is_file(),
        evidence_available=(
            artifacts_dir / _EVIDENCE_FILE_TEMPLATE.format(run_id=run_id)
        ).is_file(),
        claim_verifications_available=(
            artifacts_dir / _CLAIMS_FILE_TEMPLATE.format(run_id=run_id)
        ).is_file(),
        feedback_loop_available=(
            artifacts_dir / _FEEDBACK_FILE_TEMPLATE.format(run_id=run_id)
        ).is_file(),
    )


def _validated_report_path(artifacts_dir: Path, run_id: str) -> Optional[Path]:
    """Return the report path for a validated run ID, never from raw input."""
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        return None
    path = artifacts_dir / _REPORT_FILE_TEMPLATE.format(run_id=run_id)
    return path if path.is_file() else None


def _manager_snapshot_payload(config: BackendConfig, snapshot: dict) -> RunSnapshot:
    run_id = snapshot.get("run_id")
    artifacts = None
    if isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id):
        artifacts = _artifact_availability(config.artifacts_dir, run_id)
    return RunSnapshot(origin="manager", artifacts=artifacts, **snapshot)


def _artifact_only_payload(
    config: BackendConfig, run_id: str
) -> Optional[RunSnapshot]:
    if not _RUN_ID_RE.fullmatch(run_id):
        return None
    availability = _artifact_availability(config.artifacts_dir, run_id)
    if not availability.any_available():
        return None
    return RunSnapshot(
        origin="artifact",
        job_id=run_id,
        run_id=run_id,
        artifacts=availability,
    )


def _discover_historical_runs(
    artifacts_dir: Path, exclude_run_ids: set[str]
) -> list[HistoricalRun]:
    """Discover artifact-only runs by strict filename match, sorted by run ID.

    Only top-level regular files are inspected; nothing is inferred beyond
    the existence of strictly-named artifact files.
    """
    if not artifacts_dir.is_dir():
        return []
    try:
        entries = list(artifacts_dir.iterdir())
    except OSError:
        return []
    run_ids: set[str] = set()
    for entry in entries:
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        for pattern in _HISTORICAL_NAME_RES:
            match = pattern.match(entry.name)
            if match:
                run_ids.add(match.group(1))
                break
    return [
        HistoricalRun(
            run_id=run_id,
            artifacts=_artifact_availability(artifacts_dir, run_id),
        )
        for run_id in sorted(run_ids - exclude_run_ids)
    ]


def _default_manager_factory() -> JobManager:
    config = load_config()
    settings = load_job_settings(os.environ)
    return JobManager(
        settings,
        repo_root=config.repo_root,
        artifacts_dir=config.artifacts_dir,
    )


def create_app(
    *,
    manager_factory: Optional[Callable[[], JobManager]] = None,
) -> FastAPI:
    """Build the FastAPI application.

    ``manager_factory`` is an injection seam for tests; when omitted, the
    lifespan handler builds a real :class:`~backend.jobs.JobManager` from the
    resolved configuration and the process environment.
    """
    config = load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        factory = manager_factory or _default_manager_factory
        manager = factory()
        manager.start()
        app.state.job_manager = manager
        try:
            yield
        finally:
            await asyncio.to_thread(manager.shutdown)
            app.state.job_manager = None

    app = FastAPI(
        title="Research Engine Backend",
        version="0.1.0",
        description=(
            "Local-only API layer for the multi-agent research system. "
            "Not safe to expose publicly."
        ),
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.frontend_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return build_health_report(load_config())

    @app.post("/api/runs", status_code=202)
    def create_run(payload: RunCreateRequest, request: Request) -> RunSnapshot:
        manager = _require_manager(request)
        topic = payload.topic.strip()
        if not topic:
            raise HTTPException(
                status_code=422, detail="topic must be a non-empty string"
            )
        if len(topic) > MAX_TOPIC_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=f"topic must be at most {MAX_TOPIC_LENGTH} characters",
            )
        try:
            job_id = manager.submit(topic)
        except QueueFullError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (ManagerNotStartedError, ManagerShutdownError) as exc:
            raise HTTPException(
                status_code=503, detail="job manager is not accepting jobs"
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        snapshot = manager.get(job_id)
        if snapshot is None:
            raise HTTPException(status_code=500, detail="job was not recorded")
        return _manager_snapshot_payload(load_config(), snapshot)

    @app.get("/api/runs")
    def list_runs(request: Request) -> RunListResponse:
        manager = _require_manager(request)
        config = load_config()
        jobs = [
            _manager_snapshot_payload(config, snapshot)
            for snapshot in manager.list_jobs()
        ]
        owned = {job.run_id for job in jobs if job.run_id}
        historical = _discover_historical_runs(config.artifacts_dir, owned)
        return RunListResponse(jobs=jobs, historical_runs=historical)

    @app.get("/api/runs/{job_id}")
    def get_run(job_id: str, request: Request) -> RunSnapshot:
        manager = _require_manager(request)
        config = load_config()
        snapshot = manager.get(job_id)
        if snapshot is not None:
            return _manager_snapshot_payload(config, snapshot)
        artifact_only = _artifact_only_payload(config, job_id)
        if artifact_only is not None:
            return artifact_only
        raise HTTPException(status_code=404, detail="unknown job ID")

    @app.post("/api/runs/{job_id}/cancel")
    def cancel_run(job_id: str, request: Request) -> CancelResponse:
        manager = _require_manager(request)
        config = load_config()
        if manager.get(job_id) is None:
            raise HTTPException(status_code=404, detail="unknown job ID")
        accepted = manager.cancel(job_id)
        current = manager.get(job_id)
        if current is None:
            raise HTTPException(status_code=404, detail="unknown job ID")
        return CancelResponse(
            accepted=bool(accepted),
            job=_manager_snapshot_payload(config, current),
        )

    @app.get("/api/runs/{job_id}/report")
    def get_run_report(job_id: str, request: Request) -> FileResponse:
        manager = _require_manager(request)
        config = load_config()
        snapshot = manager.get(job_id)
        if snapshot is not None:
            run_id = snapshot.get("run_id")
            if (
                snapshot.get("state") != JobState.COMPLETED.value
                or not isinstance(run_id, str)
            ):
                raise HTTPException(
                    status_code=404, detail="report is not available for this job"
                )
        else:
            run_id = job_id
        path = _validated_report_path(config.artifacts_dir, run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="report not found")
        return FileResponse(path, media_type="text/markdown")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("backend.app:app", host="127.0.0.1", port=8000)
