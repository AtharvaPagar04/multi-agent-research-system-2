"""
test_pipeline_flow.py
---------------------
Regression tests for data flow, intermediate validation, context propagation,
and explicit failure handling across the research pipeline.

Requirements covered:
1. Non-empty search results flow into validation.
2. Validated sources flow into extraction.
3. Extracted passages flow into summary task.
4. An empty upstream task output cannot silently yield a placeholder report that looks successful.
5. A genuinely empty-evidence run produces an explicit failure or clearly marked unsuccessful result.
6. Diagnostic interception of [Assistant called tool ...] pseudo-calls.
7. Pure offline tests with synthetic fixtures and mocks only; no network calls.
"""

import json
from unittest.mock import MagicMock, patch
import pytest
from research_crew.pipeline_flow import select_foundation_passages
from crewai.tasks.task_output import TaskOutput

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.main import MeshDiagnosticInterceptor, run_research_pipeline
from research_crew.pipeline_flow import (
    PipelineFlowCoordinator,
    check_synthesizer_context,
    extract_pseudo_tool_calls,
    parse_extracted_passages,
    parse_json_safely,
    parse_search_sources,
    parse_validated_sources,
    recover_extracted_evidence_from_sources,
    recover_search_sources_from_store,
    recover_validated_sources_from_search,
)


# ---------------------------------------------------------------------------
# Synthetic Test Fixtures
# ---------------------------------------------------------------------------

SAMPLE_SEARCH_JSON = json.dumps([
    {
        "title": "Attention Is All You Need",
        "url": "https://arxiv.org/abs/1706.03762",
        "source_type": "paper",
        "published_date": "2017-06-12",
        "snippet": "We introduce the Transformer based solely on attention mechanisms.",
    },
    {
        "title": "FlashAttention",
        "url": "https://arxiv.org/abs/2205.14135",
        "source_type": "paper",
        "published_date": "2022-05-27",
        "snippet": "Fast and memory-efficient exact attention with IO-awareness.",
    },
])

SAMPLE_VALIDATED_JSON = json.dumps({
    "validated_sources": [
        {
            "title": "Attention Is All You Need",
            "url": "https://arxiv.org/abs/1706.03762",
            "source_type": "paper",
            "published_date": "2017-06-12",
            "snippet": "We introduce the Transformer based solely on attention mechanisms.",
            "score": 10,
            "rationale": "Seminal foundational paper.",
        }
    ]
})

SAMPLE_EXTRACTION_JSON = json.dumps([
    {
        "source_id": "https://arxiv.org/abs/1706.03762",
        "title": "Attention Is All You Need",
        "url": "https://arxiv.org/abs/1706.03762",
        "metrics": ["28.4 BLEU on WMT 2014 English-to-German"],
        "datasets": ["WMT 2014"],
        "key_findings": ["Transformer achieves SOTA translation quality without recurrence."],
        "quotes": ['"The Transformer is the first transduction model relying entirely on self-attention."'],
    }
])


# ---------------------------------------------------------------------------
# Test 1: Non-empty search results flow into validation
# ---------------------------------------------------------------------------

def test_search_results_flow_into_validation(tmp_path):
    """Non-empty search results parse cleanly and populate search_sources for validation."""
    store = EvidenceStore.create(output_dir=tmp_path)
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")

    task_out = TaskOutput(
        name="Search",
        description="Search task",
        expected_output="JSON array",
        raw=SAMPLE_SEARCH_JSON,
        agent="Search Agent",
    )

    coordinator.on_search_completed(task_out)

    assert len(coordinator.search_sources) == 2
    assert coordinator.search_sources[0]["url"] == "https://arxiv.org/abs/1706.03762"
    assert coordinator.search_sources[1]["url"] == "https://arxiv.org/abs/2205.14135"
    assert "https://arxiv.org/abs/1706.03762" in task_out.raw


# ---------------------------------------------------------------------------
# Test 2: Validated sources flow into extraction
# ---------------------------------------------------------------------------

def test_validated_sources_flow_into_extraction(tmp_path):
    """Validated sources parse cleanly and populate validated_sources for extraction."""
    store = EvidenceStore.create(output_dir=tmp_path)
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")

    task_out = TaskOutput(
        name="Validation",
        description="Validation task",
        expected_output="JSON object",
        raw=SAMPLE_VALIDATED_JSON,
        agent="Validator Agent",
    )

    coordinator.on_validation_completed(task_out)

    assert len(coordinator.validated_sources) == 1
    assert coordinator.validated_sources[0]["url"] == "https://arxiv.org/abs/1706.03762"
    assert coordinator.validated_sources[0]["score"] == 10


# ---------------------------------------------------------------------------
# Test 3: Extracted passages flow into summary task
# ---------------------------------------------------------------------------

def test_extracted_passages_flow_into_summary(tmp_path):
    """Extracted evidence passages parse cleanly and establish valid research material for summary."""
    store = EvidenceStore.create(output_dir=tmp_path)
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")

    task_out = TaskOutput(
        name="Extraction",
        description="Extraction task",
        expected_output="JSON array",
        raw=SAMPLE_EXTRACTION_JSON,
        agent="Extractor Agent",
    )

    coordinator.on_extraction_completed(task_out)

    assert len(coordinator.extracted_evidence) == 1
    assert coordinator.has_research_material is True
    assert "28.4 BLEU" in coordinator.extracted_evidence[0]["metrics"][0]


# ---------------------------------------------------------------------------
# Test 4: Search recovery when LLM outputs pseudo-call instead of JSON array
# ---------------------------------------------------------------------------

def test_search_output_recovers_from_store_when_raw_is_pseudo_tool_call(tmp_path):
    """When search agent output is a pseudo-call string, sources in store are recovered into task raw output."""
    store = EvidenceStore.create(output_dir=tmp_path)
    # Add records captured during tool execution
    store.add(EvidenceRecord.from_fetch(
        url="https://arxiv.org/abs/1706.03762",
        captured_text="Attention Is All You Need full text.",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
        title="Attention Is All You Need",
    ))
    store.add(EvidenceRecord.from_fetch(
        url="https://arxiv.org/abs/2205.14135",
        captured_text="FlashAttention full text.",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
        title="FlashAttention",
    ))

    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")

    # Simulate the exact failure mode where gemini emitted [Assistant called tool ...]
    pseudo_call_text = (
        '[Assistant called tool exa_academic_search with arguments '
        '{"query": "Long Range Arena benchmark", "num_results": 8}]'
    )
    task_out = TaskOutput(
        name="Search",
        description="Search task",
        expected_output="JSON array",
        raw=pseudo_call_text,
        agent="Search Agent",
    )

    coordinator.on_search_completed(task_out)

    # Invariant: task_out.raw must be replaced with the 2 recovered sources from store
    assert len(coordinator.search_sources) == 2
    parsed_after = json.loads(task_out.raw)
    assert len(parsed_after) == 2
    assert parsed_after[0]["url"] == "https://arxiv.org/abs/1706.03762"
    assert parsed_after[1]["url"] == "https://arxiv.org/abs/2205.14135"


# ---------------------------------------------------------------------------
# Test 5: Validation recovery when validator outputs 0 sources
# ---------------------------------------------------------------------------

def test_validation_recovers_top_sources_when_validator_returns_empty(tmp_path):
    """When validator outputs empty validated_sources, top search sources are recovered."""
    store = EvidenceStore.create(output_dir=tmp_path)
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")
    coordinator.search_sources = [
        {"title": "P1", "url": "https://arxiv.org/abs/1", "snippet": "Snippet 1"},
        {"title": "P2", "url": "https://arxiv.org/abs/2", "snippet": "Snippet 2"},
    ]

    task_out = TaskOutput(
        name="Validation",
        description="Validation task",
        expected_output="JSON object",
        raw='{"validated_sources": []}',
        agent="Validator Agent",
    )

    coordinator.on_validation_completed(task_out)

    assert len(coordinator.validated_sources) == 2
    assert coordinator.validated_sources[0]["url"] == "https://arxiv.org/abs/1"
    parsed_after = json.loads(task_out.raw)
    assert len(parsed_after["validated_sources"]) == 2


# ---------------------------------------------------------------------------
# Test 6: Extraction recovery when extractor outputs empty array
# ---------------------------------------------------------------------------

def test_extraction_recovers_evidence_when_extractor_returns_empty(tmp_path):
    """When extractor outputs empty list, evidence objects are synthesized from validated sources & store."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(EvidenceRecord.from_fetch(
        url="https://arxiv.org/abs/1",
        captured_text="Transformers eliminate recurrence through self-attention mechanisms.",
        retrieval_status=RetrievalStatus.SNIPPET_ONLY,
        title="Attention Paper",
    ))
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")
    coordinator.validated_sources = [
        {"title": "Attention Paper", "url": "https://arxiv.org/abs/1", "snippet": "Snippet 1"}
    ]

    task_out = TaskOutput(
        name="Extraction",
        description="Extraction task",
        expected_output="JSON array",
        raw="[]",
        agent="Extractor Agent",
    )

    coordinator.on_extraction_completed(task_out)

    assert len(coordinator.extracted_evidence) == 1
    assert coordinator.has_research_material is True
    parsed_after = json.loads(task_out.raw)
    assert len(parsed_after) == 1
    assert parsed_after[0]["url"] == "https://arxiv.org/abs/1"
    assert len(parsed_after[0]["key_findings"]) > 0


# ---------------------------------------------------------------------------
# Test 7: Genuinely empty evidence run raises explicit error
# ---------------------------------------------------------------------------

def test_genuinely_empty_evidence_run_raises_explicit_error(tmp_path):
    """A genuinely empty run with 0 sources in store and search raises an explicit failure."""
    store = EvidenceStore.create(output_dir=tmp_path)
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")
    # Store has 0 records, search_sources is empty
    task_out = TaskOutput(
        name="Extraction",
        description="Extraction task",
        expected_output="JSON array",
        raw="[]",
        agent="Extractor Agent",
    )

    with pytest.raises(RuntimeError, match="zero evidence sources retrieved in run"):
        coordinator.on_extraction_completed(task_out)


# ---------------------------------------------------------------------------
# Test 8: Empty upstream cannot silently yield successful placeholder report
# ---------------------------------------------------------------------------

def test_placeholder_report_is_rejected_as_failure(tmp_path):
    """verify_final_report rejects placeholder reports with explicit failure."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(EvidenceRecord.from_fetch(
        url="https://arxiv.org/abs/1",
        captured_text="Sample text",
        retrieval_status=RetrievalStatus.FULL_TEXT,
    ))
    coordinator = PipelineFlowCoordinator(store=store, topic="transformer attention")

    # The exact placeholder report generated during the failed live run
    failed_report = (
        "# Research Summary: Explain transformer attention\n\n"
        "## Core Foundations & Mechanism\n"
        "*(Note: No evidence was provided in the context to support detailing the core foundations...)*\n\n"
        "## Key Insights\n"
        "*(Note: No evidence was provided in the context to formulate key insights.)*\n\n"
        "## Sources\n*(None)*\n"
    )

    with pytest.raises(RuntimeError, match="placeholder marker"):
        coordinator.verify_final_report(failed_report)


# ---------------------------------------------------------------------------
# Test 9: Mesh Diagnostic Interceptor converts pseudo tool calls
# ---------------------------------------------------------------------------

def test_interceptor_converts_pseudo_tool_calls_to_tool_calls():
    """MeshDiagnosticInterceptor parses [Assistant called tool ...] into standard tool_calls."""
    import httpx

    interceptor = MeshDiagnosticInterceptor()

    raw_body = {
        "id": "chatcmpl-test",
        "choices": [{
            "message": {
                "role": "assistant",
                "content": (
                    '[Assistant called tool exa_academic_search with arguments '
                    '{"query": "transformer attention", "num_results": 8}]'
                ),
                "tool_calls": None,
            },
            "finish_reason": "stop",
        }],
        "usage": {"total_tokens": 50},
    }

    req = httpx.Request("POST", "https://api.meshapi.ai/v1/chat/completions")
    resp = httpx.Response(200, content=json.dumps(raw_body).encode("utf-8"), request=req)

    rewritten_resp = interceptor.on_inbound(resp)

    data = json.loads(rewritten_resp.read())
    choice = data["choices"][0]
    msg = choice["message"]

    assert choice["finish_reason"] == "tool_calls"
    assert msg["content"] is None
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["function"]["name"] == "exa_academic_search"
    args = json.loads(msg["tool_calls"][0]["function"]["arguments"])
    assert args["query"] == "transformer attention"
    assert args["num_results"] == 8


def test_foundation_passages_are_selected_verbatim():
    source = (
        "Introduction to the paper. "
        "Given a query q and keys k, attention computes dot products. "
        "The weights are the softmax of the dot products of the query and keys. "
        "Attention is a weighted sum of the value vectors. "
        "The paper also discusses memory complexity."
    )

    passages = select_foundation_passages(source)

    assert passages
    assert all(passage in source for passage in passages)
    assert any("softmax" in passage for passage in passages)
    assert any("weighted sum of the value vectors" in passage for passage in passages)