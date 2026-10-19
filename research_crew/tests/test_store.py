"""
tests/test_store.py
-------------------
Unit tests for research_crew.evidence.store (EvidenceStore, generate_run_id).
Synthetic fixtures and mocks only; no external network I/O.
"""

import json
from dataclasses import replace
from pathlib import Path
import pytest

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore, generate_run_id
from tests.conftest import (
    SAMPLE_REDIRECT_FINAL_URL,
    SAMPLE_TEXT,
    SAMPLE_URL,
    SAMPLE_URL_2,
    SNIPPET_TEXT,
)


def test_generate_run_id():
    """generate_run_id produces unique 32-hex-char strings."""
    id1 = generate_run_id()
    id2 = generate_run_id()
    assert id1 != id2
    assert len(id1) == 32
    assert all(c in "0123456789abcdef" for c in id1)


def test_store_creation_unique_run_id(tmp_path):
    """EvidenceStore.create generates unique run IDs and non-overlapping paths."""
    store1 = EvidenceStore.create(output_dir=tmp_path)
    store2 = EvidenceStore.create(output_dir=tmp_path)

    assert store1.run_id != store2.run_id
    assert store1.filepath != store2.filepath
    assert store1.filepath.name.startswith("evidence_")
    assert store1.filepath.name.endswith(".json")


def test_store_creation_file_exists_error(tmp_path, monkeypatch):
    """If a file for the generated run_id already exists, FileExistsError is raised."""
    fixed_id = "0123456789abcdef0123456789abcdef"
    monkeypatch.setattr("research_crew.evidence.store.generate_run_id", lambda: fixed_id)

    collision_file = tmp_path / f"evidence_{fixed_id}.json"
    collision_file.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError, match="UUID collision"):
        EvidenceStore.create(output_dir=tmp_path)


def test_store_add_and_lookup(populated_store, full_text_record, snippet_record):
    """Store accurately indexes records and supports id, url, and iteration lookups."""
    assert len(populated_store) == 2

    # Lookup by evidence_id
    rec1 = populated_store.get(full_text_record.evidence_id)
    assert rec1 == full_text_record

    rec2 = populated_store.get(snippet_record.evidence_id)
    assert rec2 == snippet_record

    # Unknown ID lookup
    assert populated_store.get("nonexistent_id") is None

    # Lookup by requested URL and final URL
    assert populated_store.get_by_url(SAMPLE_URL) == full_text_record
    assert populated_store.get_by_url(SAMPLE_REDIRECT_FINAL_URL) == full_text_record
    assert populated_store.get_by_url(SAMPLE_URL_2) == snippet_record
    assert populated_store.get_by_url("https://unknown.example") is None

    # Iteration and all_records
    records = list(populated_store)
    assert len(records) == 2
    assert populated_store.all_records() == [full_text_record, snippet_record]


# ---------------------------------------------------------------------------
# Duplicate source handling tests
# ---------------------------------------------------------------------------

def test_store_upgrade_snippet_to_full_text(tmp_path):
    """A subsequent FULL_TEXT retrieval upgrades a SNIPPET_ONLY record, preserving snippet provenance."""
    store = EvidenceStore.create(output_dir=tmp_path)

    # 1. Add snippet first
    rec_snip = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=SNIPPET_TEXT,
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    store.add(rec_snip)
    assert len(store) == 1
    assert store.get(rec_snip.evidence_id).retrieval_status == RetrievalStatus.SNIPPET_ONLY

    # 2. Add full text for same URL
    rec_full = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=SAMPLE_TEXT,
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(rec_full)

    # Must upgrade in-place without duplicating records
    assert len(store) == 1
    upgraded = store.get(rec_full.evidence_id)
    assert upgraded.retrieval_status == RetrievalStatus.FULL_TEXT
    assert upgraded.captured_text == SAMPLE_TEXT
    # Snippet provenance preserved!
    assert upgraded.snippet_text == SNIPPET_TEXT


def test_store_full_text_not_downgraded_by_snippet(tmp_path, full_text_record):
    """An incoming SNIPPET_ONLY fetch does not downgrade an existing FULL_TEXT record."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)

    # Incoming snippet for the same URL
    incoming_snip = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=SNIPPET_TEXT,
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    store.add(incoming_snip)

    assert len(store) == 1
    rec = store.get(full_text_record.evidence_id)
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.captured_text == full_text_record.captured_text
    assert rec.snippet_text == SNIPPET_TEXT


def test_store_idempotent_duplicate_full_text(tmp_path, full_text_record):
    """Adding an identical record twice is an idempotent no-op."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)
    store.add(full_text_record)
    assert len(store) == 1


def test_store_rejects_conflicting_content_same_url(tmp_path, full_text_record):
    """Adding conflicting content for the same URL raises ValueError to prevent silent replacement."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)

    conflicting_record = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text="Completely different contradictory content for the same paper URL.",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )

    with pytest.raises(ValueError, match="Conflicting.*retrieval content"):
        store.add(conflicting_record)


def test_store_failed_fetch_does_not_overwrite_successful_record(tmp_path, full_text_record):
    """A subsequent failed fetch does not overwrite an already retrieved record."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)

    failed_incoming = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text="",
        retrieval_status=RetrievalStatus.FAILED,
    )
    store.add(failed_incoming)

    assert len(store) == 1
    rec = store.get(full_text_record.evidence_id)
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.captured_text == SAMPLE_TEXT


def test_store_failed_record_replaced_by_successful_fetch(tmp_path, failed_record, full_text_record):
    """An initial failed fetch is safely replaced when a subsequent fetch succeeds."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(failed_record)
    assert store.get(failed_record.evidence_id).retrieval_status == RetrievalStatus.FAILED

    successful_incoming = EvidenceRecord.from_fetch(
        url=failed_record.source_url,
        captured_text=SAMPLE_TEXT,
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(successful_incoming)

    assert len(store) == 1
    rec = store.get(failed_record.evidence_id)
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.captured_text == SAMPLE_TEXT


# ---------------------------------------------------------------------------
# Atomic persistence & run protection tests
# ---------------------------------------------------------------------------

def test_store_atomic_flush_and_load(flushed_store, full_text_record, snippet_record):
    """Flushed store writes atomic JSON to disk which can be reloaded cleanly."""
    path = flushed_store.filepath
    assert path.exists()

    raw_data = json.loads(path.read_text(encoding="utf-8"))
    assert raw_data["version"] == 1
    assert raw_data["run_id"] == flushed_store.run_id
    assert raw_data["record_count"] == 2
    assert len(raw_data["records"]) == 2

    loaded_store = EvidenceStore.load(path)
    assert loaded_store.run_id == flushed_store.run_id
    assert len(loaded_store) == 2
    assert loaded_store.get(full_text_record.evidence_id) == full_text_record
    assert loaded_store.get(snippet_record.evidence_id) == snippet_record


def test_store_flush_refuses_to_overwrite_different_run(tmp_path, full_text_record):
    """flush() raises FileExistsError if target file belongs to a different run_id."""
    # Run 1 creates and flushes file
    store1 = EvidenceStore.create(output_dir=tmp_path)
    store1.add(full_text_record)
    store1.flush()

    # Store 2 artificially pointed to store1's file with a different run_id
    store2 = EvidenceStore(run_id="different_run_id_99999", filepath=store1.filepath)
    with pytest.raises(FileExistsError, match="belonging to run_id="):
        store2.flush()


# ---------------------------------------------------------------------------
# Strict JSON load validation tests
# ---------------------------------------------------------------------------

def test_store_load_rejects_non_dict_root(tmp_path):
    """load() rejects JSON that is not a dictionary."""
    f = tmp_path / "list_root.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="top-level element must be a dict"):
        EvidenceStore.load(f)


def test_store_load_rejects_missing_top_level_field(tmp_path):
    """load() rejects JSON missing required header fields."""
    f = tmp_path / "missing_header.json"
    payload = {
        "version": 1,
        # missing run_id, created_at, record_count, records
    }
    f.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing required field 'run_id'"):
        EvidenceStore.load(f)


def test_store_load_rejects_inconsistent_record_count(tmp_path):
    """load() rejects files where header record_count does not match records array length."""
    f = tmp_path / "count_mismatch.json"
    payload = {
        "version": 1,
        "run_id": "test_run",
        "created_at": "2026-01-01T00:00:00Z",
        "record_count": 5,  # claimed 5
        "records": [],      # actual 0
    }
    f.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Inconsistent record count"):
        EvidenceStore.load(f)


def test_store_load_rejects_duplicate_evidence_id(tmp_path, full_text_record):
    """load() rejects JSON containing duplicate evidence_id entries."""
    f = tmp_path / "dup_id.json"
    payload = {
        "version": 1,
        "run_id": "test_run",
        "created_at": "2026-01-01T00:00:00Z",
        "record_count": 2,
        "records": [full_text_record.to_dict(), full_text_record.to_dict()],
    }
    f.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate evidence_id found in loaded store"):
        EvidenceStore.load(f)


def test_store_load_nonexistent_file(tmp_path):
    """Attempting to load a non-existent file raises FileNotFoundError."""
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError):
        EvidenceStore.load(missing)


def test_store_load_unsupported_version(tmp_path):
    """Loading a file with unsupported version raises ValueError."""
    bad_ver_file = tmp_path / "bad_version.json"
    payload = {
        "version": 999,
        "run_id": "test_run",
        "created_at": "2026-01-01T00:00:00Z",
        "record_count": 0,
        "records": [],
    }
    bad_ver_file.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="Unsupported evidence store version"):
        EvidenceStore.load(bad_ver_file)


def test_store_load_corrupted_json(tmp_path):
    """Loading malformed JSON raises json.JSONDecodeError."""
    corrupt_file = tmp_path / "corrupted.json"
    corrupt_file.write_text("{ broken json content...", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        EvidenceStore.load(corrupt_file)


# ---------------------------------------------------------------------------
# Phase 2D: Canonical URL matching and precedence regression tests
# ---------------------------------------------------------------------------

def test_html_snippet_then_pdf_fulltext_lookup_precedence(tmp_path):
    """HTML snippet followed by PDF full-text retrieval for the same paper.
    Lookup using the HTML URL returns the higher-precedence full-text record."""
    store = EvidenceStore.create(output_dir=tmp_path)
    html_url = "https://arxiv.org/html/2404.03715"
    pdf_url = "https://arxiv.org/pdf/2404.03715"

    rec_snip = EvidenceRecord.from_fetch(
        url=html_url,
        captured_text="Short abstract snippet from search API...",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    store.add(rec_snip)

    rec_full = EvidenceRecord.from_fetch(
        url=pdf_url,
        captured_text="Comprehensive full empirical findings and metrics text...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(rec_full)

    # Both records are stored
    assert len(store) == 2

    # Lookup using the HTML URL returns the better full-text record
    res_html = store.get_by_url(html_url)
    assert res_html is not None
    assert res_html.retrieval_status == RetrievalStatus.FULL_TEXT
    assert res_html.source_url == pdf_url
    assert "Comprehensive full" in res_html.captured_text


def test_pdf_lookup_when_both_records_exist(tmp_path):
    """PDF lookup returns full text when both snippet and full-text records exist."""
    store = EvidenceStore.create(output_dir=tmp_path)
    html_url = "https://arxiv.org/html/2404.03715/"
    pdf_url = "https://arxiv.org/pdf/2404.03715.pdf"
    abs_url = "https://arxiv.org/abs/2404.03715"

    rec_snip = EvidenceRecord.from_fetch(
        url=html_url,
        captured_text="Author metadata and title snippet...",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    rec_full = EvidenceRecord.from_fetch(
        url=pdf_url,
        captured_text="Detailed methodology and experimental results...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(rec_snip)
    store.add(rec_full)

    assert len(store) == 2

    # PDF lookup returns full text
    res_pdf = store.get_by_url(pdf_url)
    assert res_pdf is not None
    assert res_pdf.retrieval_status == RetrievalStatus.FULL_TEXT
    assert res_pdf.source_url == pdf_url

    # Abstract endpoint lookup also resolves to the full text
    res_abs = store.get_by_url(abs_url)
    assert res_abs is not None
    assert res_abs.retrieval_status == RetrievalStatus.FULL_TEXT
    assert res_abs.source_url == pdf_url


def test_different_paper_ids_remain_separate(tmp_path):
    """Different arXiv paper IDs remain separate entities and do not match each other."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_paper_a = "https://arxiv.org/abs/2404.03715"
    url_paper_b = "https://arxiv.org/abs/2501.01880"

    rec_a = EvidenceRecord.from_fetch(
        url=url_paper_a,
        captured_text="Paper A full text...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    rec_b = EvidenceRecord.from_fetch(
        url=url_paper_b,
        captured_text="Paper B snippet...",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    store.add(rec_a)
    store.add(rec_b)

    assert len(store) == 2

    res_a = store.get_by_url("https://arxiv.org/pdf/2404.03715.pdf")
    assert res_a is not None
    assert res_a.source_url == url_paper_a

    res_b = store.get_by_url("https://arxiv.org/html/2501.01880")
    assert res_b is not None
    assert res_b.source_url == url_paper_b

    # Lookup of an un-ingested third paper ID returns None
    assert store.get_by_url("https://arxiv.org/abs/2407.16833") is None


def test_different_explicit_arxiv_versions_remain_distinguishable(tmp_path):
    """Different explicit arXiv versions (e.g. v1 vs v2) remain distinguishable."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_v1 = "https://arxiv.org/abs/2404.03715v1"
    url_v2 = "https://arxiv.org/abs/2404.03715v2"

    rec_v1 = EvidenceRecord.from_fetch(
        url=url_v1,
        captured_text="Version 1 text with initial findings...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    rec_v2 = EvidenceRecord.from_fetch(
        url=url_v2,
        captured_text="Version 2 text with updated benchmarks...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(rec_v1)
    store.add(rec_v2)

    assert len(store) == 2

    # Lookup for v1 matches v1 (HTML, PDF, or abs)
    res_v1_pdf = store.get_by_url("https://arxiv.org/pdf/2404.03715v1.pdf")
    assert res_v1_pdf is not None
    assert res_v1_pdf.source_url == url_v1
    assert "Version 1" in res_v1_pdf.captured_text

    # Lookup for v2 matches v2 (HTML, PDF, or abs)
    res_v2_html = store.get_by_url("https://arxiv.org/html/2404.03715v2/")
    assert res_v2_html is not None
    assert res_v2_html.source_url == url_v2
    assert "Version 2" in res_v2_html.captured_text


def test_get_by_url_same_tier_prefers_exact_match(tmp_path):
    """Within the same status tier, an exact URL match is preferred over canonical-only match."""
    store = EvidenceStore.create(output_dir=tmp_path)
    pdf_url = "https://arxiv.org/pdf/2404.03715.pdf"
    abs_url = "https://arxiv.org/abs/2404.03715"

    rec1 = EvidenceRecord.from_fetch(
        url=abs_url,
        captured_text="Abstract page text...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    rec2 = EvidenceRecord.from_fetch(
        url=pdf_url,
        captured_text="PDF document text...",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(rec1)
    store.add(rec2)

    # When querying with pdf_url, rec2 is exact match -> rec2 returned
    res = store.get_by_url(pdf_url)
    assert res is not None
    assert res.source_url == pdf_url
    assert "PDF document text" in res.captured_text

    # When querying with abs_url, rec1 is exact match -> rec1 returned
    res = store.get_by_url(abs_url)
    assert res is not None
    assert res.source_url == abs_url
    assert "Abstract page text" in res.captured_text


# ---------------------------------------------------------------------------
# Phase 2V.4: title provenance across snippet-to-full-text upgrades
# ---------------------------------------------------------------------------

def test_phase2v4_upgrade_replaces_conflicting_snippet_title_with_full_text_heading(tmp_path):
    """Snippet->full-text upgrade must not retain a snippet title that conflicts with the full-text heading."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.00099"

    snippet = EvidenceRecord.from_fetch(
        url=url,
        captured_text="DiskANN provides accurate nearest-neighbor search at billion-point scale.",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    snippet = replace(
        snippet,
        title="Rand-NSG: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node",
    )
    store.add(snippet)

    full = EvidenceRecord.from_fetch(
        url=url,
        captured_text=(
            "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node\n\n"
            "We present DiskANN, a graph-based indexing approach that achieves high recall."
        ),
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(full)

    upgraded = store.get(full.evidence_id)
    assert upgraded is not None
    assert upgraded.retrieval_status == RetrievalStatus.FULL_TEXT
    assert upgraded.title == (
        "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node"
    )
