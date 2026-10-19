"""
test_feedback_loop.py
---------------------
Deterministic offline unit tests for bounded verification-to-research feedback loop:
- Failed / insufficient claim triggers additional targeted research
- New evidence is captured into EvidenceStore and claims are re-verified
- Early exit when all retained claims become supported
- Exhausted retries do not leave unsupported claims presented as verified
- Labeling vs removal policy enforcement on exhaustion
- Auditable run log records every iteration, query, new evidence ID, and verdict
- Safe failure mode when verification loop encounters unexpected error
"""

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.verifier import (
    ClaimAction,
    ClaimVerdict,
    ClaimVerificationRecord,
    ClaimVerifier,
)
from research_crew.feedback_loop import (
    FeedbackLoopRunLog,
    evaluate_targeted_claim_evidence_gain,
    execute_research_feedback_loop,
    execute_targeted_retrieval,
    identify_research_gaps,
    regenerate_report_with_evidence,
    save_feedback_run_log,
    select_focused_evidence_passages,
)
from research_crew.tools.adapters import PDFExtractorAdapter, WebParserAdapter
from research_crew.tools.pdf_extractor import PDFExtractorTool
from research_crew.tools.tool_result import ToolFetchResult
from research_crew.tools.web_parser import WebParserTool


def test_feedback_loop_triggers_research_and_resolves_gap(tmp_path):
    """Initial unsupported claim triggers targeted retrieval, captures evidence into store, and reverifies as supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "transformer attention mechanisms"

    # Initial draft report with an unsupported claim citing a URL not in store
    new_url = "https://arxiv.org/abs/2307.08691"
    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention-2 optimizes work partitioning across thread blocks [1].\n\n"
        "## Sources\n\n"
        f"[1] {new_url}\n"
    )

    # Mock researcher: when called with queries, returns a new EvidenceRecord for new_url
    def mock_researcher(queries):
        assert len(queries) > 0
        captured_text = (
            "FlashAttention-2 optimizes work partitioning across thread blocks "
            "to achieve up to 2x speedup compared to FlashAttention."
        )
        rec = EvidenceRecord.from_tool_result(
            ToolFetchResult.success(new_url, captured_text, title="FlashAttention-2")
        )
        return [rec]

    # Mock verifier judge: classifies as supported once passage is in store
    def mock_judge(claim_text, passage_text, source_url):
        if "work partitioning" in passage_text:
            return ClaimVerdict.SUPPORTED, "Passage directly confirms work partitioning optimization."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    # Verifications:
    # 1. Store now contains the new evidence record
    assert len(store) == 1
    assert store.get_by_url(new_url) is not None

    # 2. Feedback loop executed 1 round and terminated because all claims are supported
    assert loop_log.total_rounds_executed == 1
    assert loop_log.terminated_reason == "all_claims_supported"
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0
    assert summary.contradicted_claims_count == 0

    # 3. Auditable run log details:
    assert len(loop_log.iterations) == 2  # round 0 (initial) + round 1
    # Round 0: 1 unverified claim
    assert loop_log.iterations[0].iteration_index == 0
    assert loop_log.iterations[0].verdict_breakdown["insufficient_evidence"] == 1
    # Round 1: 1 query issued, 1 new evidence ID captured, 1 supported verdict
    assert loop_log.iterations[1].iteration_index == 1
    assert len(loop_log.iterations[1].queries_issued) > 0
    assert len(loop_log.iterations[1].new_evidence_ids) == 1
    assert loop_log.iterations[1].verdict_breakdown["supported"] == 1

    # 4. Final report contains supported claim without warning badge
    assert "⚠️ Unverified" not in final_report
    assert "FlashAttention-2 optimizes work partitioning" in final_report


def test_feedback_loop_exhausts_retries_and_labels_unresolved_claims(tmp_path):
    """When retries are exhausted without finding supporting evidence, unresolved claims are clearly labeled."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "transformer attention mechanisms"
    fake_url = "https://unverified-claim.org/data"

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "Transformers eliminate the need for matrix multiplications entirely [1].\n\n"
        "## Sources\n\n"
        f"[1] {fake_url}\n"
    )

    # Mock researcher: returns unrelated evidence each round
    round_counter = 0

    def mock_researcher(queries):
        nonlocal round_counter
        round_counter += 1
        url = f"https://unrelated-paper-{round_counter}.org"
        return [
            EvidenceRecord.from_tool_result(
                ToolFetchResult.success(url, f"Unrelated content from round {round_counter}", title=f"Paper {round_counter}")
            )
        ]

    # Mock judge: claim is never substantiated
    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence supports eliminating matrix multiplications."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    # Verification:
    # 1. Budget exhausted after 2 rounds
    assert loop_log.total_rounds_executed == 2
    assert loop_log.terminated_reason == "exhausted_retries"

    # 2. Unresolved claim is NOT presented as verified
    assert summary.supported_claims_count == 0
    assert summary.insufficient_evidence_claims_count == 1
    assert summary.records[0].verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert summary.records[0].action_taken == ClaimAction.LABELED_UNVERIFIED.value

    # 3. Report contains diagnostic warning badge
    assert "⚠️ Unverified claim" in final_report


def test_feedback_loop_exhausts_retries_and_removes_unresolved_claims(tmp_path):
    """When retries are exhausted under action_policy='remove', unresolved claims are excised from report."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "quantum algorithms"
    url = "https://example.com/quantum"

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "Quantum superposition enables exponential state parallelism [1]. "
        "Standard laptops can run Shor algorithm in 2 picoseconds [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    # Store already has text supporting superposition, but nothing supporting 2 picoseconds
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                url,
                "Quantum superposition enables exponential state parallelism across qubits.",
                title="Quantum Primer",
            )
        )
    )

    def mock_researcher(queries):
        # Additional research fails to find evidence for picoseconds claim
        return []

    def mock_judge(claim_text, passage_text, source_url):
        if "superposition" in claim_text:
            return ClaimVerdict.SUPPORTED, "Superposition confirmed."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Laptops cannot run Shor algorithm in picoseconds."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="remove",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    # Verifications:
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 1
    # Supported claim retained:
    assert "exponential state parallelism" in final_report
    # Unsupported claim removed:
    assert "2 picoseconds" not in final_report


def test_feedback_loop_all_claims_supported_exits_immediately(tmp_path):
    """When all draft claims are already supported, loop terminates immediately with 0 additional rounds."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = "Attention mechanisms calculate pairwise dot-product compatibility scores."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani")))

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "Attention mechanisms calculate pairwise dot-product compatibility scores [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    research_called = False

    def mock_researcher(queries):
        nonlocal research_called
        research_called = True
        return []

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Direct match."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic="attention",
        max_rounds=2,
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    assert research_called is False
    assert loop_log.total_rounds_executed == 0
    assert loop_log.terminated_reason == "all_claims_supported"
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0


def test_feedback_loop_auditable_run_log_persistence(tmp_path):
    """Verify that FeedbackLoopRunLog structure captures all details and persists valid JSON."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2205.14135"

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention uses GPU SRAM tiling to minimize HBM IO [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_researcher(queries):
        rec = EvidenceRecord.from_tool_result(
            ToolFetchResult.success(url, "FlashAttention uses GPU SRAM tiling to minimize HBM IO.", title="Dao")
        )
        return [rec]

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Direct match."

    _, _, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic="FlashAttention",
        max_rounds=2,
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    log_path = tmp_path / f"feedback_loop_{store.run_id}.json"
    save_feedback_run_log(log_path, loop_log)

    assert log_path.exists()
    data = json.loads(log_path.read_text(encoding="utf-8"))

    assert data["run_id"] == store.run_id
    assert data["max_rounds"] == 2
    assert data["total_rounds_executed"] == 1
    assert data["terminated_reason"] == "all_claims_supported"
    assert len(data["iterations"]) == 2

    # Check iteration 1 audit fields:
    it1 = data["iterations"][1]
    assert it1["iteration_index"] == 1
    assert len(it1["queries_issued"]) > 0
    assert len(it1["new_evidence_ids"]) == 1
    assert it1["verdict_breakdown"]["supported"] == 1
    assert it1["action_taken"] == "all_claims_supported"


def test_identify_research_gaps_llm_and_fallback():
    """Verify identify_research_gaps handles LLM output and heuristic fallback."""
    rec = ClaimVerificationRecord(
        claim_id="claim_001",
        claim_text="Speculative decoding achieves 2.5x speedup without accuracy degradation.",
        source_url=None,
        passage_id=None,
        passage_text=None,
        verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
        rationale="Missing citation.",
        action_taken=ClaimAction.LABELED_UNVERIFIED.value,
    )

    # 1. Fallback heuristic (when llm is None)
    queries = identify_research_gaps([rec], topic="LLM inference acceleration", llm=None)
    assert len(queries) > 0
    assert "LLM inference acceleration" in queries[0]

    # 2. Mock LLM JSON output
    mock_llm = MagicMock()
    mock_llm.call.return_value = '{"queries": ["speculative decoding benchmark speedup", "speculative drafting verification"]}'
    llm_queries = identify_research_gaps([rec], topic="LLM inference acceleration", llm=mock_llm)
    assert len(llm_queries) == 2
    assert llm_queries[0] == "speculative decoding benchmark speedup"


def test_feedback_loop_fail_safe_on_pipeline_error(tmp_path, monkeypatch):
    """Verify that if execute_research_feedback_loop encounters an exception, pipeline falls back safely."""
    from research_crew.main import run_research_pipeline
    from crewai import Crew

    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = "Attention mechanisms compute representations."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani")))

    report_text = (
        "# Core Foundations & Mechanism\n\n"
        "Attention mechanisms compute representations [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    # Mock Crew.kickoff to return report_text
    monkeypatch.setattr(Crew, "kickoff", lambda self: report_text)
    monkeypatch.setenv("MESH_API_KEY", "mock-mesh-key")

    # Mock execute_research_feedback_loop to raise an unexpected runtime error
    def crashing_loop(*args, **kwargs):
        raise RuntimeError("Simulated transient network failure during feedback retrieval")

    monkeypatch.setattr("research_crew.main.execute_research_feedback_loop", crashing_loop)

    # Pipeline must complete without crashing, falling back safely
    final_output = run_research_pipeline(topic="attention", store=store)

    assert "Attention mechanisms compute representations" in final_output
    assert "Evidence store run ID" in final_output


def test_feedback_loop_tracks_relevant_evidence_gain(tmp_path):
    """Ensure feedback loop explicitly tracks whether each targeted claim gained relevant new evidence."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "transformer attention mechanisms"
    url = "https://arxiv.org/abs/2307.08691"

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention-2 optimizes thread block work partitioning [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_researcher(queries):
        return [
            EvidenceRecord.from_tool_result(
                ToolFetchResult.success(
                    url,
                    "FlashAttention-2 optimizes thread block work partitioning to achieve higher GPU efficiency.",
                    title="FlashAttention-2",
                )
            )
        ]

    def mock_judge(claim_text, passage_text, source_url):
        if "thread block work partitioning" in passage_text:
            return ClaimVerdict.SUPPORTED, "Passage directly confirms thread block work partitioning."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    assert loop_log.total_rounds_executed == 1
    assert loop_log.terminated_reason == "all_claims_supported"

    # Verify iteration 1 contains targeted_claim_resolutions:
    round1_record = loop_log.iterations[1]
    assert len(round1_record.targeted_claim_resolutions) == 1
    res = round1_record.targeted_claim_resolutions[0]

    assert res["gained_relevant_evidence"] is True
    assert res["evidence_id"] in round1_record.new_evidence_ids
    assert res["source_url"] == url
    assert res["verdict_after_recheck"] == "supported"
    assert "Gained relevant new evidence" in res["notes"]


def test_feedback_loop_rejects_duplicate_or_irrelevant_evidence(tmp_path):
    """Do not count duplicate or irrelevant evidence as gap resolution.

    Guarantees:
    1. Irrelevant evidence records return gained_relevant_evidence = False.
    2. Duplicate evidence records return gained_relevant_evidence = False.
    3. Unresolved claims remain unverified and labeled.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "transformer attention mechanisms"
    unsubstantiated_url = "https://example.com/unsubstantiated"

    # Pre-populate store with an existing record (duplicate candidate)
    existing_url = "https://arxiv.org/abs/1706.03762"
    existing_rec = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(existing_url, "Transformer models rely on self-attention mechanisms.")
    )
    store.add(existing_rec)

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "Transformer attention circuits synthesize cold fusion at room temperature [1].\n\n"
        "## Sources\n\n"
        f"[1] {unsubstantiated_url}\n"
    )

    # Round 1: researcher returns an irrelevant record (about sourdough baking)
    # Round 2: researcher returns a duplicate record (existing_rec)
    round_counter = 0

    def mock_researcher(queries):
        nonlocal round_counter
        round_counter += 1
        if round_counter == 1:
            # Irrelevant evidence
            return [
                EvidenceRecord.from_tool_result(
                    ToolFetchResult.success(
                        "https://cooking.com/bread",
                        "Sourdough fermentation requires wild yeast cultures and flour.",
                        title="Baking",
                    )
                )
            ]
        else:
            # Duplicate evidence (already in store)
            return [
                EvidenceRecord.from_tool_result(
                    ToolFetchResult.success(
                        existing_url,
                        "Transformer models rely on self-attention mechanisms.",
                        title="Duplicate",
                    )
                )
            ]

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence supports cold fusion in transformers."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    # 1. Bounded retry limit preserved: executed exactly 2 rounds and terminated safely
    assert loop_log.total_rounds_executed == 2
    assert loop_log.terminated_reason in ("exhausted_retries", "no_new_evidence")

    # 2. Round 1 audit: Irrelevant evidence was NOT counted as gained relevant evidence
    it1 = loop_log.iterations[1]
    assert len(it1.targeted_claim_resolutions) == 1
    assert it1.targeted_claim_resolutions[0]["gained_relevant_evidence"] is False
    assert "irrelevant" in it1.targeted_claim_resolutions[0]["notes"].lower()

    # 3. Round 2 audit: Duplicate evidence did not add new evidence IDs, NOT counted as gained relevant evidence
    it2 = loop_log.iterations[2]
    assert len(it2.new_evidence_ids) == 0
    assert len(it2.targeted_claim_resolutions) == 1
    assert it2.targeted_claim_resolutions[0]["gained_relevant_evidence"] is False

    # 4. Final claim status: gap remains unresolved and is clearly labeled as unverified
    assert summary.supported_claims_count == 0
    assert summary.insufficient_evidence_claims_count == 1
    assert "⚠️ Unverified claim" in final_report


def test_feedback_loop_empty_extraction_does_not_report_success(tmp_path):
    """When a report contains zero extractable claims, the loop must not report all_claims_supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "empty report topic"
    empty_claims_report = (
        "---\n"
        "# Research Summary: Empty Topic\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric | Value | Baseline | Source |\n"
        "|---|---|---|---|\n\n"
        "## Sources\n"
    )

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=empty_claims_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
    )

    assert loop_log.terminated_reason == "no_claims_extracted"
    assert loop_log.terminated_reason != "all_claims_supported"
    assert loop_log.total_rounds_executed == 0
    assert loop_log.final_claims_supported_count == 0
    assert loop_log.final_unresolved_claims_count == 0
    assert summary.total_claims == 0
    assert summary.supported_claims_count == 0
    assert len(summary.warnings) > 0
    assert "no verifiable factual claims" in summary.warnings[0].lower()


def test_feedback_loop_genuine_success_remains_successful(tmp_path):
    """When claims are extracted and all are supported, all_claims_supported is reported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "transformer attention mechanisms"
    url = "https://arxiv.org/abs/1706.03762"
    captured = "Attention mechanisms compute weighted representations across tokens."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani 2017")))

    report = (
        "---\n"
        "# Research Summary: Transformers\n\n"
        "## Core Foundations & Mechanism\n"
        "Attention mechanisms compute weighted representations across tokens [1].\n\n"
        "## Sources\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Passage directly confirms the claim."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
    )

    assert summary.total_claims == 1
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0
    assert loop_log.total_rounds_executed == 0
    assert loop_log.terminated_reason == "all_claims_supported"
    assert loop_log.final_claims_supported_count == 1
    assert loop_log.final_unresolved_claims_count == 0
    assert "⚠️ Unverified" not in final_report


def test_feedback_loop_unresolved_claims_trigger_feedback_behavior(tmp_path):
    """When extracted claims are unresolved, the feedback loop executes research rounds up to max_rounds."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "retrieval augmented generation"
    url1 = "https://arxiv.org/abs/2005.11401"
    url2 = "https://arxiv.org/abs/2312.10997"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, "RAG combines parametric and non-parametric memory.", title="Lewis 2020")))

    initial_report = (
        "---\n"
        "# Research Summary: RAG\n\n"
        "## Core Foundations & Mechanism\n"
        "RAG combines parametric and non-parametric memory [1].\n\n"
        "## Key Insights\n"
        "Advanced RAG uses iterative retrieval trees to resolve complex multi-hop queries [2].\n\n"
        "## Sources\n"
        f"[1] {url1}\n"
        f"[2] {url2}\n"
    )

    research_calls = []

    def mock_researcher(queries):
        research_calls.extend(queries)
        return [
            EvidenceRecord.from_tool_result(
                ToolFetchResult.success(
                    url2,
                    "Advanced RAG uses iterative retrieval trees to resolve complex multi-hop queries.",
                    title="Advanced RAG",
                )
            )
        ]

    def mock_judge(claim_text, passage_text, source_url):
        if "iterative retrieval trees" in claim_text:
            if passage_text and "iterative retrieval trees" in passage_text:
                return ClaimVerdict.SUPPORTED, "Passage confirms iterative retrieval trees."
            return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence yet for iterative retrieval trees."
        return ClaimVerdict.SUPPORTED, "Supported."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
    )

    assert len(research_calls) > 0
    assert loop_log.total_rounds_executed == 1
    assert loop_log.terminated_reason == "all_claims_supported"
    assert summary.supported_claims_count == 2
    assert summary.insufficient_evidence_claims_count == 0
    assert "⚠️ Unverified" not in final_report


# ===========================================================================
# Phase 2B Regression Tests: Coverage Gap Driven Feedback Loop
# ===========================================================================

def test_phase2b_feedback_loop_resolves_coverage_gap(tmp_path):
    """Verify that an uncovered topic dimension triggers targeted retrieval and is resolved by follow-up research."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "Distributed database sharding and consensus protocols"
    shard_url = "https://example.com/sharding"
    raft_url = "https://example.com/raft"

    # Pre-populate store with sharding and throughput evidence
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                shard_url,
                "Consistent hashing and hash partitioning distribute keys across database shards. "
                "Hash partitioning ensures balanced data distribution across cluster nodes. "
                "Throughput reached 50,000 TPS with linear scaling benchmarks.",
                title="Sharding Architecture",
            )
        )
    )

    # Initial report covers sharding and throughput, but consensus protocols is completely absent (coverage gap)
    initial_report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Consistent hashing and hash partitioning distribute keys across database shards [1].\n"
        "Hash partitioning ensures balanced data distribution across cluster nodes [1].\n\n"
        "## Performance\n"
        "Throughput reached 50,000 TPS with linear scaling benchmarks [1].\n\n"
        "## Sources\n"
        f"[1] {shard_url}\n"
    )

    # All claims in draft are supported
    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Supported by sharding evidence."

    research_queries_issued = []

    def mock_researcher(queries):
        research_queries_issued.extend(queries)
        return [
            EvidenceRecord.from_tool_result(
                ToolFetchResult.success(
                    raft_url,
                    "Raft consensus protocol coordinates leader election and log replication across quorum nodes. "
                    "Quorum consensus ensures state machine consistency across database replicas.",
                    title="Raft Consensus",
                )
            )
        ]

    def mock_synthesizer(draft, unverified, new_evidence, top, coverage_gaps=None):
        # Incorporate consensus protocols into report body and cite source under ## Sources
        return (
            f"# Research Summary: {top}\n\n"
            "## Core Foundations & Mechanism\n"
            "Consistent hashing and hash partitioning distribute keys across database shards [1].\n"
            "Hash partitioning ensures balanced data distribution across cluster nodes [1].\n\n"
            "## Consensus Protocols\n"
            "Raft consensus protocol coordinates leader election and log replication across quorum nodes [2].\n"
            "Quorum consensus ensures state machine consistency [2].\n\n"
            "## Performance\n"
            "Throughput reached 50,000 TPS with linear scaling benchmarks [1].\n\n"
            "## Sources\n"
            f"[1] {shard_url}\n"
            f"[2] {raft_url}\n"
        )

    # Explicitly require sharding, throughput, and consensus_protocols
    required_dims = ["data_sharding", "throughput_benchmarks", "consensus_protocols"]

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
        synthesizer_fn=mock_synthesizer,
        required_dimensions=required_dims,
    )

    # Verifications:
    # 1. Feedback loop recognized consensus gap and did NOT exit at round 0 despite all draft claims being supported
    assert loop_log.total_rounds_executed == 1
    assert loop_log.terminated_reason == "all_claims_supported"

    # 2. Targeted queries were issued for the uncovered dimension
    assert len(research_queries_issued) > 0
    assert any("consensus" in q.lower() for q in research_queries_issued)

    # 3. Store now contains new Raft evidence
    assert store.get_by_url(raft_url) is not None

    # 4. Final report contains the resolved consensus dimension
    assert "Raft consensus protocol" in final_report
    assert summary.supported_claims_count > 0
    assert summary.insufficient_evidence_claims_count == 0


def test_phase2b_feedback_loop_no_new_evidence_terminates_safely_with_gaps_visible(tmp_path):
    """Verify that when follow-up research yields zero new evidence, the loop terminates safely with gaps visible."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "Distributed database sharding and consensus protocols"
    shard_url = "https://example.com/sharding"

    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                shard_url,
                "Consistent hashing distributes keys across database shards.",
                title="Sharding Architecture",
            )
        )
    )

    # Report has sharding covered, but consensus_protocols is absent
    initial_report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Consistent hashing distributes keys across database shards [1].\n"
        "Hash partitioning ensures balanced data distribution across cluster nodes [1].\n\n"
        "## Sources\n"
        f"[1] {shard_url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Supported."

    # Retrieval returns NO new evidence
    def mock_researcher(queries):
        return []

    required_dims = ["data_sharding", "consensus_protocols"]

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
        required_dimensions=required_dims,
    )

    # Verifications:
    # 1. Terminated safely with "no_new_evidence"
    assert loop_log.terminated_reason == "no_new_evidence"
    assert loop_log.total_rounds_executed == 1

    # 2. Did NOT falsely report all claims supported / complete coverage
    assert loop_log.terminated_reason != "all_claims_supported"

    # 3. Unresolved coverage gap is preserved in warnings
    assert len(summary.warnings) > 0
    assert any("consensus_protocols" in w for w in summary.warnings)


def test_phase2b_coverage_gaps_prevent_premature_all_claims_supported(tmp_path):
    """Verify that coverage gaps prevent round 0 early exit when all draft claims are supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    topic = "Distributed database sharding and consensus protocols"
    url = "https://example.com/sharding"

    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                url,
                "Data sharding partitions write workloads across nodes.",
                title="Sharding",
            )
        )
    )

    initial_report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Data sharding partitions write workloads across nodes [1].\n\n"
        "## Sources\n"
        f"[1] {url}\n"
    )

    research_invoked = False

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Supported."

    def mock_researcher(queries):
        nonlocal research_invoked
        research_invoked = True
        return []

    required_dims = ["data_sharding", "consensus_protocols"]

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic=topic,
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=mock_researcher,
        required_dimensions=required_dims,
    )

    # If coverage gaps didn't exist, it would exit at round 0 with research_invoked == False.
    # Because consensus_protocols is missing, research MUST be invoked!
    assert research_invoked is True
    assert loop_log.total_rounds_executed >= 1


# ---------------------------------------------------------------------------
# Phase 2E Regression Tests: Targeted Academic Evidence Retrieval
# ---------------------------------------------------------------------------

def test_targeted_retrieval_routes_arxiv_html_to_pdf_extractor(tmp_path):
    """Verify that arXiv /html/ URLs are routed to PDF extractor and not generic web parser."""
    store = EvidenceStore.create(output_dir=tmp_path)
    arxiv_html_url = "https://arxiv.org/html/2404.03715v1"

    # Pre-existing snippet record from search
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                arxiv_html_url,
                "FlashAttention-2 provides faster attention kernels.",
                status=RetrievalStatus.SNIPPET_ONLY,
                title="FlashAttention-2",
            )
        )
    )

    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_pdf_tool._run.return_value = (
        "FlashAttention-2 optimizes work partitioning across thread blocks to reach 73% peak throughput."
    )

    mock_web_tool = MagicMock(spec=WebParserTool)
    mock_web_tool.name = "Webpage Text Parser"

    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)
    web_adapter = WebParserAdapter(store=store, tool=mock_web_tool)

    new_ids = execute_targeted_retrieval(
        queries=[arxiv_html_url],
        store=store,
        extractor_tools=[pdf_adapter, web_adapter],
    )

    # 1. PDF extractor tool was invoked with resolved PDF URL
    mock_pdf_tool._run.assert_called_once_with("https://arxiv.org/pdf/2404.03715v1.pdf")

    # 2. Web parser was NOT called because better-supported PDF route succeeded
    mock_web_tool._run.assert_not_called()

    # 3. Store was upgraded to FULL_TEXT
    rec = store.get_by_url(arxiv_html_url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.final_url == "https://arxiv.org/pdf/2404.03715v1.pdf"
    assert rec.source_url == arxiv_html_url
    assert "73% peak throughput" in rec.captured_text
    assert rec.evidence_id in new_ids


def test_targeted_retrieval_falls_back_to_web_parser_on_pdf_failure(tmp_path):
    """Verify that failed PDF extraction on academic URL falls back safely to web parser."""
    store = EvidenceStore.create(output_dir=tmp_path)
    paper_url = "https://arxiv.org/html/2404.03715v1"

    # Pre-existing snippet record
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                paper_url,
                "FlashAttention snippet.",
                status=RetrievalStatus.SNIPPET_ONLY,
                title="FlashAttention-2",
            )
        )
    )

    # PDF extractor fails
    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_pdf_tool._run.side_effect = ConnectionError("Could not connect to PDF repository")

    # Web parser succeeds as fallback
    mock_web_tool = MagicMock(spec=WebParserTool)
    mock_web_tool.name = "Webpage Text Parser"
    mock_web_tool._run.return_value = "Fallback HTML text: FlashAttention-2 speedups on Hopper GPUs."

    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)
    web_adapter = WebParserAdapter(store=store, tool=mock_web_tool)

    new_ids = execute_targeted_retrieval(
        queries=[paper_url],
        store=store,
        extractor_tools=[pdf_adapter, web_adapter],
    )

    # Both tools were called (primary PDF failed, fallback Web succeeded)
    mock_pdf_tool._run.assert_called_once()
    mock_web_tool._run.assert_called_once_with(paper_url)

    # Store was upgraded to FULL_TEXT via fallback
    rec = store.get_by_url(paper_url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert "Fallback HTML text" in rec.captured_text
    assert rec.evidence_id in new_ids


def test_evaluate_gain_prefers_full_text_over_snippet(tmp_path):
    """Verify that evaluate_targeted_claim_evidence_gain prioritizes FULL_TEXT over SNIPPET_ONLY."""
    store = EvidenceStore.create(output_dir=tmp_path)
    claim_text = "FlashAttention-2 achieves up to 73% theoretical peak throughput on A100."
    claim_rec = ClaimVerificationRecord(
        claim_id="claim_001",
        claim_text=claim_text,
        source_url="https://arxiv.org/abs/2307.08691",
        passage_id=None,
        passage_text=None,
        verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
        rationale="Insufficient evidence.",
        action_taken=ClaimAction.LABELED_UNVERIFIED.value,
    )

    # Record 1: Snippet only
    rec_snip = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            "https://arxiv.org/abs/2307.08691",
            "FlashAttention-2 achieves higher theoretical peak throughput on modern GPUs.",
            status=RetrievalStatus.SNIPPET_ONLY,
            title="Snippet Result",
        )
    )

    # Record 2: Full text with empirical passage
    rec_full = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            "https://arxiv.org/abs/2307.08691v2",
            "Abstract...\n\nResults:\nFlashAttention-2 achieves up to 73% theoretical peak throughput on A100.",
            status=RetrievalStatus.FULL_TEXT,
            title="Full Paper",
        )
    )
    store.add(rec_snip)
    store.add(rec_full)

    # Pass both snippet and full text new IDs
    gained, ev_id, src_url, notes = evaluate_targeted_claim_evidence_gain(
        claim_rec,
        [rec_snip.evidence_id, rec_full.evidence_id],
        store,
    )

    assert gained is True
    assert ev_id == rec_full.evidence_id
    assert src_url == rec_full.source_url
    assert "full_text" in notes


def test_evaluate_gain_rejects_irrelevant_snippet_and_handles_failed_fetches(tmp_path):
    """Verify that irrelevant snippets and failed retrievals return gained_relevant_evidence = False."""
    store = EvidenceStore.create(output_dir=tmp_path)
    claim_rec = ClaimVerificationRecord(
        claim_id="claim_002",
        claim_text="PostgreSQL partitioned tables improve query latency by 45%.",
        source_url="https://postgresql.example/partitioning",
        passage_id=None,
        passage_text=None,
        verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
        rationale="Insufficient evidence.",
        action_taken=ClaimAction.LABELED_UNVERIFIED.value,
    )

    # 1. Irrelevant snippet
    rec_irrel = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            "https://cooking.example/sourdough",
            "Sourdough baking requires bread flour, salt, and water.",
            status=RetrievalStatus.SNIPPET_ONLY,
            title="Baking",
        )
    )
    store.add(rec_irrel)

    gained_irrel, _, _, notes_irrel = evaluate_targeted_claim_evidence_gain(
        claim_rec,
        [rec_irrel.evidence_id],
        store,
    )
    assert gained_irrel is False
    assert "irrelevant" in notes_irrel.lower()

    # 2. Failed retrieval record
    rec_failed = EvidenceRecord.from_tool_result(
        ToolFetchResult.failure(
            "https://timeout.example/paper",
            "Connection timeout",
            provider="web_parser",
        )
    )
    store.add(rec_failed)

    gained_failed, _, _, notes_failed = evaluate_targeted_claim_evidence_gain(
        claim_rec,
        [rec_failed.evidence_id],
        store,
    )
    assert gained_failed is False
    assert "failed" in notes_failed.lower()


def test_select_focused_evidence_passages_prefers_relevant_passage_over_irrelevant():
    """Verify focused passage selection extracts relevant empirical results rather than header/author padding."""
    claim = ClaimVerificationRecord(
        claim_id="claim_003",
        claim_text="FlashAttention-2 optimizes work partitioning across thread blocks to reach 73% peak throughput.",
        source_url="https://arxiv.org/abs/2307.08691",
        passage_id=None,
        passage_text=None,
        verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
        rationale="Insufficient evidence.",
        action_taken=ClaimAction.LABELED_UNVERIFIED.value,
    )

    paper_text = (
        "FlashAttention-2: Faster Attention with Better Parallelism and Work Partitioning\n"
        "Tri Dao, Stanford University, Department of Computer Science.\n"
        "Contact: tridao@stanford.edu. Code available at github.com/Dao-AILab/flash-attention.\n\n"
        "1. Introduction\n"
        "Attention is a fundamental building block of modern large language models. As context lengths grow "
        "from 2K to 32K or 100K tokens, attention becomes a computational bottleneck. Past works have explored "
        "sparse approximations, low-rank factorizations, and linear attention mechanisms.\n\n"
        "2. Empirical Results and Performance Benchmarks\n"
        "FlashAttention-2 optimizes work partitioning across thread blocks to reach 73% peak throughput on A100 GPUs. "
        "Compared to FlashAttention-1, the memory bandwidth overhead is significantly reduced and warp-level scheduling "
        "is streamlined to achieve up to 2x speedup on forward and backward passes across various batch sizes.\n\n"
        "3. Acknowledgments\n"
        "We thank our colleagues and funding agencies for their generous compute grant support."
    )

    rec = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            "https://arxiv.org/abs/2307.08691",
            paper_text,
            status=RetrievalStatus.FULL_TEXT,
            title="FlashAttention-2",
        )
    )

    selected = select_focused_evidence_passages(rec, [claim])

    # Relevant empirical passage was selected
    assert "73% peak throughput on A100 GPUs" in selected
    assert "work partitioning across thread blocks" in selected

    # Author contact and acknowledgment padding were NOT selected
    assert "tridao@stanford.edu" not in selected
    assert "funding agencies" not in selected


def test_regenerate_report_with_evidence_preserves_source_attribution():
    """Verify that regenerate_report_with_evidence formats prompt with source URLs, title, and focused passages."""
    claim = ClaimVerificationRecord(
        claim_id="claim_004",
        claim_text="Mamba-2 achieves 8x faster inference than Transformers on 16k context.",
        source_url="https://arxiv.org/abs/2405.21060",
        passage_id=None,
        passage_text=None,
        verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE,
        rationale="Insufficient evidence.",
        action_taken=ClaimAction.LABELED_UNVERIFIED.value,
    )

    rec = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            "https://arxiv.org/abs/2405.21060",
            "Mamba-2 introduces State Space Duality.\n\nEmpirical benchmarks show Mamba-2 achieves 8x faster inference than Transformers on 16k context.",
            status=RetrievalStatus.FULL_TEXT,
            title="Transformers are SSMs: Generalized Models and Efficient Algorithms Through Structured State Space Duality",
        )
    )

    captured_prompt = None

    class MockLLM:
        def call(self, prompt: str) -> str:
            nonlocal captured_prompt
            captured_prompt = prompt
            return "# Updated Report\n\nMamba-2 achieves 8x faster inference than Transformers on 16k context [1].\n\n## Sources\n[1] https://arxiv.org/abs/2405.21060"

    updated = regenerate_report_with_evidence(
        draft_report="# Draft Report\n\nSome draft text.",
        unverified_claims=[claim],
        new_evidence_records=[rec],
        topic="State Space Models",
        llm=MockLLM(),
    )

    assert "Mamba-2 achieves 8x faster inference" in updated
    assert captured_prompt is not None

    assert "- Source: https://arxiv.org/abs/2405.21060" in captured_prompt
    assert "[full_text]" in captured_prompt
    assert "Title: Transformers are SSMs" in captured_prompt
    assert "8x faster inference than Transformers on 16k context" in captured_prompt


# ===========================================================================
# Phase 2I Regression Tests: Feedback-Loop Query History & Bounded Termination
# ===========================================================================

def test_phase2i_identify_gap_queries_avoids_repeating_executed_queries():
    """Task 3: identify_gap_queries avoids returning previously executed queries."""
    from research_crew.feedback_loop import identify_gap_queries, _normalize_query
    from research_crew.evidence.verifier import ClaimVerificationRecord, ClaimVerdict

    claim = ClaimVerificationRecord(
        claim_id="claim_001",
        claim_text="Direct Alignment Algorithms commonly deteriorate from over-optimization before completing one epoch.",
        source_url="https://arxiv.org/abs/2406.02900",
        passage_id=None,
        passage_text=None,
        verdict=ClaimVerdict.CONTRADICTED,
        rationale="Over-optimization occurs after 1 epoch.",
        action_taken="labeled_contradicted",
    )

    # First iteration queries
    topic = "RLHF vs DPO Alignment"
    q_iter1 = identify_gap_queries([claim], topic=topic)
    assert len(q_iter1) > 0

    # Supply first round queries as executed_queries for round 2
    q_iter2 = identify_gap_queries([claim], topic=topic, executed_queries=q_iter1)

    # Assert that no query from iteration 2 duplicates any query from iteration 1 (case/whitespace normalized)
    iter1_norms = {_normalize_query(q) for q in q_iter1}
    for q in q_iter2:
        assert _normalize_query(q) not in iter1_norms


def test_phase2i_feedback_loop_no_new_evidence_preserves_unresolved_claims_and_gaps(tmp_path):
    """Task 3: Terminating on no_new_evidence preserves unresolved claims and does not falsely claim completion."""
    from research_crew.feedback_loop import execute_research_feedback_loop
    from research_crew.evidence.verifier import ClaimVerdict

    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://example.com/unrelated"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, "Completely unrelated text.")))

    report = (
        "# Research Summary: DPO vs PPO\n\n"
        "## Core Foundations & Mechanism\n"
        "PPO requires 3x higher memory footprint than DPO [1].\n\n"
        "## Sources\n"
        f"[1] {url}\n"
    )

    def mock_verifier(claim_text, passage_text, source_url):
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Passage does not substantiate memory footprint."

    # Retrieval function returns no new records
    def empty_research(queries):
        return []

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=report,
        store=store,
        topic="RLHF vs DPO",
        max_rounds=2,
        verifier_fn=mock_verifier,
        research_fn=empty_research,
    )

    assert loop_log.terminated_reason == "no_new_evidence"
    assert loop_log.final_unresolved_claims_count > 0
    assert summary.insufficient_evidence_claims_count > 0
    assert len(summary.warnings) > 0
    assert any("Unresolved claim after feedback loop" in w for w in summary.warnings)
    # Never reports all_claims_supported when claims remain unverified
    assert loop_log.terminated_reason != "all_claims_supported"


def test_phase2i_feedback_loop_retry_limits_remain_bounded(tmp_path):
    """Task 3: Feedback loop terminates within max_rounds even when claims remain unresolved."""
    from research_crew.feedback_loop import execute_research_feedback_loop
    from research_crew.evidence.verifier import ClaimVerdict

    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://example.com/paper"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, "Sample text.")))

    report = (
        "# Research Summary: Topic\n\n"
        "Claim A requires further proof [1].\n\n"
        "## Sources\n"
        f"[1] {url}\n"
    )

    rounds_called = 0
    def research_dummy(queries):
        nonlocal rounds_called
        rounds_called += 1
        # Add a new record so it does not terminate on no_new_evidence
        new_url = f"https://example.com/new_{rounds_called}"
        r = EvidenceRecord.from_tool_result(ToolFetchResult.success(new_url, "More evidence."))
        store.add(r)
        return [r]

    def mock_verifier(claim_text, passage_text, source_url):
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Still insufficient."

    max_rounds = 2
    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=report,
        store=store,
        topic="RLHF vs DPO",
        max_rounds=max_rounds,
        verifier_fn=mock_verifier,
        research_fn=research_dummy,
    )

    assert loop_log.total_rounds_executed <= max_rounds
    assert loop_log.terminated_reason in ("exhausted_retries", "no_new_evidence")
    assert loop_log.final_unresolved_claims_count > 0


# ===========================================================================
# Task 13 — Upgrade Cited snippet_only Sources During Feedback Rechecks
# ===========================================================================

def _make_upgrade_store(tmp_path, url, snippet_text):
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                url,
                snippet_text,
                status=RetrievalStatus.SNIPPET_ONLY,
                title="FlashAttention-2",
            )
        )
    )
    return store


def test_task13_targeted_retrieval_upgrades_preexisting_snippet(tmp_path):
    """Pre-existing SNIPPET_ONLY records routed via upgrade_urls receive full-text extraction."""
    url = "https://arxiv.org/abs/2404.03715"
    store = _make_upgrade_store(tmp_path, url, "FlashAttention-2 provides faster attention kernels.")
    prior = store.get_by_url(url)
    prior_id = prior.evidence_id

    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_pdf_tool._run.return_value = (
        "FlashAttention-2 optimizes work partitioning across thread blocks to reach 73% peak throughput."
    )
    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)

    new_ids = execute_targeted_retrieval(
        queries=[],
        store=store,
        extractor_tools=[pdf_adapter],
        upgrade_urls=[url],
    )

    mock_pdf_tool._run.assert_called_once_with("https://arxiv.org/pdf/2404.03715.pdf")
    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.evidence_id == prior_id
    assert rec.source_url == url
    assert rec.snippet_text == "FlashAttention-2 provides faster attention kernels."
    assert "73% peak throughput" in rec.captured_text
    assert "pdf_extractor" in (rec.provider or "")
    assert rec.evidence_id in new_ids


def test_task13_targeted_retrieval_skips_full_text_and_failed_urls(tmp_path):
    """FIXED URLs are skipped and FAILED records are not retried via upgrade routing."""
    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_web_tool = MagicMock(spec=WebParserTool)
    mock_web_tool.name = "Webpage Text Parser"

    store = EvidenceStore.create(output_dir=tmp_path)
    fixed_url = "https://docs.example/guide"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(fixed_url, "Full guide text.")))

    failed_url = "https://unavailable.example/dev"
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.failure(failed_url, "ConnectionError", provider="web_parser")
        )
    )

    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)
    web_adapter = WebParserAdapter(store=store, tool=mock_web_tool)

    new_ids = execute_targeted_retrieval(
        queries=[],
        store=store,
        extractor_tools=[pdf_adapter, web_adapter],
        upgrade_urls=[fixed_url, failed_url, fixed_url],
    )

    mock_pdf_tool._run.assert_not_called()
    mock_web_tool._run.assert_not_called()
    assert new_ids == []
    assert store.get_by_url(fixed_url).retrieval_status == RetrievalStatus.FULL_TEXT
    assert store.get_by_url(failed_url).retrieval_status == RetrievalStatus.FAILED


def test_task13_targeted_retrieval_failed_extractor_preserves_snippet(tmp_path):
    """When every extractor fails, the snippet record stays intact and no upgrade is fabricated."""
    url = "https://arxiv.org/abs/2404.03715"
    store = _make_upgrade_store(tmp_path, url, "FlashAttention-2 provides faster attention kernels.")
    prior = store.get_by_url(url)

    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_pdf_tool._run.side_effect = ConnectionError("repository unavailable")
    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)

    new_ids = execute_targeted_retrieval(
        queries=[],
        store=store,
        extractor_tools=[pdf_adapter],
        upgrade_urls=[url],
    )

    assert new_ids == []
    assert len(store) == 1
    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY
    assert rec.evidence_id == prior.evidence_id
    assert rec.captured_text == prior.captured_text


def test_task13_targeted_retrieval_upgrade_urls_capped_at_twelve(tmp_path):
    """A deterministic maximum of 12 cited snippet URLs are routed per retrieval step."""
    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_pdf_tool._run.return_value = "Extracted body text for the paper."

    store = EvidenceStore.create(output_dir=tmp_path)
    urls = [f"https://papers.example/p{i}" for i in range(14)]
    for raw_url in urls:
        store.add(
            EvidenceRecord.from_tool_result(
                ToolFetchResult.success(raw_url, f"Snippet for {raw_url}.", status=RetrievalStatus.SNIPPET_ONLY)
            )
        )

    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)

    upgrade_urls = list(reversed(urls)) + urls[:2]
    execute_targeted_retrieval(
        queries=[],
        store=store,
        extractor_tools=[pdf_adapter],
        upgrade_urls=upgrade_urls,
    )

    assert mock_pdf_tool._run.call_count == 12
    called_urls = [call.args[0] for call in mock_pdf_tool._run.call_args_list]
    assert called_urls == list(reversed(urls))[:12]
    for remaining in urls[:2]:
        assert store.get_by_url(remaining).retrieval_status == RetrievalStatus.SNIPPET_ONLY


def test_task13_feedback_loop_upgrades_citations_of_unverified_claims(tmp_path):
    """Full loop: a cited snippet-only source is upgraded and its claim re-verified as supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2404.03715"
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                url,
                "FlashAttention-2 is a MRI-optimized attention kernel for GPUs.",
                status=RetrievalStatus.SNIPPET_ONLY,
                title="FlashAttention-2",
            )
        )
    )
    prior = store.get_by_url(url)

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention-2 sustains 73% peak throughput on A100 GPUs [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    mock_pdf_tool = MagicMock(spec=PDFExtractorTool)
    mock_pdf_tool.name = "PDF Text Extractor"
    mock_pdf_tool._run.return_value = (
        "FlashAttention-2 is a memory-optimized attention kernel for GPUs. "
        "Measured results sustain 73% peak throughput on A100."
    )
    pdf_adapter = PDFExtractorAdapter(store=store, tool=mock_pdf_tool)

    def mock_judge(claim_text, passage_text, source_url):
        if "73% peak" in passage_text:
            return ClaimVerdict.SUPPORTED, "Passage directly states the measured throughput."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Snippet does not quantify throughput."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic="transformer attention mechanisms",
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        extractor_tools=[pdf_adapter],
    )

    assert loop_log.total_rounds_executed >= 1
    upgraded_iteration = next(
        (it for it in loop_log.iterations[1:] if it.new_evidence_ids), None
    )
    assert upgraded_iteration is not None
    assert prior.evidence_id in upgraded_iteration.new_evidence_ids

    rec = store.get_by_url(url)
    assert rec is not None
    assert rec.retrieval_status == RetrievalStatus.FULL_TEXT
    assert rec.evidence_id == prior.evidence_id
    assert rec.source_url == url
    assert "73% peak throughput on A100" in rec.captured_text

    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0
    assert loop_log.terminated_reason == "all_claims_supported"


# ===========================================================================
# Task 14 — Safe Feedback-Loop Claim Matching
# ===========================================================================

from research_crew.feedback_loop import (
    ClaimResolutionAudit,
    _match_recheck_record,
)


def _rec(claim_id, claim_text, verdict=ClaimVerdict.INSUFFICIENT_EVIDENCE):
    return ClaimVerificationRecord(
        claim_id=claim_id,
        claim_text=claim_text,
        source_url=None,
        passage_id=None,
        passage_text=None,
        verdict=verdict,
        rationale="test record",
        action_taken=ClaimAction.LABELED_UNVERIFIED.value,
    )


def test_task14_match_recheck_reworded_unique_fuzzy(tmp_path):
    old = "FlashAttention-2 provides measured speedup gains for long attention workloads on GPUs"
    reworded = "FlashAttention-2 delivers measured speedup improvements for lengthy attention workloads on GPUs"
    record = _rec("claim_001", reworded)

    matched, reason = _match_recheck_record(old, [record])
    assert matched is record
    assert reason == ""


def test_task14_match_recheck_exact_beats_higher_fuzzy_and_reordering(tmp_path):
    old = "Sparse attention provides efficient memory use for long sequences"
    decoy = _rec("claim_001", "Sparse attention delivers efficient memory usage for extended sequences")
    exact = _rec("claim_002", old)

    matched, reason = _match_recheck_record(old, [decoy, exact])
    assert matched is exact
    assert reason == ""

    # Identical claims reordered across rounds still match by text, not position
    swapped, reason2 = _match_recheck_record(old, [exact, decoy])
    assert swapped is exact
    assert reason2 == ""


def test_task14_match_recheck_ambiguous_near_duplicates(tmp_path):
    old = "GGUF quantization levels Q4KM and Q5KM support diverse hardware profiles under tight budgets"
    cand_a = _rec("claim_001", "GGUF quantization levels Q4KM and Q5KM support various hardware profiles under tight budgets")
    cand_b = _rec("claim_002", "GGUF quantization levels Q4KM and Q5KM enable diverse hardware profiles under tight budgets")

    matched, reason = _match_recheck_record(old, [cand_a, cand_b])
    assert matched is None
    assert "ambiguous" in reason
    assert "similar re-extracted claim" in reason


def test_task14_match_recheck_removed_claim(tmp_path):
    old = "Native fp models deliver concise YAML tool-call streaming in every open source release"
    unrelated = _rec("claim_001", "PostgreSQL row-level security policies are evaluated per command")

    matched, reason = _match_recheck_record(old, [unrelated])
    assert matched is None
    assert "no matching re-extracted claim" in reason


def test_task14_match_recheck_split_claim_refused(tmp_path):
    old = "GGUF quantization levels Q4KM and Q5KM allow deployment across diverse hardware profiles and sustain stable throughput"
    frag1 = _rec("claim_001", "GGUF quantization levels Q4KM and Q5KM allow deployment across diverse hardware profiles")
    frag2 = _rec("claim_002", "GGUF quantization levels Q4KM and Q5KM sustain stable throughput")

    matched, reason = _match_recheck_record(old, [frag1, frag2])
    assert matched is None
    assert "ambiguous" in reason


def test_task14_match_recheck_duplicate_target_collision(tmp_path):
    shared = _rec("claim_001", "Sparse attention delivers efficient memory usage for extended sequences")
    strong_old = "Sparse attention provides efficient memory use for long sequences on GPUs"
    overlapping_old = "Sparse attention offers efficient memory use for long sequences"

    first, reason1 = _match_recheck_record(strong_old, [shared])
    assert first is shared

    second, reason2 = _match_recheck_record(overlapping_old, [shared], claimed_claim_ids={first.claim_id})
    assert second is None
    assert "ambiguous" in reason2


def test_task14_feedback_loop_reworded_claim_resolves(tmp_path):
    """Full loop: a reworded re-extraction of an unverified claim is still recognized as the same claim's recheck."""
    store = EvidenceStore.create(output_dir=tmp_path)
    snippet_url = "https://arxiv.example/abs/2404.03715"
    full_url = "https://arxiv.example/abs/2404.03716"
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                snippet_url,
                "FlashAttention-2 is an optimized attention kernel implementation.",
                status=RetrievalStatus.SNIPPET_ONLY,
            )
        )
    )

    full_record = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            full_url,
            "Abstract.\n\nResults:\nFlashAttention-2 provides measured speedup gains for long attention workloads on GPUs.",
        )
    )

    initial_report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention-2 provides measured speedup gains for long attention workloads on GPUs [1].\n\n"
        "## Sources\n\n"
        f"[1] {snippet_url}\n"
    )

    def rewording_synthesizer(draft_report, unverified_claims, new_evidence_records, topic):
        assert "FlashAttention-2" in draft_report
        return (
            "# Core Foundations & Mechanism\n\n"
            "FlashAttention-2 delivers measured speedup improvements for lengthy attention workloads on GPUs [2].\n\n"
            "## Sources\n\n"
            f"[1] {snippet_url}\n"
            f"[2] {full_url}\n"
        )

    def mock_judge(claim_text, passage_text, source_url):
        if "speedup gains for long attention workloads" in passage_text:
            return ClaimVerdict.SUPPORTED, "Passage states the measured speedup directly."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Snippet lacks the measured speedup."

    final_report, summary, loop_log = execute_research_feedback_loop(
        initial_report=initial_report,
        store=store,
        topic="transformer attention mechanisms",
        max_rounds=2,
        action_policy="label",
        verifier_fn=mock_judge,
        research_fn=lambda queries: [full_record],
        synthesizer_fn=rewording_synthesizer,
    )

    assert loop_log.terminated_reason == "all_claims_supported"
    resolutions = loop_log.iterations[1].targeted_claim_resolutions
    assert len(resolutions) == 1
    resolution = resolutions[0]
    assert "recheck reason" not in resolution["notes"]
    assert resolution["verdict_after_recheck"] == ClaimVerdict.SUPPORTED.value
    new_claim_id = loop_log.iterations[1].records[0]["claim_id"]
    assert resolution["recheck_claim_id"] == new_claim_id
    assert summary.supported_claims_count == 1


def test_task14_resolution_audit_serialization_backward_compatible():
    """ClaimResolutionAudit JSON keeps the historical key set and adds nullable recheck_claim_id."""
    audit = ClaimResolutionAudit(
        claim_id="claim_001",
        claim_text="A claim",
        gained_relevant_evidence=True,
        evidence_id="evid",
        source_url="https://example.com",
        verdict_after_recheck="supported",
        notes="ok",
        recheck_claim_id="claim_002",
    )
    dumped = audit.to_dict()
    assert set(dumped.keys()) == {
        "claim_id",
        "claim_text",
        "gained_relevant_evidence",
        "evidence_id",
        "source_url",
        "verdict_after_recheck",
        "notes",
        "recheck_claim_id",
    }
    assert dumped["recheck_claim_id"] == "claim_002"
    assert dumped["claim_id"] == "claim_001"

    unmatched = ClaimResolutionAudit(claim_id="claim_003", claim_text="B", gained_relevant_evidence=False)
    assert unmatched.to_dict()["recheck_claim_id"] is None
    assert unmatched.to_dict()["verdict_after_recheck"] is None

    historical = json.loads(
        (Path("evidence_runs") / "feedback_loop_de3572bb68b442fd92e4ca8086cbea18.json").read_text()
    )
    old_shape_keys = set(historical["iterations"][0]["targeted_claim_resolutions"][0].keys()) if historical["iterations"][0].get("targeted_claim_resolutions") else set(dumped.keys()) - {"recheck_claim_id"}
    assert old_shape_keys == set(dumped.keys()) - {"recheck_claim_id"}

