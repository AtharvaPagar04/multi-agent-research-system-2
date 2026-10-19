"""
tests/test_model.py
-------------------
Unit tests for research_crew.evidence.model (EvidenceRecord, PassageRecord, RetrievalStatus).
Synthetic fixtures and mocks only; no external network I/O.
"""

from dataclasses import FrozenInstanceError
import pytest

from research_crew.evidence.model import (
    EvidenceRecord,
    PassageRecord,
    RetrievalStatus,
    canonicalize_url,
    make_content_hash,
    make_evidence_id,
    make_passage_id,
    parse_arxiv_id,
)
from research_crew.tools.tool_result import ToolFetchResult
from tests.conftest import (
    SAMPLE_REDIRECT_FINAL_URL,
    SAMPLE_TEXT,
    SAMPLE_TITLE,
    SAMPLE_URL,
    SAMPLE_URL_2,
    SAMPLE_URL_3,
    SNIPPET_TEXT,
)


def test_make_evidence_id_deterministic():
    """make_evidence_id should produce a stable 32-hex-char digest."""
    id1 = make_evidence_id(SAMPLE_URL)
    id2 = make_evidence_id(SAMPLE_URL)
    assert id1 == id2
    assert len(id1) == 32
    assert all(c in "0123456789abcdef" for c in id1)


def test_make_evidence_id_distinct_for_different_urls():
    """Different URLs must produce distinct evidence IDs."""
    id1 = make_evidence_id(SAMPLE_URL)
    id2 = make_evidence_id(SAMPLE_URL_2)
    assert id1 != id2


def test_source_id_vs_passage_id_separation():
    """Source-level evidence_id must be distinct from passage-level passage_id."""
    ev_id = make_evidence_id(SAMPLE_URL)
    passage = "94.2% accuracy on SQuAD 2.0"
    p_id = make_passage_id(ev_id, passage)

    assert ev_id != p_id
    assert len(p_id) == 32
    assert all(c in "0123456789abcdef" for c in p_id)

    # Different passages under same source produce different passage IDs
    p_id2 = make_passage_id(ev_id, "Baseline BERT achieves 86.3%")
    assert p_id != p_id2


def test_extract_passage_record(full_text_record):
    """extract_passage extracts a typed PassageRecord with offsets and IDs."""
    target_passage = "94.2% accuracy on SQuAD 2.0"
    prec = full_text_record.extract_passage(target_passage)

    assert isinstance(prec, PassageRecord)
    assert prec.evidence_id == full_text_record.evidence_id
    assert prec.text == target_passage
    assert prec.char_offset == full_text_record.captured_text.index(target_passage)
    assert prec.passage_id == make_passage_id(full_text_record.evidence_id, target_passage)


def test_extract_passage_rejects_missing_or_empty(full_text_record):
    """extract_passage raises ValueError for absent or empty passages."""
    with pytest.raises(ValueError, match="empty or whitespace-only"):
        full_text_record.extract_passage("   ")

    with pytest.raises(ValueError, match="does not occur in captured_text"):
        full_text_record.extract_passage("Nonexistent hallucinated passage")


def test_make_content_hash_digest():
    """make_content_hash should produce a full 64-hex-char SHA-256 hash."""
    h = make_content_hash(SAMPLE_TEXT)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)
    empty_hash = make_content_hash("")
    assert empty_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_from_fetch_full_text(full_text_record):
    """FULL_TEXT record should retain captured text and accurate content hash."""
    assert full_text_record.source_url == SAMPLE_URL
    assert full_text_record.retrieval_status == RetrievalStatus.FULL_TEXT
    assert full_text_record.captured_text == SAMPLE_TEXT
    assert full_text_record.final_url == SAMPLE_REDIRECT_FINAL_URL
    assert full_text_record.title == SAMPLE_TITLE
    assert full_text_record.evidence_id == make_evidence_id(SAMPLE_URL)
    assert full_text_record.content_hash == make_content_hash(SAMPLE_TEXT)


def test_from_fetch_snippet_only(snippet_record):
    """SNIPPET_ONLY record should retain snippet and snippet content hash."""
    assert snippet_record.source_url == SAMPLE_URL_2
    assert snippet_record.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert snippet_record.captured_text == SNIPPET_TEXT
    assert snippet_record.content_hash == make_content_hash(SNIPPET_TEXT)


def test_from_fetch_failed_discards_error_string(failed_record):
    """FAILED record must enforce empty captured text and never store tool error strings."""
    assert failed_record.source_url == SAMPLE_URL_3
    assert failed_record.retrieval_status == RetrievalStatus.FAILED
    assert failed_record.captured_text == ""
    assert failed_record.content_hash == make_content_hash("")


def test_record_immutability(full_text_record):
    """EvidenceRecord is frozen; attribute mutations must be rejected."""
    with pytest.raises(FrozenInstanceError):
        full_text_record.captured_text = "Mutated text"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        full_text_record.content_hash = "fake_hash"  # type: ignore


def test_dict_serialization_roundtrip(full_text_record):
    """Serialization to dict and reconstruction from dict must preserve all data."""
    data = full_text_record.to_dict()
    assert isinstance(data, dict)
    assert data["evidence_id"] == full_text_record.evidence_id
    assert data["source_url"] == full_text_record.source_url
    assert data["retrieval_status"] == "full_text"
    assert data["captured_text"] == full_text_record.captured_text
    assert data["content_hash"] == full_text_record.content_hash
    assert data["final_url"] == full_text_record.final_url
    assert data["title"] == full_text_record.title

    reconstructed = EvidenceRecord.from_dict(data)
    assert reconstructed == full_text_record


def test_from_dict_validation_rejects_missing_fields():
    """from_dict raises ValueError when required fields are missing."""
    invalid = {
        "evidence_id": "abc",
        "source_url": "https://example.com",
        "retrieval_status": "full_text",
        # missing captured_text and content_hash
    }
    with pytest.raises(ValueError, match="missing required field"):
        EvidenceRecord.from_dict(invalid)


def test_from_dict_validation_rejects_invalid_status():
    """from_dict raises ValueError when retrieval_status is not a valid enum value."""
    invalid = {
        "evidence_id": "abc",
        "source_url": "https://example.com",
        "retrieval_status": "not_a_valid_status",
        "captured_text": "sample",
        "content_hash": "123",
    }
    with pytest.raises(ValueError, match="Invalid retrieval_status"):
        EvidenceRecord.from_dict(invalid)


def test_matches_url_requested_and_final(full_text_record):
    """matches_url matches both the original requested URL and redirected final URL."""
    assert full_text_record.matches_url(SAMPLE_URL) is True
    assert full_text_record.matches_url(SAMPLE_REDIRECT_FINAL_URL) is True
    assert full_text_record.matches_url("  " + SAMPLE_URL + "  ") is True
    assert full_text_record.matches_url("https://other.example/abs/2401.00001") is False


def test_from_tool_result_integration():
    """EvidenceRecord.from_tool_result constructs valid record from typed tool boundary result."""
    # Success case
    tool_succ = ToolFetchResult.success(
        url=SAMPLE_URL,
        content=SAMPLE_TEXT,
        final_url=SAMPLE_REDIRECT_FINAL_URL,
        title=SAMPLE_TITLE,
    )
    rec_succ = EvidenceRecord.from_tool_result(tool_succ)
    assert rec_succ.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec_succ.captured_text == SAMPLE_TEXT
    assert rec_succ.final_url == SAMPLE_REDIRECT_FINAL_URL

    # Failure case: ensures error message is never stored as captured_text
    tool_fail = ToolFetchResult.failure(
        url=SAMPLE_URL_3,
        error_message="Connection timed out after 30s",
    )
    rec_fail = EvidenceRecord.from_tool_result(tool_fail)
    assert rec_fail.retrieval_status == RetrievalStatus.FAILED
    assert rec_fail.captured_text == ""
    assert rec_fail.content_hash == make_content_hash("")


def test_parse_arxiv_id_variants():
    """parse_arxiv_id extracts canonical IDs across /abs/, /html/, /pdf/ and mirror hosts."""
    assert parse_arxiv_id("https://arxiv.org/abs/2404.03715") == "2404.03715"
    assert parse_arxiv_id("https://arxiv.org/html/2404.03715") == "2404.03715"
    assert parse_arxiv_id("https://arxiv.org/pdf/2404.03715.pdf") == "2404.03715"
    assert parse_arxiv_id("https://arxiv.org/pdf/2404.03715") == "2404.03715"
    assert parse_arxiv_id("http://export.arxiv.org/abs/2404.03715/") == "2404.03715"
    assert parse_arxiv_id("https://ar5iv.labs.arxiv.org/html/2404.03715") == "2404.03715"

    # Pre-2007 archive identifiers
    assert parse_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"
    assert parse_arxiv_id("https://arxiv.org/pdf/math/0309136v1.pdf") == "math/0309136v1"

    # Explicit versions are preserved in identifier
    assert parse_arxiv_id("https://arxiv.org/abs/2404.03715v1") == "2404.03715v1"
    assert parse_arxiv_id("https://arxiv.org/html/2404.03715v2/") == "2404.03715v2"
    assert parse_arxiv_id("https://arxiv.org/abs/2404.03715v1") != parse_arxiv_id("https://arxiv.org/abs/2404.03715v2")

    # Non-arXiv and invalid URLs return None
    assert parse_arxiv_id("https://example.com/abs/2404.03715") is None
    assert parse_arxiv_id("https://arxiv.org/help/contact") is None
    assert parse_arxiv_id("") is None


def test_canonicalize_url_normalization():
    """canonicalize_url normalizes trailing slashes, equivalent arXiv endpoints, and ports."""
    canon = "https://arxiv.org/abs/2404.03715"
    assert canonicalize_url("https://arxiv.org/abs/2404.03715") == canon
    assert canonicalize_url("https://arxiv.org/html/2404.03715") == canon
    assert canonicalize_url("https://arxiv.org/pdf/2404.03715.pdf") == canon
    assert canonicalize_url("https://arxiv.org/html/2404.03715/") == canon

    # Web URL trailing slash normalization
    assert canonicalize_url("https://github.com/org/repo/") == "https://github.com/org/repo"
    assert canonicalize_url("https://github.com/org/repo") == "https://github.com/org/repo"

    # Distinct versions remain distinguishable
    assert canonicalize_url("https://arxiv.org/abs/2404.03715v1") == "https://arxiv.org/abs/2404.03715v1"
    assert canonicalize_url("https://arxiv.org/pdf/2404.03715v2.pdf") == "https://arxiv.org/abs/2404.03715v2"
    assert canonicalize_url("https://arxiv.org/abs/2404.03715v1") != canonicalize_url("https://arxiv.org/abs/2404.03715v2")


def test_matches_url_equivalent_arxiv_forms():
    """matches_url matches across equivalent arXiv URLs while keeping versions and IDs separate."""
    rec = EvidenceRecord.from_fetch(
        url="https://arxiv.org/html/2404.03715",
        captured_text="Synthetic full content",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    # Equivalent representations match
    assert rec.matches_url("https://arxiv.org/pdf/2404.03715.pdf") is True
    assert rec.matches_url("https://arxiv.org/pdf/2404.03715") is True
    assert rec.matches_url("https://arxiv.org/abs/2404.03715") is True
    assert rec.matches_url("https://arxiv.org/html/2404.03715/") is True
    assert rec.matches_url("http://export.arxiv.org/abs/2404.03715") is True

    # Different versions do NOT match
    assert rec.matches_url("https://arxiv.org/abs/2404.03715v1") is False
    assert rec.matches_url("https://arxiv.org/html/2404.03715v2") is False

    # Different paper IDs do NOT match
    assert rec.matches_url("https://arxiv.org/abs/2501.01880") is False

    # Different non-arxiv domains do NOT match
    assert rec.matches_url("https://other.example/abs/2404.03715") is False


# ---------------------------------------------------------------------------
# Phase 2V.4: provider title vs captured content heading
# ---------------------------------------------------------------------------

def test_phase2v4_from_fetch_replaces_conflicting_provider_title_with_heading():
    """A provider title that conflicts with the captured document heading is replaced."""
    record = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=(
            "## DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node\n\n"
            "We present DiskANN, a graph-based indexing approach that achieves high recall."
        ),
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
        title="Rand-NSG: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node",
    )

    assert record.title == (
        "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node"
    )


def test_phase2v4_from_fetch_replaces_conflicting_provider_title_with_plain_first_line():
    """A plain-text capture (PDF extract) whose first line is the title replaces a conflicting provider title."""
    record = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=(
            "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node\n\n"
            "We present DiskANN, a graph-based indexing approach that achieves high recall."
        ),
        retrieval_status=RetrievalStatus.FULL_TEXT,
        title="Rand-NSG: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node",
    )

    assert record.title == (
        "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node"
    )


def test_phase2v4_from_fetch_preserves_consistent_provider_title():
    """A provider title consistent with the captured heading is preserved unchanged."""
    record = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text="# HNSW\n\nHierarchical Navigable Small World graphs enable fast search.",
        retrieval_status=RetrievalStatus.FULL_TEXT,
        title="HNSW — Wikipedia",
    )

    assert record.title == "HNSW — Wikipedia"


def test_phase2v4_from_fetch_preserves_title_when_content_has_no_heading():
    """Title resolution is a no-op when the captured text exposes no trustworthy title."""
    record = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=SAMPLE_TEXT,
        retrieval_status=RetrievalStatus.FULL_TEXT,
        title=SAMPLE_TITLE,
    )

    assert record.title == SAMPLE_TITLE


def test_phase2v4_from_tool_result_sanitizes_conflicting_fetch_title():
    """Records built from provider tool results carry the content-derived title, not the conflicting provider title."""
    result = ToolFetchResult.success(
        SAMPLE_URL,
        (
            "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node\n\n"
            "We present DiskANN, a graph-based indexing approach that achieves high recall."
        ),
        title="Rand-NSG: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node",
    )
    record = EvidenceRecord.from_tool_result(result, provider="exa")

    assert record.title == (
        "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node"
    )
