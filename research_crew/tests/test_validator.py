"""
tests/test_validator.py
-----------------------
Unit tests for research_crew.evidence.validator:
- validate_passage
- validate_record_integrity
- ValidationOutcome, ValidationResult (retrieval completeness & passage IDs)

Synthetic fixtures and mocks only; no external network I/O.
"""

import pytest

from research_crew.evidence.model import (
    EvidenceRecord,
    RetrievalStatus,
    make_passage_id,
)
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import (
    ValidationOutcome,
    ValidationResult,
    validate_passage,
    validate_record_integrity,
)
from tests.conftest import (
    SAMPLE_REDIRECT_FINAL_URL,
    SAMPLE_TEXT,
    SAMPLE_URL,
    SAMPLE_URL_2,
    SAMPLE_URL_3,
    SNIPPET_TEXT,
)


def test_validate_passage_valid_full_text(populated_store, full_text_record):
    """Valid passage within FULL_TEXT captured text must pass validation and expose full-text completeness."""
    passage = "94.2% accuracy on SQuAD 2.0"
    res = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=passage,
    )
    assert res.is_valid is True
    assert res.outcome == ValidationOutcome.VALID
    assert res.retrieval_status == RetrievalStatus.FULL_TEXT
    assert res.is_full_text is True
    assert res.is_snippet_only is False
    assert res.evidence_id == full_text_record.evidence_id
    assert res.passage == passage
    assert res.passage_id == make_passage_id(full_text_record.evidence_id, passage)


def test_validate_passage_valid_snippet_only(populated_store, snippet_record):
    """Valid passage within SNIPPET_ONLY captured text must pass and expose snippet completeness."""
    passage = "state-of-the-art results on NLP benchmarks"
    res = validate_passage(
        store=populated_store,
        evidence_id=snippet_record.evidence_id,
        claimed_url=SAMPLE_URL_2,
        passage=passage,
    )
    assert res.is_valid is True
    assert res.outcome == ValidationOutcome.VALID
    assert res.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert res.is_full_text is False
    assert res.is_snippet_only is True


def test_validate_passage_redirect_final_url_match(populated_store, full_text_record):
    """Validation succeeds when caller supplies the final redirected URL instead of requested URL."""
    passage = "94.2% accuracy on SQuAD 2.0"
    res = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_REDIRECT_FINAL_URL,  # Redirected URL
        passage=passage,
    )
    assert res.is_valid is True
    assert res.outcome == ValidationOutcome.VALID
    assert res.is_full_text is True


def test_validate_passage_preserved_snippet_after_upgrade(tmp_path):
    """A passage present in the preserved search snippet validates with SNIPPET_ONLY status even after upgrade."""
    store = EvidenceStore.create(output_dir=tmp_path)

    # 1. Snippet record added
    unique_snippet_phrase = "Unique search engine summary of benchmark results"
    rec_snip = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=unique_snippet_phrase,
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
    )
    store.add(rec_snip)

    # 2. Upgraded to full text (which does not contain the search engine's unique summary phrase)
    rec_full = EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=SAMPLE_TEXT,
        retrieval_status=RetrievalStatus.FULL_TEXT,
    )
    store.add(rec_full)

    # Passage from historical snippet still validates, correctly marked as SNIPPET_ONLY
    res = validate_passage(
        store=store,
        evidence_id=rec_snip.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=unique_snippet_phrase,
    )
    assert res.is_valid is True
    assert res.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert res.is_snippet_only is True


def test_validate_passage_unknown_evidence_id(populated_store):
    """Fabricated or unknown evidence_id must be rejected with UNKNOWN_EVIDENCE_ID."""
    fabricated_id = "f" * 32
    res = validate_passage(
        store=populated_store,
        evidence_id=fabricated_id,
        claimed_url=SAMPLE_URL,
        passage="94.2% accuracy",
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.UNKNOWN_EVIDENCE_ID
    assert "No record found" in res.detail


def test_validate_passage_url_mismatch(populated_store, full_text_record):
    """Evidence with mismatched URL must be rejected with URL_MISMATCH."""
    mismatched_url = "https://fabricated-domain.example/paper"
    res = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=mismatched_url,
        passage="94.2% accuracy",
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.URL_MISMATCH
    assert "URL mismatch" in res.detail


@pytest.mark.parametrize("empty_passage", ["", "   ", "\t\n  "])
def test_validate_passage_empty_or_whitespace(populated_store, full_text_record, empty_passage):
    """Empty or whitespace-only passage must be rejected with EMPTY_PASSAGE."""
    res = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=empty_passage,
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.EMPTY_PASSAGE
    assert "empty or whitespace-only" in res.detail


def test_validate_passage_tampered_text_invalid_hash(tmp_path, full_text_record):
    """Post-capture tampering of captured_text must trigger INVALID_HASH rejection."""
    tampered_record = EvidenceRecord.from_dict({
        "evidence_id": full_text_record.evidence_id,
        "source_url": full_text_record.source_url,
        "retrieval_status": full_text_record.retrieval_status.value,
        "captured_text": full_text_record.captured_text + " [TAMPERED_INJECTION]",
        "content_hash": full_text_record.content_hash,  # original hash, now mismatched
        "title": full_text_record.title,
    })

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(tampered_record)

    res = validate_passage(
        store=store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage="94.2% accuracy",
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.INVALID_HASH
    assert "Content hash mismatch" in res.detail


def test_validate_passage_failed_retrieval(store_with_failed, failed_record):
    """A FAILED retrieval holds no valid source text; must return FAILED_RETRIEVAL."""
    res = validate_passage(
        store=store_with_failed,
        evidence_id=failed_record.evidence_id,
        claimed_url=SAMPLE_URL_3,
        passage="Could not fetch README",
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.FAILED_RETRIEVAL
    assert "retrieval_status=failed" in res.detail


def test_validate_passage_not_in_captured_text(populated_store, full_text_record):
    """Passage absent from captured text must be rejected with PASSAGE_NOT_IN_TEXT."""
    hallucinated_passage = "We achieve 99.9% accuracy on SQuAD 2.0"
    res = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=hallucinated_passage,
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.PASSAGE_NOT_IN_TEXT
    assert "Passage is not a substring" in res.detail


def test_validate_passage_exact_case_sensitive(populated_store, full_text_record):
    """Literal substring validation is case-sensitive."""
    case_altered = "we achieve 94.2% accuracy"
    res = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=case_altered,
    )
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.PASSAGE_NOT_IN_TEXT


def test_validate_record_integrity_valid(full_text_record):
    """validate_record_integrity confirms valid hash without requiring an EvidenceStore."""
    res = validate_record_integrity(full_text_record)
    assert res.is_valid is True
    assert res.outcome == ValidationOutcome.VALID
    assert res.evidence_id == full_text_record.evidence_id


def test_validate_record_integrity_tampered(full_text_record):
    """validate_record_integrity detects tampered hash or content."""
    tampered = EvidenceRecord.from_dict({
        "evidence_id": full_text_record.evidence_id,
        "source_url": full_text_record.source_url,
        "retrieval_status": full_text_record.retrieval_status.value,
        "captured_text": full_text_record.captured_text,
        "content_hash": "0" * 64,  # Bad hash
        "title": full_text_record.title,
    })
    res = validate_record_integrity(tampered)
    assert res.is_valid is False
    assert res.outcome == ValidationOutcome.INVALID_HASH


def test_provenance_vs_truth_distinction(populated_store, full_text_record):
    """Deterministic validation establishes consistency and provenance, not factual truth.

    A factually true statement not present in the captured text fails provenance check.
    A benchmark metric present in the captured text passes provenance check.
    """
    external_fact = "Einstein published the theory of general relativity in 1915."
    res_external = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=external_fact,
    )
    assert res_external.is_valid is False
    assert res_external.outcome == ValidationOutcome.PASSAGE_NOT_IN_TEXT

    captured_statement = "Baseline BERT achieves 86.3% on the same benchmark."
    res_captured = validate_passage(
        store=populated_store,
        evidence_id=full_text_record.evidence_id,
        claimed_url=SAMPLE_URL,
        passage=captured_statement,
    )
    assert res_captured.is_valid is True
