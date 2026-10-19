"""
backend/engine_runner.py
------------------------
Subprocess entry point for invoking the research engine pipeline.

Protocol contract (consumed by the future subprocess-based job manager):

* The first stdout line is exactly ``__RUN_ID__<32-hex-run-id>`` and is
  flushed before the research pipeline is executed.
* On success the Markdown report returned by the engine is written verbatim
  to ``research_report_<run_id>.md`` inside the artifact directory, which
  matches the frontend's artifact discovery rules.
* Diagnostics are written to stderr; the exit code is nonzero on failure.

Engine imports are performed lazily inside ``_create_store`` and
``_run_pipeline`` so offline tests can monkeypatch both seams without
importing the engine or performing any network access.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

if TYPE_CHECKING:
    from research_crew.evidence.store import EvidenceStore

RUN_ID_MARKER_PREFIX = "__RUN_ID__"
_REPORT_FILE_TEMPLATE = "research_report_{run_id}.md"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def resolve_repo_root() -> Path:
    """Return the repository root, honouring the RESEARCH_REPO_ROOT override."""
    override = os.getenv("RESEARCH_REPO_ROOT")
    if override and override.strip():
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def resolve_artifacts_dir(repo_root: Path) -> Path:
    """Return the artifact directory shared with the engine and the frontend.

    Precedence: RESEARCH_ARTIFACTS_DIR, then EVIDENCE_DIR, then
    ``<repo_root>/evidence_runs``.
    """
    for var in ("RESEARCH_ARTIFACTS_DIR", "EVIDENCE_DIR"):
        override = os.getenv(var)
        if override and override.strip():
            return Path(override).expanduser().resolve()
    return (repo_root / "evidence_runs").resolve()


def _create_store(artifacts_dir: Path) -> "EvidenceStore":
    from research_crew.evidence.store import EvidenceStore

    return EvidenceStore.create(output_dir=artifacts_dir)


def _run_pipeline(topic: str, store: "EvidenceStore") -> str:
    from research_crew.main import run_research_pipeline

    return run_research_pipeline(topic, store=store)


def _fail(message: str) -> int:
    print(f"[engine_runner] {message}", file=sys.stderr, flush=True)
    return 1


def run(
    topic: str,
    artifacts_dir: Path,
    store_factory: Optional[Callable[[Path], "EvidenceStore"]] = None,
    pipeline_runner: Optional[Callable[[str, "EvidenceStore"], str]] = None,
) -> int:
    """Execute one research run for *topic* against *artifacts_dir*.

    Returns the process exit code (0 on success, 1 on failure).
    """
    factory = store_factory if store_factory is not None else _create_store
    runner = pipeline_runner if pipeline_runner is not None else _run_pipeline

    try:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        store = factory(artifacts_dir)
    except Exception as exc:
        return _fail(f"failed to create evidence store: {type(exc).__name__}: {exc}")

    run_id = getattr(store, "run_id", None)
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id):
        return _fail(
            "evidence store returned an invalid run ID (expected 32 lowercase hex "
            "chars); refusing to start the research pipeline"
        )

    print(f"{RUN_ID_MARKER_PREFIX}{run_id}", flush=True)

    try:
        report = runner(topic, store)
    except Exception as exc:
        return _fail(f"research pipeline failed: {type(exc).__name__}: {exc}")

    if not isinstance(report, str):
        return _fail(
            f"research pipeline returned {type(report).__name__} instead of report text"
        )

    report_path = artifacts_dir / _REPORT_FILE_TEMPLATE.format(run_id=run_id)
    try:
        report_path.write_text(report, encoding="utf-8")
    except OSError as exc:
        return _fail(f"failed to write report to {report_path}: {exc}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="engine_runner",
        description="Run one research engine pipeline invocation and persist its report.",
        epilog='Example: python backend/engine_runner.py --topic "state space models"',
    )
    parser.add_argument(
        "--topic",
        required=True,
        help="Research topic to investigate (non-empty).",
    )
    args = parser.parse_args(argv)

    topic = args.topic.strip()
    if not topic:
        print("[engine_runner] --topic must be a non-empty string", file=sys.stderr, flush=True)
        return 2

    repo_root = resolve_repo_root()
    artifacts_dir = resolve_artifacts_dir(repo_root)
    return run(topic, artifacts_dir)


if __name__ == "__main__":
    sys.exit(main())
