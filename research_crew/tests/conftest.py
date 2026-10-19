"""
tests/conftest.py
-----------------
Shared synthetic fixtures for Phase 1A evidence tests.

All data here is synthetic. No network calls, no API keys, no file system
side-effects outside of pytest's tmp_path directories.
"""

import pytest

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore

# ---------------------------------------------------------------------------
# Synthetic constants — never real URLs
# ---------------------------------------------------------------------------

SAMPLE_URL = "https://arxiv.example/abs/2401.00001"
SAMPLE_REDIRECT_FINAL_URL = "https://arxiv.example/pdf/2401.00001.pdf"
SAMPLE_URL_2 = "https://github.example/org/repo"
SAMPLE_URL_3 = "https://docs.example/project/v2"

SAMPLE_TEXT = (
    "We achieve 94.2% accuracy on SQuAD 2.0 using a transformer architecture "
    "with multi-head attention. The model was trained on 256 TPU-v4 chips for "
    "72 hours. Baseline BERT achieves 86.3% on the same benchmark."
)

SNIPPET_TEXT = (
    "Transformer models achieve state-of-the-art results on NLP benchmarks."
)

SAMPLE_TITLE = "Attention Is All You Need (Synthetic)"


# ---------------------------------------------------------------------------
# EvidenceRecord fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def full_text_record() -> EvidenceRecord:
    """A well-formed FULL_TEXT record with synthetic content."""
    return EvidenceRecord.from_fetch(
        url=SAMPLE_URL,
        captured_text=SAMPLE_TEXT,
        retrieval_status=RetrievalStatus.FULL_TEXT,
        final_url=SAMPLE_REDIRECT_FINAL_URL,
        title=SAMPLE_TITLE,
    )


@pytest.fixture
def snippet_record() -> EvidenceRecord:
    """A SNIPPET_ONLY record — partial content, valid for passage checks."""
    return EvidenceRecord.from_fetch(
        url=SAMPLE_URL_2,
        captured_text=SNIPPET_TEXT,
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
        title="Transformers Overview (Synthetic Snippet)",
    )


@pytest.fixture
def failed_record() -> EvidenceRecord:
    """A FAILED record — captured_text is always empty."""
    return EvidenceRecord.from_fetch(
        url=SAMPLE_URL_3,
        captured_text="Could not fetch README for https://docs.example/project/v2: ConnectionError",
        retrieval_status=RetrievalStatus.FAILED,
        title=None,
    )


# ---------------------------------------------------------------------------
# EvidenceStore fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def populated_store(tmp_path, full_text_record, snippet_record) -> EvidenceStore:
    """An in-memory store containing two valid records."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)
    store.add(snippet_record)
    return store


@pytest.fixture
def store_with_failed(tmp_path, full_text_record, failed_record) -> EvidenceStore:
    """An in-memory store containing one valid and one failed record."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)
    store.add(failed_record)
    return store


@pytest.fixture
def flushed_store(tmp_path, full_text_record, snippet_record) -> EvidenceStore:
    """A store that has been flushed to disk (ready for load testing)."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(full_text_record)
    store.add(snippet_record)
    store.flush()
    return store
