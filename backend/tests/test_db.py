"""
backend/tests/test_db.py
------------------------
Automated tests for PostgreSQL persistence, schema isolation, and restart recovery.

All database tests operate inside uniquely named temporary PostgreSQL schemas
(e.g. `test_schema_<hex>`) that are cleanly dropped at fixture teardown, guaranteeing
complete isolation and zero contamination of local development databases.
"""

from __future__ import annotations

import os
import uuid
import pytest
from pathlib import Path

from backend.config import JobManagerSettings
from backend.db import DatabaseManager, DatabaseConnectionError, DatabaseError
from backend.jobs import JobManager, JobState
from backend.tests.test_jobs import make_runner_factory

TEST_DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres@127.0.0.1:5432/multi_agent_research"
)


@pytest.fixture
def isolated_schema():
    """Create a temporary PostgreSQL schema for the test and drop it when done."""
    schema_name = f"test_schema_{uuid.uuid4().hex[:12]}"
    db = DatabaseManager(TEST_DATABASE_URL, schema=schema_name)
    try:
        db.init_db()
    except Exception as exc:
        pytest.skip(f"PostgreSQL not available at {TEST_DATABASE_URL}: {exc}")
    try:
        yield db, schema_name
    finally:
        try:
            with db._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE;")
                conn.commit()
        except Exception:
            pass


def test_db_connection_health_check(isolated_schema):
    """health_check returns True when operational."""
    db, _ = isolated_schema
    assert db.health_check() is True


def test_db_connection_failure():
    """Unreachable database triggers DatabaseConnectionError clearly."""
    bad_db = DatabaseManager("postgresql://postgres@127.0.0.1:54329/nonexistent_db_xyz")
    assert bad_db.health_check() is False
    with pytest.raises(DatabaseConnectionError):
        bad_db.init_db()


def test_db_job_submission_and_get(isolated_schema):
    """Job submissions are recorded and queryable in PostgreSQL."""
    db, _ = isolated_schema
    job_id = uuid.uuid4().hex
    topic = "PostgreSQL persistence verification"

    db.record_job_submitted(job_id, topic, status="queued", stage="queued")

    snapshot = db.get_job(job_id)
    assert snapshot is not None
    assert snapshot["job_id"] == job_id
    assert snapshot["topic"] == topic
    assert snapshot["state"] == "queued"
    assert snapshot["stage"] == "queued"
    assert snapshot["created_at"] is not None


def test_db_status_transitions_and_run_link(isolated_schema):
    """Lifecycle transitions update status, timestamps, and run links."""
    db, _ = isolated_schema
    job_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    topic = "Lifecycle persistence"

    db.record_job_submitted(job_id, topic, status="queued")
    db.record_job_started(job_id, status="running", stage="searching")

    running_snap = db.get_job(job_id)
    assert running_snap["state"] == "running"
    assert running_snap["stage"] == "searching"
    assert running_snap["started_at"] is not None

    db.record_job_completed(
        job_id,
        status="completed",
        exit_code=0,
        error=None,
        stage="completed",
        run_id=run_id,
        artifact_path=f"/path/to/report_{run_id}.md",
    )

    completed_snap = db.get_job(job_id)
    assert completed_snap["state"] == "completed"
    assert completed_snap["exit_code"] == 0
    assert completed_snap["run_id"] == run_id
    assert completed_snap["finished_at"] is not None

    # Retrieve by run_id
    by_run = db.get_job_by_run_id(run_id)
    assert by_run is not None
    assert by_run["job_id"] == job_id


def test_db_recover_stale_jobs(isolated_schema):
    """Stale jobs left queued or running are transitioned to interrupted on restart."""
    db, _ = isolated_schema
    job1 = uuid.uuid4().hex
    job2 = uuid.uuid4().hex
    job3 = uuid.uuid4().hex

    db.record_job_submitted(job1, "queued job", status="queued")
    db.record_job_submitted(job2, "running job", status="running")
    db.record_job_submitted(job3, "completed job", status="completed")

    recovered = db.recover_stale_jobs()
    assert recovered == 2

    assert db.get_job(job1)["state"] == "interrupted"
    assert db.get_job(job2)["state"] == "interrupted"
    assert db.get_job(job3)["state"] == "completed"


def test_job_manager_persistence_and_restart_recovery(isolated_schema, tmp_path):
    """JobManager persists jobs to PostgreSQL and restores them after manager restart."""
    db, schema_name = isolated_schema
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    records: dict = {}

    settings = JobManagerSettings(
        max_concurrent=1,
        queue_capacity=8,
        timeout_seconds=5.0,
        kill_grace_seconds=0.5,
    )

    # Instance 1: submit and complete a job
    mgr1 = JobManager(
        settings,
        repo_root=tmp_path,
        artifacts_dir=artifacts_dir,
        env={},
        popen_factory=make_runner_factory(records, artifacts_dir),
        db=db,
    )
    mgr1.start()

    job_id = mgr1.submit("test restart recovery topic")
    assert job_id is not None

    # Wait for completion
    timeout = 5.0
    start = 0.0
    while start < timeout:
        snap = mgr1.get(job_id)
        if snap and snap["state"] in {"completed", "failed"}:
            break
        import time
        time.sleep(0.05)
        start += 0.05

    assert mgr1.get(job_id)["state"] == "completed"
    mgr1.shutdown()

    # Instance 2 (simulate backend restart with clean in-memory state):
    db2 = DatabaseManager(TEST_DATABASE_URL, schema=schema_name)
    mgr2 = JobManager(
        settings,
        repo_root=tmp_path,
        artifacts_dir=artifacts_dir,
        env={},
        popen_factory=make_runner_factory(records, artifacts_dir),
        db=db2,
    )
    mgr2.start()

    # In-memory is empty initially:
    assert len(mgr2._jobs) == 0

    # But querying by job_id restores completed metadata from PostgreSQL!
    restored = mgr2.get(job_id)
    assert restored is not None
    assert restored["job_id"] == job_id
    assert restored["topic"] == "test restart recovery topic"
    assert restored["state"] == "completed"

    # list_jobs() lists the historical completed job from PostgreSQL:
    all_jobs = mgr2.list_jobs()
    assert any(j["job_id"] == job_id for j in all_jobs)

    mgr2.shutdown()
