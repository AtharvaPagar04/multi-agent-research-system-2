"""
backend/app.py
--------------
Local-only FastAPI application for the research engine backend.

Local development (loopback only):

    .venv313/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8001

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
import json
import logging
import os
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from dotenv import load_dotenv
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
    resolve_backend_port,
)
from backend.db import DatabaseManager
from backend.jobs import (
    JobManager,
    JobState,
    ManagerNotStartedError,
    ManagerShutdownError,
    QueueFullError,
)

load_dotenv()
logger = logging.getLogger(__name__)

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
    data = dict(snapshot)
    origin = data.pop("origin", "manager")
    run_id = data.get("run_id")
    artifacts = None
    if isinstance(run_id, str) and _RUN_ID_RE.fullmatch(run_id):
        artifacts = _artifact_availability(config.artifacts_dir, run_id)
    return RunSnapshot(origin=origin, artifacts=artifacts, **data)


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


def _resolve_run_id_for_job(manager: JobManager, job_id: str) -> Optional[str]:
    """Resolve a job_id or run_id to a validated 32-hex run_id.

    Returns the 32-hex run_id if:
    1. job_id corresponds to a recorded job in manager (or DB) and has a valid run_id.
    2. job_id is itself a valid 32-hex run_id.
    Otherwise returns None.
    """
    snapshot = manager.get(job_id)
    if snapshot is not None:
        run_id = snapshot.get("run_id")
        if (
            snapshot.get("state") == JobState.COMPLETED.value
            and isinstance(run_id, str)
            and _RUN_ID_RE.fullmatch(run_id)
        ):
            return run_id
        return None
    if isinstance(job_id, str) and _RUN_ID_RE.fullmatch(job_id):
        return job_id
    return None


def _build_audit_payload(artifacts_dir: Path, run_id: str) -> Optional[dict[str, Any]]:
    """Build a structured audit response from artifact files."""
    audit_path = artifacts_dir / f"audit_{run_id}.json"
    if audit_path.is_file():
        try:
            with audit_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    report_path = artifacts_dir / _REPORT_FILE_TEMPLATE.format(run_id=run_id)
    audit_lines: list[dict[str, str]] = []
    warnings: list[str] = []
    citation_audit: dict[str, Any] = {}
    claim_audit: dict[str, Any] = {}
    metric_audit: dict[str, Any] = {}
    has_report = False

    if report_path.is_file():
        has_report = True
        try:
            report_text = report_path.read_text(encoding="utf-8")
            for line in report_text.splitlines():
                trimmed = line.strip()
                if trimmed.startswith("*") and trimmed.endswith("*"):
                    content = trimmed[1:-1].strip()
                    if content.startswith("Warning:"):
                        warnings.append(content[len("Warning:"):].strip())
                    colon_idx = content.find(":")
                    if colon_idx != -1:
                        label = content[:colon_idx].strip()
                        detail = content[colon_idx + 1:].strip()
                        audit_lines.append({"label": label, "detail": detail, "raw": content})
                        label_lower = label.lower()
                        if "citation-level evidence depth" in label_lower:
                            m = re.search(r"(\d+)/(\d+)", detail)
                            if m:
                                citation_audit["matched"] = int(m.group(1))
                                citation_audit["total"] = int(m.group(2))
                            citation_audit["detail"] = detail
                        elif "claim verification" in label_lower:
                            m = re.search(r"(\d+)/(\d+)", detail)
                            if m:
                                claim_audit["supported"] = int(m.group(1))
                                claim_audit["total"] = int(m.group(2))
                            claim_audit["detail"] = detail
                        elif "metrics audit" in label_lower:
                            m = re.search(r"(\d+)/(\d+)", detail)
                            if m:
                                metric_audit["traceable"] = int(m.group(1))
                                metric_audit["total"] = int(m.group(2))
                            metric_audit["detail"] = detail
        except Exception:
            pass

    claims_path = artifacts_dir / _CLAIMS_FILE_TEMPLATE.format(run_id=run_id)
    evidence_path = artifacts_dir / _EVIDENCE_FILE_TEMPLATE.format(run_id=run_id)

    if not has_report and not claims_path.is_file() and not evidence_path.is_file():
        return None

    if claims_path.is_file() and not claim_audit:
        try:
            with claims_path.open("r", encoding="utf-8") as f:
                cdata = json.load(f)
                claim_audit = {
                    "supported": cdata.get("supported_claims_count"),
                    "contradicted": cdata.get("contradicted_claims_count"),
                    "insufficient": cdata.get("insufficient_evidence_claims_count"),
                    "total": cdata.get("total_claims"),
                }
                if isinstance(cdata.get("warnings"), list):
                    for w in cdata["warnings"]:
                        if w not in warnings:
                            warnings.append(w)
        except Exception:
            pass

    if evidence_path.is_file() and not citation_audit:
        try:
            with evidence_path.open("r", encoding="utf-8") as f:
                edata = json.load(f)
                records = edata.get("records", [])
                full_text = sum(1 for r in records if r.get("retrieval_status") == "full_text")
                snippet = sum(1 for r in records if r.get("retrieval_status") == "snippet_only")
                failed = sum(1 for r in records if r.get("retrieval_status") == "failed")
                citation_audit = {
                    "total_sources": len(records),
                    "full_text": full_text,
                    "snippet_only": snippet,
                    "failed": failed,
                }
        except Exception:
            pass

    return {
        "run_id": run_id,
        "report_available": has_report,
        "audit_lines": audit_lines,
        "citation_audit": citation_audit,
        "claim_audit": claim_audit,
        "metric_audit": metric_audit,
        "warnings": warnings,
    }


def _default_manager_factory() -> JobManager:
    config = load_config()
    settings = load_job_settings(os.environ)
    db = None
    if config.database_url:
        try:
            db = DatabaseManager(config.database_url)
        except Exception as exc:
            logger.warning("Could not initialize database manager: %s", exc)
    return JobManager(
        settings,
        repo_root=config.repo_root,
        artifacts_dir=config.artifacts_dir,
        db=db,
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

    @app.get("/api/runs/{job_id}/claims")
    def get_run_claims(job_id: str, request: Request) -> dict[str, Any]:
        manager = _require_manager(request)
        config = load_config()
        run_id = _resolve_run_id_for_job(manager, job_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="Run or job not found")
        path = config.artifacts_dir / _CLAIMS_FILE_TEMPLATE.format(run_id=run_id)
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Claims artifact not found for run {run_id}",
            )
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read claims artifact: {exc}"
            ) from exc

    @app.get("/api/runs/{job_id}/evidence")
    def get_run_evidence(job_id: str, request: Request) -> dict[str, Any]:
        manager = _require_manager(request)
        config = load_config()
        run_id = _resolve_run_id_for_job(manager, job_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="Run or job not found")
        path = config.artifacts_dir / _EVIDENCE_FILE_TEMPLATE.format(run_id=run_id)
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Evidence artifact not found for run {run_id}",
            )
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read evidence artifact: {exc}"
            ) from exc

    @app.get("/api/runs/{job_id}/feedback")
    def get_run_feedback(job_id: str, request: Request) -> dict[str, Any]:
        manager = _require_manager(request)
        config = load_config()
        run_id = _resolve_run_id_for_job(manager, job_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="Run or job not found")
        path = config.artifacts_dir / _FEEDBACK_FILE_TEMPLATE.format(run_id=run_id)
        if not path.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Feedback loop artifact not found for run {run_id}",
            )
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            raise HTTPException(
                status_code=500, detail=f"Failed to read feedback artifact: {exc}"
            ) from exc

    @app.get("/api/runs/{job_id}/audit")
    def get_run_audit(job_id: str, request: Request) -> dict[str, Any]:
        manager = _require_manager(request)
        config = load_config()
        run_id = _resolve_run_id_for_job(manager, job_id)
        if run_id is None:
            raise HTTPException(status_code=404, detail="Run or job not found")
        audit_payload = _build_audit_payload(config.artifacts_dir, run_id)
        if audit_payload is None:
            raise HTTPException(
                status_code=404,
                detail=f"Audit artifact not found for run {run_id}",
            )
        return audit_payload

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = resolve_backend_port(os.environ)
    uvicorn.run("backend.app:app", host="127.0.0.1", port=port)
