"""
Offline tests for backend.engine_runner.
----------------------------------------
Every test that reaches the runner's engine seams patches
``engine_runner._create_store`` and ``engine_runner._run_pipeline``, so no
engine import, Mesh API call, network request, or real research execution is
possible. Artifact writes are confined to pytest ``tmp_path`` directories.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import engine_runner

RUN_ID = "0123456789abcdef0123456789abcdef"
SAMPLE_REPORT = (
    "# State Space Models\n\n"
    "Mamba-2 achieves 8x faster inference than Transformers [1].\n\n"
    "## Sources\n[1] https://arxiv.example/abs/2405.21060\n"
)

REAL_ARTIFACTS_DIR = Path(engine_runner.__file__).resolve().parents[1] / "evidence_runs"


def _fake_store(run_id):
    return SimpleNamespace(run_id=run_id, filepath=Path(f"/fake/evidence_{run_id}.json"))


def _patch_seams(
    monkeypatch,
    *,
    store=None,
    store_error=None,
    report=SAMPLE_REPORT,
    pipeline_error=None,
):
    calls = {"pipeline_invocations": []}

    def fake_create(artifacts_dir):
        calls["artifacts_dir"] = artifacts_dir
        if store_error is not None:
            raise store_error
        return store

    def fake_pipeline(topic, active_store):
        calls["pipeline_invocations"].append((topic, active_store))
        if pipeline_error is not None:
            raise pipeline_error
        return report

    monkeypatch.setattr(engine_runner, "_create_store", fake_create)
    monkeypatch.setattr(engine_runner, "_run_pipeline", fake_pipeline)
    return calls


def test_successful_run_passes_topic_and_store(monkeypatch, tmp_path):
    fake_store = _fake_store(RUN_ID)
    calls = _patch_seams(monkeypatch, store=fake_store)

    rc = engine_runner.run("quantum error correction", tmp_path)

    assert rc == 0
    assert calls["artifacts_dir"] == tmp_path
    assert calls["pipeline_invocations"] == [("quantum error correction", fake_store)]


def test_run_id_marker_is_first_and_only_stdout_line(monkeypatch, tmp_path, capsys):
    _patch_seams(monkeypatch, store=_fake_store(RUN_ID))

    rc = engine_runner.run("topic", tmp_path)

    assert rc == 0
    assert capsys.readouterr().out.splitlines() == [f"__RUN_ID__{RUN_ID}"]


def test_report_named_for_run_id_and_written_verbatim(monkeypatch, tmp_path):
    _patch_seams(monkeypatch, store=_fake_store(RUN_ID))

    rc = engine_runner.run("topic", tmp_path)

    assert rc == 0
    report_path = tmp_path / f"research_report_{RUN_ID}.md"
    assert report_path.read_bytes() == SAMPLE_REPORT.encode("utf-8")
    assert [p.name for p in tmp_path.glob("research_report_*.md")] == [report_path.name]


def test_pipeline_failure_returns_nonzero_and_writes_no_report(
    monkeypatch, tmp_path, capsys
):
    fake_store = _fake_store(RUN_ID)
    calls = _patch_seams(
        monkeypatch,
        store=fake_store,
        pipeline_error=RuntimeError("Mesh API unavailable"),
    )

    rc = engine_runner.run("topic", tmp_path)

    assert rc == 1
    assert calls["pipeline_invocations"] == [("topic", fake_store)]
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [f"__RUN_ID__{RUN_ID}"]
    assert "research pipeline failed" in captured.err
    assert "RuntimeError" in captured.err
    assert "Mesh API unavailable" in captured.err
    assert not (tmp_path / f"research_report_{RUN_ID}.md").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "bad_run_id",
    ["", None, "not-a-uuid", "A" * 32, "0" * 31, "0" * 33, "g" * 32],
)
def test_invalid_run_id_fails_before_pipeline_invocation(
    monkeypatch, tmp_path, capsys, bad_run_id
):
    calls = _patch_seams(monkeypatch, store=_fake_store(bad_run_id))

    rc = engine_runner.run("topic", tmp_path)

    assert rc == 1
    assert calls["pipeline_invocations"] == []
    captured = capsys.readouterr()
    assert "__RUN_ID__" not in captured.out
    assert "invalid run ID" in captured.err
    assert not (tmp_path / f"research_report_{RUN_ID}.md").exists()


def test_store_creation_failure_returns_nonzero_without_marker(
    monkeypatch, tmp_path, capsys
):
    calls = _patch_seams(monkeypatch, store_error=OSError("disk offline"))

    rc = engine_runner.run("topic", tmp_path)

    assert rc == 1
    captured = capsys.readouterr()
    assert "__RUN_ID__" not in captured.out
    assert "failed to create evidence store" in captured.err
    assert "disk offline" in captured.err
    assert calls["pipeline_invocations"] == []


def test_non_string_report_fails_without_writing_file(monkeypatch, tmp_path, capsys):
    _patch_seams(monkeypatch, store=_fake_store(RUN_ID), report=123)

    rc = engine_runner.run("topic", tmp_path)

    assert rc == 1
    assert "instead of report text" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_isolated_artifacts_dir_never_touches_real_evidence_runs(monkeypatch, tmp_path):
    if not REAL_ARTIFACTS_DIR.is_dir():
        pytest.skip("real evidence_runs/ directory not present")
    before = sorted(p.name for p in REAL_ARTIFACTS_DIR.iterdir())
    _patch_seams(monkeypatch, store=_fake_store(RUN_ID))

    rc = engine_runner.run("topic", tmp_path)

    after = sorted(p.name for p in REAL_ARTIFACTS_DIR.iterdir())
    assert rc == 0
    assert after == before
    assert (tmp_path / f"research_report_{RUN_ID}.md").exists()


def test_main_resolves_artifacts_dir_from_env_and_strips_topic(
    monkeypatch, tmp_path, capsys
):
    artifacts = tmp_path / "artifacts"
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)
    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", str(artifacts))
    fake_store = _fake_store(RUN_ID)
    calls = _patch_seams(monkeypatch, store=fake_store)

    rc = engine_runner.main(["--topic", "  spaced topic  "])

    assert rc == 0
    assert calls["artifacts_dir"] == artifacts.resolve()
    assert calls["pipeline_invocations"] == [("spaced topic", fake_store)]
    report_path = artifacts / f"research_report_{RUN_ID}.md"
    assert report_path.read_text(encoding="utf-8") == SAMPLE_REPORT
    assert capsys.readouterr().out.splitlines() == [f"__RUN_ID__{RUN_ID}"]


def test_resolve_artifacts_dir_precedence(monkeypatch, tmp_path):
    repo_root = engine_runner.resolve_repo_root()
    monkeypatch.delenv("RESEARCH_ARTIFACTS_DIR", raising=False)
    monkeypatch.delenv("EVIDENCE_DIR", raising=False)
    assert engine_runner.resolve_artifacts_dir(repo_root) == (
        repo_root / "evidence_runs"
    ).resolve()

    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "engine"))
    assert engine_runner.resolve_artifacts_dir(repo_root) == (
        tmp_path / "engine"
    ).resolve()

    monkeypatch.setenv("RESEARCH_ARTIFACTS_DIR", str(tmp_path / "frontend"))
    assert engine_runner.resolve_artifacts_dir(repo_root) == (
        tmp_path / "frontend"
    ).resolve()


def test_main_requires_topic_argument(capsys):
    with pytest.raises(SystemExit) as excinfo:
        engine_runner.main([])

    assert excinfo.value.code == 2
    assert "__RUN_ID__" not in capsys.readouterr().out


def test_main_rejects_blank_topic_before_engine_use(monkeypatch, capsys):
    calls = _patch_seams(monkeypatch, store=_fake_store(RUN_ID))

    rc = engine_runner.main(["--topic", "   "])

    assert rc == 2
    assert calls["pipeline_invocations"] == []
    assert "artifacts_dir" not in calls
    captured = capsys.readouterr()
    assert "__RUN_ID__" not in captured.out
    assert "non-empty" in captured.err
