"""
backend/db.py
-------------
PostgreSQL persistence layer for the Multi-Agent Research System.

Provides persistent storage for research job submissions, lifecycle state
transitions, exit diagnostics, and run artifact links using PostgreSQL.
Thread-safe, parameterized queries, and isolated schema support for testing.
"""

from __future__ import annotations

import datetime
import logging
import os
import uuid
from typing import Any, Mapping, Optional

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "postgresql://postgres@127.0.0.1:5432/multi_agent_research"


class DatabaseError(RuntimeError):
    """Base exception for database errors."""


class DatabaseConnectionError(DatabaseError):
    """Raised when connecting to PostgreSQL fails."""


def _to_dt(ts: Optional[float | datetime.datetime]) -> Optional[datetime.datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime.datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=datetime.timezone.utc)
    try:
        return datetime.datetime.fromtimestamp(float(ts), tz=datetime.timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def _to_epoch(dt: Optional[datetime.datetime]) -> Optional[float]:
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return float(dt)
    return dt.timestamp()


class DatabaseManager:
    """Manages PostgreSQL persistence for research jobs and research runs."""

    def __init__(
        self,
        database_url: Optional[str] = None,
        *,
        schema: Optional[str] = None,
    ) -> None:
        if psycopg is None:
            raise DatabaseError(
                "psycopg is not installed. Please install 'psycopg[binary]'."
            )
        resolved_url = database_url or os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL
        env_mode = (os.getenv("ENV") or os.getenv("ENVIRONMENT") or "").lower()
        if env_mode == "production" and not os.getenv("DATABASE_URL"):
            raise DatabaseError("DATABASE_URL must be explicitly configured in production")
        self.database_url = resolved_url.strip()
        self.schema = schema.strip() if schema and schema.strip() else None

    def _connect(self):
        try:
            conn = psycopg.connect(self.database_url, row_factory=dict_row)
            if self.schema:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {self.schema}, public")
            return conn
        except Exception as exc:
            raise DatabaseConnectionError(
                f"Failed to connect to PostgreSQL at {self.database_url}: {exc}"
            ) from exc

    def health_check(self) -> bool:
        """Return True if PostgreSQL connection is operational."""
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    return bool(cur.fetchone())
        except Exception as exc:
            logger.warning("Database health check failed: %s", exc)
            return False

    def init_db(self, schema: Optional[str] = None) -> None:
        """Initialize database schema, tables, and indexes."""
        active_schema = schema or self.schema
        with self._connect() as conn:
            with conn.cursor() as cur:
                if active_schema:
                    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {active_schema}")
                    cur.execute(f"SET search_path TO {active_schema}, public")
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS research_jobs (
                        id VARCHAR(64) PRIMARY KEY,
                        query TEXT NOT NULL,
                        status VARCHAR(32) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL,
                        started_at TIMESTAMPTZ,
                        completed_at TIMESTAMPTZ,
                        error TEXT,
                        exit_code INTEGER,
                        failure_kind VARCHAR(64),
                        stage VARCHAR(64)
                    );
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS research_runs (
                        id VARCHAR(64) PRIMARY KEY,
                        job_id VARCHAR(64) REFERENCES research_jobs(id) ON DELETE CASCADE,
                        run_id VARCHAR(32) NOT NULL,
                        artifact_path TEXT,
                        created_at TIMESTAMPTZ NOT NULL,
                        completed_at TIMESTAMPTZ
                    );
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_jobs_created_at ON research_jobs(created_at DESC);"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_runs_job_id ON research_runs(job_id);"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_research_runs_run_id ON research_runs(run_id);"
                )
            conn.commit()
            if active_schema:
                self.schema = active_schema

    def recover_stale_jobs(self) -> int:
        """Mark unfinished jobs from previous server instances as interrupted."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research_jobs
                    SET status = 'interrupted',
                        stage = 'interrupted',
                        error = 'interrupted by server restart'
                    WHERE status IN ('queued', 'starting', 'running');
                    """
                )
                count = cur.rowcount
            conn.commit()
            return count

    def record_job_submitted(
        self,
        job_id: str,
        query: str,
        status: str = "queued",
        created_at: Optional[float | datetime.datetime] = None,
        stage: str = "queued",
    ) -> None:
        """Insert a newly queued research job."""
        dt = _to_dt(created_at) or datetime.datetime.now(datetime.timezone.utc)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO research_jobs (id, query, status, created_at, stage)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        query = EXCLUDED.query,
                        status = EXCLUDED.status,
                        stage = EXCLUDED.stage;
                    """,
                    (job_id, query, status, dt, stage),
                )
            conn.commit()

    def record_job_started(
        self,
        job_id: str,
        status: str = "running",
        started_at: Optional[float | datetime.datetime] = None,
        stage: str = "running",
    ) -> None:
        """Update job when child process spawns."""
        dt = _to_dt(started_at) or datetime.datetime.now(datetime.timezone.utc)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research_jobs
                    SET status = %s,
                        started_at = %s,
                        stage = %s
                    WHERE id = %s;
                    """,
                    (status, dt, stage, job_id),
                )
            conn.commit()

    def record_job_completed(
        self,
        job_id: str,
        status: str,
        completed_at: Optional[float | datetime.datetime] = None,
        exit_code: Optional[int] = None,
        error: Optional[str] = None,
        failure_kind: Optional[str] = None,
        stage: str = "completed",
        run_id: Optional[str] = None,
        artifact_path: Optional[str] = None,
    ) -> None:
        """Finalize job state and record research run link if applicable."""
        dt = _to_dt(completed_at) or datetime.datetime.now(datetime.timezone.utc)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE research_jobs
                    SET status = %s,
                        completed_at = %s,
                        exit_code = %s,
                        error = %s,
                        failure_kind = %s,
                        stage = %s
                    WHERE id = %s;
                    """,
                    (status, dt, exit_code, error, failure_kind, stage, job_id),
                )
                if run_id:
                    run_pk = uuid.uuid4().hex
                    cur.execute(
                        """
                        INSERT INTO research_runs (id, job_id, run_id, artifact_path, created_at, completed_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING;
                        """,
                        (run_pk, job_id, run_id, artifact_path, dt, dt),
                    )
            conn.commit()

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single job with its linked run metadata, or None."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.id AS job_id,
                           j.query AS topic,
                           j.status AS state,
                           j.stage,
                           j.created_at,
                           j.started_at,
                           j.completed_at AS finished_at,
                           j.exit_code,
                           j.error,
                           j.failure_kind,
                           r.run_id,
                           r.artifact_path
                    FROM research_jobs j
                    LEFT JOIN research_runs r ON r.job_id = j.id
                    WHERE j.id = %s;
                    """,
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_snapshot(row)

    def get_job_by_run_id(self, run_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a job by its linked 32-hex run_id."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.id AS job_id,
                           j.query AS topic,
                           j.status AS state,
                           j.stage,
                           j.created_at,
                           j.started_at,
                           j.completed_at AS finished_at,
                           j.exit_code,
                           j.error,
                           j.failure_kind,
                           r.run_id,
                           r.artifact_path
                    FROM research_runs r
                    JOIN research_jobs j ON j.id = r.job_id
                    WHERE r.run_id = %s;
                    """,
                    (run_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return self._row_to_snapshot(row)

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        """List historical jobs ordered by created_at DESC."""
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT j.id AS job_id,
                           j.query AS topic,
                           j.status AS state,
                           j.stage,
                           j.created_at,
                           j.started_at,
                           j.completed_at AS finished_at,
                           j.exit_code,
                           j.error,
                           j.failure_kind,
                           r.run_id,
                           r.artifact_path
                    FROM research_jobs j
                    LEFT JOIN research_runs r ON r.job_id = j.id
                    ORDER BY j.created_at DESC
                    LIMIT %s;
                    """,
                    (limit,),
                )
                return [self._row_to_snapshot(row) for row in cur.fetchall()]

    def _row_to_snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        started = _to_epoch(row.get("started_at"))
        finished = _to_epoch(row.get("finished_at"))
        if started and finished:
            elapsed = max(0.0, finished - started)
        elif started:
            elapsed = max(0.0, datetime.datetime.now(datetime.timezone.utc).timestamp() - started)
        else:
            elapsed = None

        return {
            "job_id": row["job_id"],
            "topic": row.get("topic"),
            "state": row.get("state"),
            "stage": row.get("stage") or "completed",
            "created_at": _to_epoch(row.get("created_at")),
            "started_at": started,
            "finished_at": finished,
            "elapsed_seconds": elapsed,
            "exit_code": row.get("exit_code"),
            "run_id": row.get("run_id"),
            "error": row.get("error"),
            "failure_kind": row.get("failure_kind"),
            "stdout": None,
            "stderr": None,
            "stdout_truncated": None,
            "stderr_truncated": None,
        }
