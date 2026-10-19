"""
tests/test_adapters.py
----------------------
Unit tests for Phase 1B tool adapters and pipeline integration:
- ExaSearchAdapter, TavilySearchAdapter
- PDFExtractorAdapter, WebParserAdapter
- Duplicate URL handling across providers with conflicting snippets
- Snippet-to-full-text upgrade through adapter flow
- Persistence on failure paths
- Pure offline tests with synthetic fixtures and mocks only; no network calls.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import validate_passage
from research_crew.main import run_research_pipeline
from research_crew.tools.adapters import (
    ExaSearchAdapter,
    PDFExtractorAdapter,
    TavilySearchAdapter,
    WebParserAdapter,
    create_adapted_extractor_tools,
    create_adapted_search_tools,
)
from research_crew.tools.pdf_extractor import PDFExtractorTool
from research_crew.tools.search_tool import ExaSearchTool, TavilySearchTool
from research_crew.tools.web_parser import WebParserTool


# ---------------------------------------------------------------------------
# Synthetic search data
# ---------------------------------------------------------------------------

EXA_SEARCH_JSON = json.dumps([
    {
        "title": "Attention Is All You Need",
        "url": "https://arxiv.example/abs/1706.03762",
        "source_type": "paper",
        "published_date": "2017-06-12",
        "snippet": "The dominant sequence transduction models are based on complex recurrent or convolutional neural networks.",
    },
    {
        "title": "BERT Pre-training",
        "url": "https://arxiv.example/abs/1810.04805",
        "source_type": "paper",
        "published_date": "2018-10-11",
        "snippet": "We introduce a new language representation model called BERT.",
    },
])

TAVILY_SEARCH_JSON = json.dumps([
    {
        "title": "Attention Is All You Need (Tavily)",
        "url": "https://arxiv.example/abs/1706.03762",
        "source_type": "paper",
        "published_date": "2017-06-12",
        "snippet": "Transformers dispense with recurrence and convolutions entirely, relying solely on an attention mechanism.",
    },
])


# ---------------------------------------------------------------------------
# Search Adapter Tests
# ---------------------------------------------------------------------------

def test_exa_search_adapter_records_snippets(tmp_path):
    """ExaSearchAdapter captures search snippets into EvidenceStore as SNIPPET_ONLY."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=ExaSearchTool)
    mock_tool._run.return_value = EXA_SEARCH_JSON

    adapter = ExaSearchAdapter(store=store, tool=mock_tool)
    output = adapter._run(query="transformer architecture", num_results=2)

    assert output == EXA_SEARCH_JSON
    mock_tool._run.assert_called_once_with(query="transformer architecture", num_results=2)

    assert len(store) == 2
    rec1 = store.get_by_url("https://arxiv.example/abs/1706.03762")
    assert rec1 is not None
    assert rec1.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert rec1.provider == "exa"
    assert "sequence transduction models" in rec1.captured_text


def test_tavily_search_adapter_ignores_error_responses(tmp_path):
    """TavilySearchAdapter ignores error responses and does not store them as evidence."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=TavilySearchTool)
    mock_tool._run.return_value = json.dumps({"error": "TAVILY_API_KEY not set in environment"})

    adapter = TavilySearchAdapter(store=store, tool=mock_tool)
    output = adapter._run(query="test query")

    assert "TAVILY_API_KEY not set" in output
    # Must NOT record anything in store
    assert len(store) == 0


def test_duplicate_snippets_across_providers_merge_gracefully(tmp_path):
    """Differing snippets for the same URL across Exa and Tavily are merged without crashing."""
    store = EvidenceStore.create(output_dir=tmp_path)

    mock_exa = MagicMock(spec=ExaSearchTool)
    mock_exa._run.return_value = EXA_SEARCH_JSON
    exa_adapter = ExaSearchAdapter(store=store, tool=mock_exa)
    exa_adapter._run(query="transformers")

    mock_tavily = MagicMock(spec=TavilySearchTool)
    mock_tavily._run.return_value = TAVILY_SEARCH_JSON
    tavily_adapter = TavilySearchAdapter(store=store, tool=mock_tavily)
    tavily_adapter._run(query="transformers")

    # Record was merged, not duplicated
    assert len(store) == 2
    rec = store.get_by_url("https://arxiv.example/abs/1706.03762")
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert "exa" in rec.provider and "tavily" in rec.provider
    assert len(rec.snippet_observations) == 2

    # Passage from Exa snippet validates
    res_exa = validate_passage(
        store=store,
        evidence_id=rec.evidence_id,
        claimed_url=rec.source_url,
        passage="sequence transduction models are based on complex recurrent",
    )
    assert res_exa.is_valid is True
    assert res_exa.is_snippet_only is True

    # Passage from Tavily snippet validates
    res_tavily = validate_passage(
        store=store,
        evidence_id=rec.evidence_id,
        claimed_url=rec.source_url,
        passage="Transformers dispense with recurrence and convolutions entirely",
    )
    assert res_tavily.is_valid is True
    assert res_tavily.is_snippet_only is True


# ---------------------------------------------------------------------------
# PDF Extractor Adapter Tests
# ---------------------------------------------------------------------------

def test_pdf_extractor_adapter_success_and_arxiv_redirect(tmp_path):
    """PDFExtractorAdapter records FULL_TEXT and computes arxiv /pdf/ final URL."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=PDFExtractorTool)
    pdf_content = "We demonstrate that self-attention mechanisms achieve superior quality on WMT 2014 translation."
    mock_tool._run.return_value = pdf_content

    adapter = PDFExtractorAdapter(store=store, tool=mock_tool)
    raw_url = "https://arxiv.example/abs/1706.03762"
    output = adapter._run(url=raw_url)

    assert output == pdf_content
    rec = store.get_by_url(raw_url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.final_url == "https://arxiv.example/pdf/1706.03762.pdf"
    assert rec.captured_text == pdf_content
    assert rec.provider == "pdf_extractor"


def test_pdf_extractor_adapter_legacy_error_recorded_as_failed(tmp_path):
    """Legacy PyMuPDF error string is classified as FAILED and never stored as content."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=PDFExtractorTool)
    mock_tool._run.return_value = "Error: PyMuPDF (fitz) is not installed. Run: pip install PyMuPDF"

    adapter = PDFExtractorAdapter(store=store, tool=mock_tool)
    url = "https://arxiv.example/abs/1706.03762"
    output = adapter._run(url=url)

    assert "Error: PyMuPDF" in output
    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FAILED
    assert rec.captured_text == ""


def test_pdf_extractor_adapter_empty_content_recorded_as_failed(tmp_path):
    """Empty PDF content is classified as FAILED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=PDFExtractorTool)
    mock_tool._run.return_value = "   \n\t "

    adapter = PDFExtractorAdapter(store=store, tool=mock_tool)
    url = "https://arxiv.example/abs/empty.0001"
    adapter._run(url=url)

    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FAILED
    assert rec.captured_text == ""


def test_pdf_extractor_adapter_exception_recorded_and_reraised(tmp_path):
    """Exception during PDF extraction records FAILED record before re-raising."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=PDFExtractorTool)
    mock_tool._run.side_effect = ConnectionError("Failed to connect to arXiv server")

    adapter = PDFExtractorAdapter(store=store, tool=mock_tool)
    url = "https://arxiv.example/abs/1706.03762"

    with pytest.raises(ConnectionError, match="Failed to connect"):
        adapter._run(url=url)

    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FAILED
    assert rec.captured_text == ""


def test_pdf_extractor_adapter_handles_arxiv_html_url(tmp_path):
    """PDFExtractorAdapter resolves arXiv /html/ URLs to .pdf endpoint, preserving requested citation URL and version."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=PDFExtractorTool)
    paper_text = "We introduce Mamba-2, a state-space model that unifies SSMs and attention mechanisms."
    mock_tool._run.return_value = paper_text

    adapter = PDFExtractorAdapter(store=store, tool=mock_tool)
    html_url = "https://arxiv.org/html/2405.21060v1"
    output = adapter._run(url=html_url)

    assert output == paper_text
    mock_tool._run.assert_called_once_with("https://arxiv.org/pdf/2405.21060v1.pdf")

    rec = store.get_by_url(html_url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.source_url == html_url
    assert rec.final_url == "https://arxiv.org/pdf/2405.21060v1.pdf"
    assert rec.captured_text == paper_text


def test_pdf_extractor_tool_resolves_arxiv_html_url(monkeypatch):
    """PDFExtractorTool._run converts arXiv /html/ URLs to direct PDF endpoint during HTTP fetch."""
    captured_urls = []

    def mock_get(url, **kwargs):
        captured_urls.append(url)
        mock_resp = MagicMock()
        mock_resp.iter_content.return_value = [b"%PDF-1.4 mock pdf content"]
        mock_resp.raise_for_status = MagicMock()
        return mock_resp

    monkeypatch.setattr("requests.get", mock_get)

    mock_doc = MagicMock()
    mock_page = MagicMock()
    mock_page.get_text.return_value = "Mock parsed PDF text from arXiv paper."
    mock_doc.__len__.return_value = 1
    mock_doc.__getitem__.return_value = mock_page

    mock_fitz = MagicMock()
    mock_fitz.open.return_value = mock_doc
    monkeypatch.setitem(__import__("sys").modules, "fitz", mock_fitz)

    tool = PDFExtractorTool()
    result = tool._run("https://arxiv.org/html/2405.21060v2")

    assert "Mock parsed PDF text" in result
    assert captured_urls == ["https://arxiv.org/pdf/2405.21060v2.pdf"]


# ---------------------------------------------------------------------------
# Web Parser Adapter Tests
# ---------------------------------------------------------------------------

def test_web_parser_adapter_success(tmp_path):
    """WebParserAdapter records FULL_TEXT on successful page parse."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=WebParserTool)
    page_text = "PyTorch is an optimized tensor library for deep learning using GPUs and CPUs."
    mock_tool._run.return_value = page_text

    adapter = WebParserAdapter(store=store, tool=mock_tool)
    url = "https://pytorch.example/docs"
    output = adapter._run(url=url)

    assert output == page_text
    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.captured_text == page_text
    assert rec.provider == "web_parser"


def test_web_parser_adapter_github_readme_error_recorded_as_failed(tmp_path):
    """GitHub README failure string is classified as FAILED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=WebParserTool)
    mock_tool._run.return_value = "Could not fetch README for https://github.example/org/repo: 404 Client Error"

    adapter = WebParserAdapter(store=store, tool=mock_tool)
    url = "https://github.example/org/repo"
    output = adapter._run(url=url)

    assert "Could not fetch README" in output
    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FAILED
    assert rec.captured_text == ""


def test_web_parser_adapter_empty_content_recorded_as_failed(tmp_path):
    """Empty webpage content is classified as FAILED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    mock_tool = MagicMock(spec=WebParserTool)
    mock_tool._run.return_value = ""

    adapter = WebParserAdapter(store=store, tool=mock_tool)
    url = "https://blank.example/page"
    adapter._run(url=url)

    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FAILED
    assert rec.captured_text == ""


# ---------------------------------------------------------------------------
# End-to-End Upgrade Flow through Adapters
# ---------------------------------------------------------------------------

def test_adapter_search_snippet_then_pdf_full_text_upgrade(tmp_path):
    """Search adapter records snippet, then PDF adapter upgrades to FULL_TEXT preserving snippet."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/1706.03762"

    # Step 1: Search engine returns snippet
    mock_exa = MagicMock(spec=ExaSearchTool)
    mock_exa._run.return_value = json.dumps([{
        "title": "Attention Paper",
        "url": url,
        "source_type": "paper",
        "snippet": "The dominant sequence transduction models are based on RNNs.",
    }])
    exa_adapter = ExaSearchAdapter(store=store, tool=mock_exa)
    exa_adapter._run(query="attention")

    rec_before = store.get_by_url(url)
    assert rec_before.retrieval_status == RetrievalStatus.SNIPPET_ONLY

    # Step 2: Extractor fetches full PDF
    mock_pdf = MagicMock(spec=PDFExtractorTool)
    full_pdf_text = "We propose the Transformer, a model architecture eschewing recurrence and entirely relying on attention."
    mock_pdf._run.return_value = full_pdf_text
    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf)
    pdf_adapter._run(url=url)

    rec_after = store.get_by_url(url)
    assert rec_after.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec_after.captured_text == full_pdf_text
    assert "dominant sequence transduction models" in rec_after.snippet_text

    # Validation from full text:
    res_full = validate_passage(
        store=store,
        evidence_id=rec_after.evidence_id,
        claimed_url=url,
        passage="We propose the Transformer, a model architecture eschewing recurrence",
    )
    assert res_full.is_valid is True
    assert res_full.is_full_text is True

    # Validation from original snippet:
    res_snip = validate_passage(
        store=store,
        evidence_id=rec_after.evidence_id,
        claimed_url=url,
        passage="dominant sequence transduction models are based on RNNs",
    )
    assert res_snip.is_valid is True
    assert res_snip.is_snippet_only is True


# ---------------------------------------------------------------------------
# Pipeline Integration & Persistence on Failure
# ---------------------------------------------------------------------------

def test_run_research_pipeline_persists_store_on_kickoff_exception(tmp_path, monkeypatch):
    """run_research_pipeline flushes evidence store even when kickoff raises an exception."""
    store = EvidenceStore.create(output_dir=tmp_path)
    # Add a synthetic record before failure
    store.add(EvidenceRecord.from_fetch(
        url="https://arxiv.example/abs/test.001",
        captured_text="Sample text",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    ))

    # Mock _build_llm and Crew.kickoff to simulate a mid-run failure
    from crewai import LLM
    monkeypatch.setattr("research_crew.main._build_llm", lambda: LLM(model="gpt-4o", api_key="dummy_offline_key"))
    with patch("research_crew.main.Crew") as mock_crew_class:
        mock_crew_instance = MagicMock()
        mock_crew_instance.kickoff.side_effect = RuntimeError("Simulated API rate-limit failure mid-pipeline")
        mock_crew_class.return_value = mock_crew_instance

        with pytest.raises(RuntimeError, match="Simulated API rate-limit failure"):
            run_research_pipeline(topic="test topic", store=store)

    # Invariant: Store must be persisted on disk even after exception
    assert store.filepath.exists()
    reloaded = EvidenceStore.load(store.filepath)
    assert len(reloaded) == 1
    assert reloaded.run_id == store.run_id


def test_tool_factory_helpers(tmp_path):
    """create_adapted_search_tools and create_adapted_extractor_tools return configured adapters."""
    store = EvidenceStore.create(output_dir=tmp_path)
    s_tools = create_adapted_search_tools(store)
    e_tools = create_adapted_extractor_tools(store)

    assert len(s_tools) == 2
    assert isinstance(s_tools[0], ExaSearchAdapter)
    assert isinstance(s_tools[1], TavilySearchAdapter)
    assert s_tools[0].store is store

    assert len(e_tools) == 2
    assert isinstance(e_tools[0], PDFExtractorAdapter)
    assert isinstance(e_tools[1], WebParserAdapter)
    assert e_tools[0].store is store


# ---------------------------------------------------------------------------
# Phase 2V.4: provider title vs captured content heading
# ---------------------------------------------------------------------------

def test_phase2v4_exa_adapter_snippet_title_conflicting_with_content_heading(tmp_path):
    """Exa snippets whose provider title conflicts with the captured heading store the content-derived title."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.00077"

    mock_exa = MagicMock(spec=ExaSearchTool)
    mock_exa._run.return_value = json.dumps([{
        "title": "Rand-NSG: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node",
        "url": url,
        "source_type": "paper",
        "snippet": (
            "## DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node\n\n"
            "DiskANN presents a graph-based index for billion-point datasets."
        ),
    }])
    adapter = ExaSearchAdapter(store=store, tool=mock_exa)
    adapter._run(query="diskann")

    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert rec.title == (
        "DiskANN: Fast Accurate Billion-point Nearest Neighbor Search on a Single Node"
    )
