"""
test_claim_verification.py
--------------------------
Offline unit tests for claim-level source verification:
- Supported claims against retrieved passage
- Contradicted claims against retrieved passage
- Insufficient evidence claims
- Missing citation marker or unrecorded source URL
- Irrelevant passage in an existing evidence record
- Never retaining a claim merely because citation URL exists in EvidenceStore
- Removal vs labeling action policies
- Structured verification record format and JSON persistence
- Mesh API LLM response parsing and graceful fallback
- Preservation of numeric and comparative-claim audit integration
"""

import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import (
    ReportAuditSummary,
    audit_report_citations,
    extract_report_source_mapping,
)
from research_crew.evidence.verifier import (
    AtomicClaim,
    ClaimAction,
    ClaimVerdict,
    ClaimVerificationRecord,
    ClaimVerificationSummary,
    ClaimVerifier,
    _acronym_matches_text,
    _check_entities_present,
    _clean_claim_statement,
    _decompose_claim_clauses,
    _decompose_parenthetical_propositions,
    _resolve_citations,
    _split_sentence_clauses,
    apply_verification_to_report,
    extract_atomic_claims,
    find_passage_for_url_and_query,
    find_supporting_passage,
    repair_json_escapes,
    save_verification_records,
    verify_report_claims,
)
from research_crew.main import _build_report_metadata
from research_crew.tools.tool_result import ToolFetchResult


def test_extract_atomic_claims_with_citations():
    """Verify that report prose is split into atomic claims linked to citations."""
    report = (
        "# Core Foundations & Mechanism\n\n"
        "Attention mechanisms calculate dynamic representations across token sequences [1]. "
        "Multi-head attention projects queries, keys, and values into multiple subspaces [1].\n\n"
        "## Key Findings\n\n"
        "FlashAttention reduces memory overhead via kernel tiling [2].\n\n"
        "## Sources\n\n"
        "[1] https://arxiv.org/abs/1706.03762 - Attention Is All You Need\n"
        "[2] https://arxiv.org/abs/2205.14135 - FlashAttention\n"
    )

    claims = extract_atomic_claims(report)
    assert len(claims) == 3

    assert "Attention mechanisms calculate" in claims[0].claim_text
    assert claims[0].source_url == "https://arxiv.org/abs/1706.03762"
    assert claims[0].citation_marker == "[1]"
    assert claims[0].section == "Core Foundations & Mechanism"

    assert "FlashAttention reduces" in claims[2].claim_text
    assert claims[2].source_url == "https://arxiv.org/abs/2205.14135"


def test_claim_verification_supported(tmp_path):
    """Claim substantiated by captured passage is classified as supported and retained."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2205.14135"
    captured = (
        "FlashAttention is an IO-aware exact attention algorithm. "
        "FlashAttention uses tiling to prevent reading and writing intermediate matrices "
        "to high-bandwidth GPU memory, drastically reducing memory overhead."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="FlashAttention")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention uses tiling to reduce memory overhead during attention computation [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        assert "tiling" in passage_text
        return ClaimVerdict.SUPPORTED, "Passage directly confirms tiling reduces memory overhead."

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.supported_claims_count == 1
    assert summary.contradicted_claims_count == 0
    assert summary.insufficient_evidence_claims_count == 0
    assert summary.records[0].verdict == ClaimVerdict.SUPPORTED
    assert summary.records[0].action_taken == ClaimAction.RETAINED.value
    assert summary.records[0].passage_text is not None
    assert summary.records[0].passage_id is not None
    # Report should retain the sentence without unverified badge
    assert "⚠️ Unverified" not in updated_report
    assert "FlashAttention uses tiling" in updated_report


def test_claim_verification_contradicted(tmp_path):
    """Claim refuted by captured passage is classified as contradicted and labeled."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = (
        "Standard multi-head self-attention has quadratic O(N^2) computational and memory complexity "
        "with respect to sequence length N, making long sequences expensive."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani 2017")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "Standard self-attention has linear O(N) memory complexity with respect to sequence length [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.CONTRADICTED, "Passage states complexity is quadratic O(N^2), contradicting the claimed linear O(N)."

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.supported_claims_count == 0
    assert summary.contradicted_claims_count == 1
    assert summary.records[0].verdict == ClaimVerdict.CONTRADICTED
    assert summary.records[0].action_taken == ClaimAction.LABELED_CONTRADICTED.value
    assert "⚠️ Contradicted by cited source" in updated_report


def test_claim_verification_insufficient_evidence(tmp_path):
    """Claim where source text does not substantiate assertion is classified as insufficient_evidence."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = (
        "The Transformer is the first transduction model relying entirely on self-attention "
        "to compute representations of its input and output without using sequence-aligned RNNs."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani 2017")))

    report = (
        "# Key Findings\n\n"
        "The Transformer was trained across 50,000 custom ASICs for six months continuously [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Passage describes model architecture but contains no data on training hardware or duration."

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.insufficient_evidence_claims_count == 1
    assert summary.records[0].verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert summary.records[0].action_taken == ClaimAction.LABELED_UNVERIFIED.value
    assert "⚠️ Unverified claim: Passage describes model architecture" in updated_report


def test_claim_verification_missing_citation(tmp_path):
    """Claim with an unrecorded citation or no citation marker triggers insufficient_evidence."""
    store = EvidenceStore.create(output_dir=tmp_path)
    report = (
        "# Core Foundations & Mechanism\n\n"
        "Speculative decoding accelerates inference without degrading sample quality [1].\n\n"
        "## Sources\n\n"
        "[1] https://arxiv.org/abs/2302.01318\n"
    )
    # Note: store is empty; URL is unrecorded

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.insufficient_evidence_claims_count == 1
    rec = summary.records[0]
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.passage_id is None
    assert rec.passage_text is None
    assert "was not captured in the run's EvidenceStore" in rec.rationale
    assert "⚠️ Unverified claim" in updated_report


def test_claim_verification_irrelevant_passage(tmp_path):
    """Source URL is in store, but document content is completely unrelated to the claim."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://recipes.org/sourdough-bread"
    captured = (
        "To make sourdough bread, mix flour, water, salt, and active sourdough starter. "
        "Allow dough to ferment at room temperature for 4 to 8 hours before baking at 450F."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Sourdough Guide")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "Self-attention calculates dot-product query-key similarity matrices in O(N^2) time [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.insufficient_evidence_claims_count == 1
    rec = summary.records[0]
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.passage_text is None
    assert "contains no relevant passage supporting the claim" in rec.rationale
    assert "⚠️ Unverified claim" in updated_report


def test_claim_not_retained_merely_because_citation_url_exists(tmp_path):
    """Never retain a claim merely because its citation URL exists in the EvidenceStore."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = "Standard transformer architecture uses 6 encoder layers and 6 decoder layers."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani")))

    # Citation audit alone would match this URL!
    report = (
        "# Key Findings\n\n"
        "The architecture contains 100 encoder layers and 200 decoder layers [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.CONTRADICTED, "Source document specifies 6 encoder layers, not 100."

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    # Citation audit matches the URL:
    citation_audit = audit_report_citations(report, store)
    assert citation_audit.matched_sources_count == 1
    assert citation_audit.unmatched_sources_count == 0

    # BUT claim-level verification refuses to retain the claim as verified:
    assert summary.records[0].verdict == ClaimVerdict.CONTRADICTED
    assert summary.supported_claims_count == 0
    assert summary.records[0].action_taken != ClaimAction.RETAINED.value
    assert "⚠️ Contradicted by cited source" in updated_report


def test_claim_verification_action_policy_remove(tmp_path):
    """When action_policy='remove', unsupported claims are cleanly excised from the report."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = (
        "Attention mechanisms compute weighted sums of value vectors. "
        "Medical studies confirm transformers do not cure human diseases."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "Attention mechanisms compute weighted sums of value vectors [1]. "
        "Transformers cure all human diseases instantaneously [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        if "weighted sums" in claim_text:
            return ClaimVerdict.SUPPORTED, "Passage confirms weighted sums."
        return ClaimVerdict.CONTRADICTED, "Passage has no relation to medical claims."

    updated_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="remove",
    )

    assert summary.supported_claims_count == 1
    assert summary.contradicted_claims_count == 1
    assert summary.records[1].action_taken == ClaimAction.REMOVED.value
    # The valid claim is retained:
    assert "Attention mechanisms compute weighted sums" in updated_report
    # The unsupported claim is removed from the text:
    assert "cure all human diseases" not in updated_report


def test_claim_verification_record_structure_and_persistence(tmp_path):
    """Verify structured verification record format, all required keys, and JSON file persistence."""
    rec = ClaimVerificationRecord(
        claim_id="claim_001",
        claim_text="FlashAttention uses tiling to reduce memory overhead.",
        source_url="https://arxiv.org/abs/2205.14135",
        passage_id="pass_12345678",
        passage_text="FlashAttention uses tiling to prevent reading and writing intermediate matrices.",
        verdict=ClaimVerdict.SUPPORTED,
        rationale="Tiling mechanism confirmed in source passage.",
        action_taken=ClaimAction.RETAINED.value,
        retrieval_status="full_text",
    )

    d = rec.to_dict()
    assert d["claim_id"] == "claim_001"
    assert d["claim_text"] == "FlashAttention uses tiling to reduce memory overhead."
    assert d["source_url"] == "https://arxiv.org/abs/2205.14135"
    assert d["passage_id"] == "pass_12345678"
    assert d["passage_text"] == "FlashAttention uses tiling to prevent reading and writing intermediate matrices."
    assert d["verdict"] == "supported"
    assert d["rationale"] == "Tiling mechanism confirmed in source passage."
    assert d["action_taken"] == "retained"
    assert d["retrieval_status"] == "full_text"

    summary = ClaimVerificationSummary(
        total_claims=1,
        supported_claims_count=1,
        contradicted_claims_count=0,
        insufficient_evidence_claims_count=0,
        records=[rec],
        warnings=[],
    )

    out_file = tmp_path / "claim_verifications_test_run.json"
    save_verification_records(out_file, summary)

    assert out_file.exists()
    loaded = json.loads(out_file.read_text(encoding="utf-8"))
    assert loaded["total_claims"] == 1
    assert loaded["records"][0]["claim_id"] == "claim_001"
    assert loaded["records"][0]["verdict"] == "supported"


def test_claim_verifier_mesh_llm_json_parsing():
    """Verify ClaimVerifier._verify_via_mesh_llm parses JSON correctly without live API calls."""
    mock_llm = MagicMock()

    # Case 1: Supported JSON response
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Directly supported by paragraph 3."}'
    verifier = ClaimVerifier(llm=mock_llm)
    v, r = verifier._verify_via_mesh_llm("test claim", "supporting passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r == "Directly supported by paragraph 3."

    # Case 2: Contradicted JSON response embedded in Markdown block
    mock_llm.call.return_value = '```json\n{"verdict": "contradicted", "rationale": "Passage states opposite."}\n```'
    v, r = verifier._verify_via_mesh_llm("test claim", "supporting passage text", "https://example.com")
    assert v == ClaimVerdict.CONTRADICTED
    assert r == "Passage states opposite."

    # Case 3: Insufficient evidence response
    mock_llm.call.return_value = '{"verdict": "insufficient_evidence", "rationale": "No relevant statistics mentioned."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "supporting passage text", "https://example.com")
    assert v == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert r == "No relevant statistics mentioned."

    # Case 4: Malformed response / exception fallback
    mock_llm.call.side_effect = RuntimeError("Transient network error")
    v, r = verifier._verify_via_mesh_llm("test claim", "supporting passage text", "https://example.com")
    assert v == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "Automated LLM verification" in r


def test_preserves_numeric_citation_and_comparative_audits(tmp_path):
    """Verify that existing citation, numeric, and comparative audits remain fully functional alongside claim verification."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2205.14135"
    captured = "FlashAttention yields a 3x speedup on GPT-2 compared to standard attention baselines."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="FlashAttention")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "FlashAttention improves exact attention computation efficiency [1].\n\n"
        "## Benchmarks & Metrics\n\n"
        "| Metric | Value | Baseline | Source |\n"
        "|---|---|---|---|\n"
        f"| Training Speedup | 3x | Standard attention | {url} |\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Passage confirms efficiency improvement."

    verified_report, claim_summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    audit = audit_report_citations(verified_report, store, claim_verification_summary=claim_summary)

    # Citation provenance audit intact:
    assert audit.total_citations_found == 1
    assert audit.matched_sources_count == 1
    assert audit.has_foundations_section is True

    # Numeric metrics audit intact:
    assert audit.metrics_summary is not None
    assert audit.metrics_summary.total_metrics_found == 1
    assert audit.metrics_summary.traceable_metrics_count == 1
    assert audit.metrics_summary.specified_baseline_count == 1

    # Claim verification linked:
    assert audit.claim_verification_summary is not None
    assert audit.claim_verification_summary.supported_claims_count == 1

    # Report footer includes claim verification statistics:
    footer = _build_report_metadata(audit, store, elapsed=12.0, claim_summary=claim_summary)
    assert "*Claim verification: 1/1 factual claims supported" in footer


def test_headings_and_title_fragments_excluded_from_claims_extraction_without_removal(tmp_path):
    """Ensure Markdown headings, bold title fragments, and attribution lines are not treated as factual claims.

    Guarantees:
    1. Headings like '## Core Foundations & Mechanism', '1. **Exact Hardware Optimization**',
       'Pipeline Overview:', and '*Source: [2]*' are not extracted as atomic claims.
    2. Headings remain in the report text without modification or unverified badges.
    3. ClaimVerificationSummary.total_claims counts strictly factual statements, not headings.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2205.14135"
    captured = "FlashAttention speeds up attention via IO-aware tiling and reduces memory footprint."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="FlashAttention")))

    report = (
        "# Research Summary: Transformer Attention\n\n"
        "## Core Foundations & Mechanism\n"
        "Pipeline Overview:\n"
        "1. **Exact Hardware Optimization**\n"
        "   FlashAttention speeds up attention via IO-aware tiling [1].\n"
        "   *Source: [1]*\n\n"
        "2. **Alternative State Space Hybrids**:\n"
        "   Architecture Overview\n\n"
        "## Sources\n"
        f"[1] FlashAttention paper\n"
        f"    {url}\n"
    )

    claims = extract_atomic_claims(report)
    # Exactly one factual claim: "FlashAttention speeds up attention via IO-aware tiling"
    assert len(claims) == 1
    assert "FlashAttention speeds up attention via IO-aware tiling" in claims[0].claim_text
    assert claims[0].source_url == url

    # None of the headings or title-only fragments were extracted
    claim_texts = [c.claim_text for c in claims]
    assert not any("Core Foundations" in t for t in claim_texts)
    assert not any("Pipeline Overview" in t for t in claim_texts)
    assert not any("Exact Hardware Optimization" in t for t in claim_texts)
    assert not any("Alternative State Space Hybrids" in t for t in claim_texts)
    assert not any("Architecture Overview" in t for t in claim_texts)
    assert not any("Source:" in t for t in claim_texts)

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Confirmed by passage."

    verified_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    # Summary counts only the 1 factual claim
    assert summary.total_claims == 1
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0

    # Headings and structure remain completely preserved in the report
    assert "## Core Foundations & Mechanism" in verified_report
    assert "1. **Exact Hardware Optimization**" in verified_report
    assert "*Source: [1]*" in verified_report
    assert "2. **Alternative State Space Hybrids**" in verified_report
    assert "⚠️ Unverified" not in verified_report


def test_numbered_bibliography_and_inline_citation_resolution(tmp_path):
    """Ensure claims with [1], [2], etc. correctly resolve to URLs across multi-line Sources entries."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, "Self-attention computes dynamic representations.")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url2, "FlashAttention achieves speedup via tiling.")))

    report = (
        "# Research Summary\n\n"
        "## Core Foundations & Mechanism\n"
        "Self-attention computes dynamic representations [1]. "
        "FlashAttention achieves speedup via tiling [2].\n\n"
        "## Sources\n"
        "[1] Attention Is All You Need\n"
        f"    {url1}\n\n"
        "[2] FlashAttention: Fast and Memory-Efficient Exact Attention\n"
        f"    {url2}\n"
    )

    claims = extract_atomic_claims(report)
    assert len(claims) == 2
    assert claims[0].source_url == url1
    assert claims[0].citation_marker == "[1]"
    assert claims[1].source_url == url2
    assert claims[1].citation_marker == "[2]"

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Direct match."

    verified_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    assert summary.total_claims == 2
    assert summary.supported_claims_count == 2
    assert summary.insufficient_evidence_claims_count == 0


def test_multiple_citations_in_single_sentence(tmp_path):
    """Ensure sentences citing multiple sources like [1, 2] or [1][2] associate with all cited sources."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, "Transformers enable parallel sequence computation.")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url2, "IO-aware designs accelerate Transformer pipelines.")))

    report = (
        "# Research Summary\n\n"
        "## Core Foundations & Mechanism\n"
        "Modern neural architectures combine parallel representations with IO-aware attention optimizations [1, 2].\n\n"
        "## Sources\n"
        f"[1] {url1}\n"
        f"[2] {url2}\n"
    )

    claims = extract_atomic_claims(report)
    # Extracts 1 unified claim with both cited sources preserved without duplication
    assert len(claims) == 1
    assert claims[0].source_url == url1
    assert claims[0].cited_urls == [url1, url2]
    assert "[1]" in claims[0].citation_marker and "[2]" in claims[0].citation_marker

    # Citation markers [1, 2] are stripped from the clean claim text
    for c in claims:
        assert "[1, 2]" not in c.claim_text
        assert "combine parallel representations" in c.claim_text


def test_missing_bibliography_entry_marked_insufficient_and_labeled(tmp_path):
    """Ensure citation markers without a matching bibliography entry resolve to None and are labeled unverified."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, "Self-attention computes representations.")))

    report = (
        "# Research Summary\n\n"
        "## Core Foundations & Mechanism\n"
        "Self-attention computes representations [1]. "
        "Quantum neuromorphic circuits execute self-attention with zero energy [99].\n\n"
        "## Sources\n"
        f"[1] {url1}\n"
    )
    # Note: [99] has no entry in ## Sources

    claims = extract_atomic_claims(report)
    assert len(claims) == 2

    # Claim 1 resolves to url1
    assert claims[0].source_url == url1
    assert claims[0].citation_marker == "[1]"

    # Claim 2 has citation marker [99], but source_url is None
    assert claims[1].source_url is None
    assert claims[1].citation_marker == "[99]"

    def mock_judge(claim_text, passage_text, source_url):
        return ClaimVerdict.SUPPORTED, "Supported."

    verified_report, summary = verify_report_claims(
        report=report,
        store=store,
        verifier_fn=mock_judge,
        action_policy="label",
    )

    assert summary.total_claims == 2
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 1

    missing_entry_rec = [r for r in summary.records if r.source_url is None][0]
    assert missing_entry_rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert missing_entry_rec.action_taken == ClaimAction.LABELED_UNVERIFIED.value
    assert "lacks a valid, resolvable source URL citation" in missing_entry_rec.rationale

    # Report clearly labels the missing-entry claim under label policy
    assert "⚠️ Unverified claim: Claim lacks a valid, resolvable source URL citation" in verified_report


def test_extract_atomic_claims_leading_divider_and_frontmatter():
    """Verify that a leading divider and frontmatter do not terminate claim extraction."""
    report = (
        "---\n"
        "title: Example Research Summary\n"
        "date: 2026-10-15\n"
        "---\n"
        "# Research Summary: Example\n\n"
        "## Core Foundations & Mechanism\n"
        "The proposed method improves retrieval accuracy by 12% [1]. "
        "The method was evaluated on Dataset A [1].\n\n"
        "## Sources\n\n"
        "[1] https://example.com/dataset_a_paper\n"
    )
    claims = extract_atomic_claims(report)
    assert len(claims) >= 2
    claim_texts = [c.claim_text for c in claims]
    assert any("improves retrieval accuracy by 12%" in t for t in claim_texts)
    assert any("evaluated on Dataset A" in t for t in claim_texts)
    for c in claims:
        assert c.citation_marker == "[1]"
        assert c.source_url == "https://example.com/dataset_a_paper"
        assert "title:" not in c.claim_text
        assert "date:" not in c.claim_text
        assert "---" not in c.claim_text


def test_extract_atomic_claims_actual_report_regression():
    """Verify that actual report rag_vs_long_context.md with leading '---' extracts all claims."""
    fixture_path = Path(__file__).parent / "fixtures" / "rag_vs_long_context.md"
    root_path = Path("rag_vs_long_context.md")
    report_path = fixture_path if fixture_path.exists() else root_path
    assert report_path.exists(), f"rag_vs_long_context.md fixture should exist at {fixture_path} or {root_path}"
    raw_content = report_path.read_text(encoding="utf-8")
    assert raw_content.startswith("---"), "Test must verify unstripped leading divider"

    claims = extract_atomic_claims(raw_content)
    assert len(claims) == 14
    assert all(c.section in ("Core Foundations & Mechanism", "Key Insights", "Methodology Overview") for c in claims)
    assert all(c.source_url is not None and "arxiv.org" in c.source_url for c in claims)


# ---------------------------------------------------------------------------
# Phase 2H Regression Tests: Verification Accuracy and Passage Retrieval
# ---------------------------------------------------------------------------

def test_regression_passage_selection_after_header(tmp_path):
    """1. A relevant claim whose supporting passage occurs after the document header."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290v2"
    captured = (
        "Direct Preference Optimization: Your Language Model is Secretly a Reward Model\n"
        "Rafael Rafailov, Archit Sharma, Eric Mitchell, Stefano Ermon, Christopher D. Manning, Chelsea Finn\n"
        "Department of Computer Science, Stanford University\n"
        "{rafailov, architsh, eric.mitchell}@cs.stanford.edu\n\n"
        "Abstract\n"
        "In this paper we introduce a new parameterization of the reward model in RLHF that enables "
        "extraction of the corresponding optimal policy in closed form, allowing us to solve the "
        "standard RLHF problem with only a simple classification loss. The resulting algorithm, "
        "which we call Direct Preference Optimization (DPO), reparameterizes the reward function to "
        "extract the optimal policy in closed form, eliminating the reinforcement learning loop."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="DPO")))

    claim = AtomicClaim(
        claim_id="claim_001",
        claim_text="Direct Preference Optimization (DPO) reparameterizes the reward function to extract the optimal policy in closed form, eliminating the reinforcement learning loop.",
        source_url=url,
    )

    passage, pid, rec = find_supporting_passage(claim, store)
    assert passage is not None
    assert "Stanford University" not in passage
    assert "{rafailov" not in passage
    assert "reparameterizes the reward function to extract the optimal policy in closed form" in passage
    assert pid is not None
    assert rec.source_url == url


def test_regression_compound_claim_supported_by_different_sources(tmp_path):
    """2. A compound claim whose clauses are supported by different sources."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2305.18290"

    # Source 1 only discusses PPO / reward models
    text1 = "PPO optimizes the policy against an explicit learned reward model using actor-critic reinforcement learning."
    # Source 2 only discusses DPO eliminating reward models
    text2 = "Direct Preference Optimization directly optimizes preference pairs, eliminating the reward model entirely."

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, text1, title="Vaswani/PPO")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url2, text2, title="Rafailov DPO")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "PPO optimizes the policy against an explicit learned reward model [1], whereas DPO eliminates the reward model entirely [2].\n\n"
        "## Sources\n\n"
        f"[1] {url1}\n"
        f"[2] {url2}\n"
    )

    verified_report, summary = verify_report_claims(
        report=report,
        store=store,
        action_policy="label",
    )

    assert summary.total_claims == 2
    assert summary.supported_claims_count == 2
    assert summary.insufficient_evidence_claims_count == 0
    assert summary.contradicted_claims_count == 0
    assert "⚠️ Unverified" not in verified_report
    assert "⚠️ Contradicted" not in verified_report


def test_regression_genuine_contradiction(tmp_path):
    """3. A genuine contradiction where evidence directly conflicts with the claim."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/1706.03762"
    captured = (
        "Standard multi-head self-attention has quadratic O(N^2) computational complexity "
        "with respect to sequence length N, making long sequences computationally expensive."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Vaswani 2017")))

    report = (
        "# Core Foundations & Mechanism\n\n"
        "Standard multi-head self-attention exhibits linear O(N) computational complexity with sequence length [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    verified_report, summary = verify_report_claims(
        report=report,
        store=store,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.contradicted_claims_count == 1
    assert summary.supported_claims_count == 0
    assert summary.records[0].verdict == ClaimVerdict.CONTRADICTED
    assert "⚠️ Contradicted by cited source" in verified_report


def test_regression_semantic_mismatch_not_contradiction(tmp_path):
    """4. A semantic mismatch that is not a contradiction."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2406.07327v1"
    captured = (
        "We investigate the distribution of paired preference data and offer research "
        "directions to narrow the gap between reward-free and reward-based preference learning methods."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="Preference Learning Gap")))

    report = (
        "# Methodology Overview\n\n"
        "Identifying key challenges in reward-free versus reward-based preference learning is an essential research direction [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    verifier = ClaimVerifier(action_policy="label")
    claims = extract_atomic_claims(report)
    assert len(claims) == 1

    rec = verifier.verify_claim(claims[0], store)
    # A phrasing mismatch must NEVER produce a CONTRADICTED verdict
    assert rec.verdict != ClaimVerdict.CONTRADICTED
    assert rec.verdict in (ClaimVerdict.INSUFFICIENT_EVIDENCE, ClaimVerdict.SUPPORTED)


def test_regression_claim_genuinely_insufficient(tmp_path):
    """5. A claim for which evidence is genuinely insufficient."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290v2"
    captured = (
        "We train models using Direct Preference Optimization on 8 A100 GPUs for 10 hours. "
        "We evaluate on the TL;DR summarization benchmark and Anthropic Helpful and Harmless dataset."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="DPO Experiments")))

    report = (
        "# Methodology Overview\n\n"
        "DPO was trained continuously across 100,000 TPUv5 pods for twelve months uninterrupted [1].\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    verified_report, summary = verify_report_claims(
        report=report,
        store=store,
        action_policy="label",
    )

    assert summary.total_claims == 1
    assert summary.insufficient_evidence_claims_count == 1
    assert summary.supported_claims_count == 0
    assert summary.contradicted_claims_count == 0
    assert summary.records[0].verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "⚠️ Unverified claim" in verified_report


def test_regression_numerical_result_missing_baseline(tmp_path):
    """6. A numerical result with a missing or incompatible baseline."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290v2"
    captured = "DPO achieves 85.2% win rate on summarization evaluation benchmark."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured, title="DPO Evaluation")))

    report = (
        "# Empirical Findings\n\n"
        "## Benchmarks & Metrics\n\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|---|---|---|---|\n"
        f"| Win Rate on Summarization | 85.2% | | [1] |\n\n"
        "## Sources\n\n"
        f"[1] {url}\n"
    )

    from research_crew.evidence.validator import audit_report_metrics
    metrics_summary = audit_report_metrics(report, store)

    assert metrics_summary.total_metrics_found == 1
    assert metrics_summary.missing_baseline_count == 1
    assert metrics_summary.missing_performance_baseline_count == 1
    assert any("Comparison baseline is missing or empty" in w for w in metrics_summary.warnings)


def test_regression_source_attribution_and_retrieval_status_preservation(tmp_path):
    """7. Preservation of original source attribution and retrieval status."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2305.18290v2"
    url2 = "https://arxiv.org/abs/9999.99999v1"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, "Direct Preference Optimization reparameterizes reward models.", title="DPO")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.failure(url2, "404 Not Found")))

    claim1 = AtomicClaim(claim_id="c1", claim_text="Direct Preference Optimization reparameterizes reward models.", source_url=url1)
    claim2 = AtomicClaim(claim_id="c2", claim_text="Unobtainable paper claim.", source_url=url2)

    verifier = ClaimVerifier(action_policy="label")

    rec1 = verifier.verify_claim(claim1, store)
    assert rec1.source_url == url1
    assert rec1.retrieval_status == "full_text"
    assert rec1.passage_text is not None
    assert rec1.passage_id is not None
    assert rec1.supporting_urls == [url1]

    rec2 = verifier.verify_claim(claim2, store)
    assert rec2.source_url == url2
    assert rec2.retrieval_status == "failed"
    assert rec2.passage_text is None
    assert rec2.passage_id is None
    assert rec2.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "FAILED retrieval status" in rec2.rationale


# ===========================================================================
# Phase 2I Regression Tests: Clause-Ordering Invariance & Contradiction Logic
# ===========================================================================

def test_phase2i_claim_verifier_clause_ordering_invariance():
    """Task 4: Differences in rhetorical clause order must not trigger false contradiction verdicts."""
    mock_llm = MagicMock()
    # Simulates the Phase 2H LLM judge rationale which hallucinated a contradiction due to clause ordering
    mock_llm.call.return_value = json.dumps({
        "verdict": "contradicted",
        "rationale": "swapping the causal association of enhancing math versus limited reasoning",
    })

    verifier = ClaimVerifier(llm=mock_llm)

    claim = (
        "While alignment methods can achieve near-optimal performance when trained on smaller subsets "
        "of training data and enhance mathematical problem-solving, they offer limited improvements on complex reasoning tasks."
    )
    passage = (
        "We find that: (1) alignment methods often achieve near-optimal performance even with smaller subsets of training data; "
        "(2) although they offer limited improvements on complex reasoning tasks, they enhance mathematical problem-solving; "
        "and (3) using an instruction-tuned model improves truthfulness."
    )

    verdict, rationale = verifier._verify_via_mesh_llm(claim, passage, "https://arxiv.org/html/2404.14723v2")
    assert verdict == ClaimVerdict.SUPPORTED
    assert "clause order variation reconciled" in rationale


def test_phase2i_claim_verifier_prompt_includes_clause_ordering_guidelines():
    """Task 4: Claim verifier prompt explicitly instructs the judge on clause-ordering invariance."""
    mock_llm = MagicMock()
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Directly supported."}'

    verifier = ClaimVerifier(llm=mock_llm)
    verifier._verify_via_mesh_llm("test claim", "test passage", "https://example.com")

    call_args = mock_llm.call.call_args[0][0]
    assert "Differences in clause ordering" in call_args
    assert "Compare the underlying factual assertions and their relationships" in call_args
    assert "active vs. passive voice" in call_args


def test_phase2i_claim_verifier_preserves_genuine_contradiction():
    """Task 4: Genuine factual contradictions must continue to be classified as contradicted."""
    mock_llm = MagicMock()
    mock_llm.call.return_value = json.dumps({
        "verdict": "contradicted",
        "rationale": "Passage directly refutes linear complexity, stating memory complexity is quadratic.",
    })

    verifier = ClaimVerifier(llm=mock_llm)
    claim = "The algorithm operates with linear memory complexity."
    passage = "The architecture requires quadratic memory complexity in the sequence length."

    verdict, rationale = verifier._verify_via_mesh_llm(claim, passage, "https://example.com/complexity")
    assert verdict == ClaimVerdict.CONTRADICTED
    assert "directly refutes" in rationale or "quadratic" in rationale


def test_phase2i_claim_verifier_insufficient_evidence_distinct_from_contradiction():
    """Task 4: Missing or off-topic information remains insufficient_evidence and is not converted to contradicted."""
    mock_llm = MagicMock()
    mock_llm.call.return_value = json.dumps({
        "verdict": "insufficient_evidence",
        "rationale": "Passage discusses optimization principles but does not mention AlpacaEval win rates.",
    })

    verifier = ClaimVerifier(llm=mock_llm)
    claim = "DPO achieves a 95% win rate on AlpacaEval."
    passage = "Direct Preference Optimization simplifies RLHF by optimizing implicit rewards."

    verdict, rationale = verifier._verify_via_mesh_llm(claim, passage, "https://example.com/dpo")
    assert verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "AlpacaEval" in rationale


# ===========================================================================
# Phase 2M Regression Tests: Multi-Citation Attribution & Joint Verification
# ===========================================================================

def test_phase2m_one_source_supports_full_claim(tmp_path):
    """1. One source supports the full claim: a claim citing multiple sources where one entails the claim is supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2411.04991v2"
    url2 = "https://arxiv.org/abs/2406.11584v3"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Bradley-Terry reward models provide deep-network convergence guarantees despite sparse prompt-response comparisons.",
        title="Sun et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "Paired comparisons data often exhibit cyclicality and intransitivity in ranking systems.",
        title="Singh & Davidov"
    )))

    claim = AtomicClaim(
        claim_id="claim_001",
        claim_text="Bradley-Terry reward models provide deep-network convergence guarantees despite sparse prompt-response comparisons",
        cited_urls=[url1, url2],
        source_url=url1,
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url1]
    assert rec.source_url == url1
    assert "deep-network convergence guarantees" in rec.passage_text
    assert "fully supported by cited source" in rec.rationale


def test_phase2m_two_sources_jointly_support_two_clauses(tmp_path):
    """2. Two sources jointly support two clauses: compound claim supported only when combined passages entail all substantive clauses."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Transformers enable parallel sequence computation across attention heads.",
        title="Vaswani et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "FlashAttention reduces memory overhead via kernel tiling and IO-aware IO scheduling.",
        title="Dao et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_002",
        claim_text="Transformers enable parallel sequence computation, and FlashAttention reduces memory overhead via kernel tiling",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Transformers enable parallel sequence computation",
            "FlashAttention reduces memory overhead via kernel tiling"
        ]
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert set(rec.supporting_urls) == {url1, url2}
    assert url1 in rec.passage_text and url2 in rec.passage_text
    assert "jointly supported across cited sources" in rec.rationale


def test_phase2m_one_clause_missing_remains_insufficient(tmp_path):
    """3. One clause is missing: claim remains insufficient even when the other clause is supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Transformers enable parallel sequence computation across attention heads.",
        title="Vaswani et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "FlashAttention reduces memory overhead via IO-aware GPU kernel tiling.",
        title="Dao et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_003",
        claim_text="Transformers enable parallel sequence computation, whereas quantum neuromorphic circuits execute with zero energy",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Transformers enable parallel sequence computation",
            "whereas quantum neuromorphic circuits execute with zero energy"
        ]
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "partially unsupported" in rec.rationale
    assert "quantum neuromorphic" in rec.rationale


def test_phase2m_citation_multiplicity_does_not_duplicate_logical_claim(tmp_path):
    """4. Citation multiplicity does not duplicate the logical claim: multiple citations do not create duplicate claim records."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2305.18290v2"
    url2 = "https://arxiv.org/abs/2411.04991v2"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Direct Preference Optimization simplifies RLHF by optimizing implicit rewards.",
        title="Rafailov et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "Point-wise Bradley-Terry models possess theoretical convergence guarantees under deep neural representations.",
        title="Sun et al."
    )))

    report = (
        "# Research Report\n\n"
        "## Core Foundations & Mechanism\n"
        "Direct Preference Optimization simplifies RLHF by optimizing implicit rewards [1, 2].\n\n"
        "## Sources\n"
        f"[1] {url1}\n"
        f"[2] {url2}\n"
    )

    claims = extract_atomic_claims(report)
    assert len(claims) == 1, f"Expected 1 claim, got {len(claims)} (multiplicity duplicated claims)"
    assert claims[0].cited_urls == [url1, url2]

    _, summary = verify_report_claims(report=report, store=store, action_policy="label")
    assert summary.total_claims == 1
    assert len(summary.records) == 1
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0


def test_phase2m_wrong_source_attribution_cannot_be_credited(tmp_path):
    """5. Wrong source attribution: a source that does not support the assigned clause cannot be credited for it."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Transformers rely entirely on self-attention mechanisms without recurrence.",
        title="Vaswani et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_005",
        claim_text="Kernel tiling eliminates GPU memory access bottlenecks in attention layers",
        cited_urls=[url1],
        source_url=url1,
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.supporting_urls is None


def test_phase2m_genuine_contradiction_detected(tmp_path):
    """6. Genuine contradiction: a real conflict continues to be detected under existing criteria even when multiple citations are present."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2305.18290v2"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "The standard self-attention layer exhibits quadratic memory complexity in sequence length.",
        title="Rafailov et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "IO-aware attention improves memory management on GPU hardware.",
        title="Dao et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_006",
        claim_text="The algorithm maintains linear memory complexity across long sequences",
        cited_urls=[url1, url2],
        source_url=url1,
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.CONTRADICTED
    assert "contradicts the claim" in rec.rationale
    assert rec.supporting_urls is None


def test_phase2m_unsupported_is_not_contradicted(tmp_path):
    """7. Unsupported is not contradicted: lack of evidence does not trigger a contradiction verdict."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2305.18290v2"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Direct Preference Optimization derives an exact mapping between reward functions and policies.",
        title="Rafailov et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "FlashAttention exploits SRAM memory hierarchies to reduce HBM traffic.",
        title="Dao et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_007",
        claim_text="DPO scores 98.5% win rate on AlpacaEval 2.0 with GPT-4 Turbo evaluation",
        cited_urls=[url1, url2],
        source_url=url1,
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.verdict != ClaimVerdict.CONTRADICTED
    assert rec.supporting_urls is None


def test_phase2m_unresolvable_citation_preserved_as_unresolved(tmp_path):
    """8. Unresolvable citation: missing or invalid source mapping remains unresolved rather than being silently ignored."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Attention mechanisms compute weighted sequence representations.",
        title="Vaswani et al."
    )))

    report = (
        "# Research Report\n\n"
        "## Core Foundations & Mechanism\n"
        "Attention mechanisms compute weighted sequence representations [1], but neuromorphic circuits operate at zero resistance [99].\n\n"
        "## Sources\n"
        f"[1] {url1}\n"
    )

    claims = extract_atomic_claims(report)
    assert any("[99]" in c.citation_marker or "[99]" in c.unresolved_citations for c in claims)

    _, summary = verify_report_claims(report=report, store=store, action_policy="label")
    unresolved_recs = [r for r in summary.records if r.source_url is None or r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE]
    assert len(unresolved_recs) >= 1
    assert any(r.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE for r in unresolved_recs)


def test_phase2m_repeated_citation_deduplication():
    """9. Repeated citation: repeated references to the same source do not artificially multiply support or claims."""
    report = (
        "# Research Report\n\n"
        "## Core Foundations & Mechanism\n"
        "Direct Preference Optimization eliminates reinforcement learning loops [1, 1].\n\n"
        "## Sources\n"
        "[1] https://arxiv.org/abs/2305.18290v2\n"
    )

    claims = extract_atomic_claims(report)
    assert len(claims) == 1
    assert claims[0].cited_urls == ["https://arxiv.org/abs/2305.18290v2"]


def test_phase2m_single_citation_regression(tmp_path):
    """10. Single-citation regression: ordinary single-source verification remains completely unchanged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290v2"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url,
        "Direct Preference Optimization optimizes implicit reward representations.",
        title="DPO"
    )))

    claim = AtomicClaim(
        claim_id="claim_010",
        claim_text="Direct Preference Optimization optimizes implicit reward representations",
        source_url=url,
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.source_url == url
    assert rec.supporting_urls == [url]
    assert rec.passage_text is not None


def test_phase2m_historical_phase2l_cross_citation_pattern(tmp_path):
    """11. Historical examples: reproduce the Phase 2L cross-citation failure pattern with controlled synthetic passages."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url9 = "https://arxiv.org/html/2406.11584v3"
    url10 = "https://arxiv.org/html/2411.04991v2"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url9,
        "Paired comparisons data frequently display intransitive or cyclic preferences evaluated via inconsistency indices.",
        title="Singh & Davidov"
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url10,
        "Point-wise Bradley-Terry reward models enjoy theoretical convergence guarantees when implemented with deep neural networks using embeddings.",
        title="Sun et al."
    )))

    report = (
        "# Research Report\n\n"
        "## Key Insights\n"
        "Point-wise Bradley-Terry (BT) reward models enjoy theoretical convergence guarantees when implemented with deep neural networks using embeddings [9, 10].\n\n"
        "## Sources\n"
        f"[9] {url9}\n"
        f"[10] {url10}\n"
    )

    claims = extract_atomic_claims(report)
    assert len(claims) == 1, "Should not duplicate claim into claim_003 and claim_004"
    assert claims[0].cited_urls == [url9, url10]

    verified_report, summary = verify_report_claims(report=report, store=store, action_policy="label")
    assert summary.total_claims == 1
    assert summary.supported_claims_count == 1
    assert summary.insufficient_evidence_claims_count == 0
    assert len(summary.unverified_claims) == 0
    assert "⚠️ Unverified" not in verified_report
    assert summary.records[0].verdict == ClaimVerdict.SUPPORTED
    assert summary.records[0].supporting_urls == [url10]


def test_phase2m_source_provenance_retained(tmp_path):
    """12. Source provenance: final records retain enough source and passage information for an independent reviewer to trace verdict."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Transformers process input sequences concurrently using multi-head self-attention mechanisms.",
        title="Vaswani et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "FlashAttention achieves exact attention computation with sub-quadratic memory footprint via GPU tiling.",
        title="Dao et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_012",
        claim_text="Transformers process input sequences concurrently, while FlashAttention achieves exact attention with sub-quadratic memory",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Transformers process input sequences concurrently",
            "while FlashAttention achieves exact attention with sub-quadratic memory"
        ]
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls is not None
    assert len(rec.supporting_urls) == 2
    assert url1 in rec.supporting_urls and url2 in rec.supporting_urls
    assert f"[{url1}]" in rec.passage_text
    assert f"[{url2}]" in rec.passage_text
    rec_dict = rec.to_dict()
    assert "supporting_urls" in rec_dict
    assert rec_dict["supporting_urls"] == rec.supporting_urls


def test_repair_json_escapes_rfc8259_and_latex():
    """Verify repair_json_escapes preserves RFC 8259 valid escapes and fixes invalid LaTeX backslashes."""
    # 1. Valid RFC 8259 escapes are preserved unchanged
    valid_escapes = r'{"quote": "Says \"hello\"", "slash": "a\/b", "control": "\b\f\n\r\t", "backslash": "\\", "unicode": "\u00e9\u03c7"}'
    assert repair_json_escapes(valid_escapes) == valid_escapes
    parsed = json.loads(repair_json_escapes(valid_escapes))
    assert parsed["quote"] == 'Says "hello"'
    assert parsed["unicode"] == "éχ"

    # 2. LaTeX expressions with invalid JSON escape sequences are repaired
    latex_raw = '{"rationale": "Uses $\\chi$PO and $\\chi^2$ divergence with $\\alpha$ and $\\sigma$ bounds."}'
    repaired = repair_json_escapes(latex_raw)
    parsed_latex = json.loads(repaired)
    assert r"$\chi$PO" in parsed_latex["rationale"]
    assert r"$\chi^2$" in parsed_latex["rationale"]

    # 3. Invalid / non-hex unicode is safely escaped
    invalid_unicode = '{"path": "\\user\\docs and \\underline"}'
    repaired_u = repair_json_escapes(invalid_unicode)
    parsed_u = json.loads(repaired_u)
    assert parsed_u["path"] == r"\user\docs and \underline"

    # 4. Dangling backslash before space inside string
    repaired_d = repair_json_escapes('{"key": "test\\ "}')
    parsed_d = json.loads(repaired_d)
    assert parsed_d["key"] == "test\\ "

    # 5. Truncated malformed JSON with dangling backslash at end of input
    repaired_truncated = repair_json_escapes('{"key": "test\\')
    with pytest.raises(json.JSONDecodeError):
        json.loads(repaired_truncated)


def test_claim_verifier_mesh_llm_latex_resilience():
    """Verify ClaimVerifier._verify_via_mesh_llm handles LaTeX and JSON escapes across all 10 scenarios."""
    mock_llm = MagicMock()
    verifier = ClaimVerifier(llm=mock_llm)

    # 1. A valid JSON response
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Directly supported by section 2."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r == "Directly supported by section 2."

    # 2. $\chi$PO inside a JSON string
    mock_llm.call.return_value = '{\n  "verdict": "supported",\n  "rationale": "The passage confirms that $\\chi$PO modifies the link function in DPO."\n}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\chi$PO" in r

    # 3. $\chi^2$ inside a JSON string
    mock_llm.call.return_value = '{\n  "verdict": "supported",\n  "rationale": "Passage demonstrates implicit pessimism via $\\chi^2$-divergence regularization."\n}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\chi^2$" in r

    # 4. Valid escaped quotes
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "The paper notes \\"closed-form policy extraction\\" directly."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r == 'The paper notes "closed-form policy extraction" directly.'

    # 5. Valid newline and tab escapes
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Point 1 confirmed.\\n\\tPoint 2 confirmed."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r == "Point 1 confirmed.\n\tPoint 2 confirmed."

    # 6. Valid Unicode escapes
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Greek letter \\u03c7 matches notation and \\u00e9 matches French terms."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r == "Greek letter χ matches notation and é matches French terms."

    # 7. Multiple LaTeX expressions in one response
    mock_llm.call.return_value = (
        '{\n'
        '  "verdict": "supported",\n'
        '  "rationale": "Evaluates $\\chi$PO with $\\alpha$-regularization, $\\beta$ parameters, and $\\sigma$-divergence bounds."\n'
        '}'
    )
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\chi$PO" in r
    assert r"$\alpha$" in r

    # 8. Malformed JSON that cannot be safely repaired (e.g. unclosed JSON structure)
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Unclosed quote and missing closing brace'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "Automated LLM verification produced an unparseable response." in r

    # 9. Parsing failure must not produce a supported verdict (even if text says 'supported')
    mock_llm.call.return_value = 'I strongly believe this is supported: {"verdict": "supported", broken json'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert v != ClaimVerdict.SUPPORTED
    assert "Automated LLM verification produced an unparseable response." in r

    # 10. Existing supported, contradicted, and insufficient verdict behavior remains unchanged
    mock_llm.call.return_value = '{"verdict": "contradicted", "rationale": "Passage directly refutes the claim and states opposite complexity."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.CONTRADICTED
    assert "refutes the claim" in r

    mock_llm.call.return_value = '{"verdict": "insufficient_evidence", "rationale": "Passage does not discuss hardware measurements or compute costs."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "does not discuss" in r


def test_claim_verifier_end_to_end_production_path_with_latex(tmp_path):
    """Verify the production verify_claim path succeeds on LaTeX-containing claims and model outputs."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2407.13399"
    captured_text = (
        "Chi-squared Preference Optimization (\\chi PO) modifies Direct Preference Optimization "
        "by altering the logarithmic link function to enforce \\chi^2-divergence regularization."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="\\chi PO Paper")))

    mock_llm = MagicMock()
    mock_llm.call.return_value = (
        '{\n'
        '  "verdict": "supported",\n'
        '  "rationale": "Passage confirms $\\chi$PO modifies the link function in DPO to implement $\\chi^2$-divergence."\n'
        '}'
    )

    verifier = ClaimVerifier(llm=mock_llm, action_policy="label")
    claim = AtomicClaim(
        claim_id="claim_018",
        claim_text="Chi-squared Preference Optimization ($\\chi$PO) modifies DPO's logarithmic link function to implement $\\chi^2$-divergence regularization",
        cited_urls=[url],
        source_url=url,
    )

    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.action_taken == ClaimAction.RETAINED.value
    assert r"$\chi$PO" in rec.rationale
    assert r"$\chi^2$" in rec.rationale
    assert rec.source_url == url


def test_claim_verifier_prevents_silent_latex_corruption():
    """Verify production verifier prevents silent corruption of LaTeX commands (beta, theta, rho, frac)."""
    mock_llm = MagicMock()
    verifier = ClaimVerifier(llm=mock_llm)

    # 1. $\beta$ does not corrupt into backspace (\x08) + eta
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Uses regularization coefficient $\\beta=0.1$."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\beta=0.1$" in r
    assert "\x08" not in r

    # 2. $\theta$ does not corrupt into tab (\t) + heta
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Model parameter vector $\\theta$ is optimized directly."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\theta$" in r
    assert r"\theta" in r
    assert "\t" not in r

    # 3. $\rho$ does not corrupt into carriage return (\r) + ho
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Calculates Spearman rank correlation $\\rho=0.85$."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\rho=0.85$" in r
    assert "\r" not in r

    # 4. $\frac{1}{2}$ does not corrupt into formfeed (\x0c) + rac
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Scaled by factor $\\frac{1}{2}$ in the objective function."}'
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\frac{1}{2}$" in r
    assert "\x0c" not in r

    # 5. Mixture of ambiguous and unambiguous LaTeX commands
    mock_llm.call.return_value = (
        '{\n'
        '  "verdict": "supported",\n'
        '  "rationale": "Combines $\\chi$PO and $\\alpha$-regularization with $\\beta$ weighting, '
        '$\\theta$ updates, $\\rho$ correlation, and $\\frac{1}{2}\\sigma^2$ variance bounds."\n'
        '}'
    )
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert r"$\chi$PO" in r
    assert r"$\alpha$" in r
    assert r"$\beta$" in r
    assert r"$\theta$" in r
    assert r"$\rho$" in r
    assert r"$\frac{1}{2}\sigma^2$" in r

    # 6. Genuine JSON newline, tab, backspace, and form-feed escapes retain their JSON semantics
    mock_llm.call.return_value = (
        '{\n'
        '  "verdict": "supported",\n'
        '  "rationale": "Summary:\\n\\t- First point\\r\\n\\fPage break\\bbackspace."\n'
        '}'
    )
    v, r = verifier._verify_via_mesh_llm("test claim", "passage text", "https://example.com")
    assert v == ClaimVerdict.SUPPORTED
    assert "\n" in r
    assert "\t" in r
    assert "\r\n" in r
    assert "\x0c" in r  # form feed
    assert "\x08" in r  # backspace


def test_claim_verifier_end_to_end_latex_corruption_prevention(tmp_path):
    """Verify that verify_claim retains correct LaTeX without corruption in production ClaimVerificationRecord."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2402.10038"
    captured_text = (
        "RS-DPO aligns models under parameter \\beta with temperature \\theta and correlation \\rho."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="RS-DPO Paper")))

    mock_llm = MagicMock()
    mock_llm.call.return_value = (
        '{\n'
        '  "verdict": "supported",\n'
        '  "rationale": "Substantiated by passage: tunes $\\beta=0.1$ and $\\theta$ with factor $\\frac{1}{2}$."\n'
        '}'
    )

    verifier = ClaimVerifier(llm=mock_llm, action_policy="label")
    claim = AtomicClaim(
        claim_id="claim_025",
        claim_text="RS-DPO tunes $\\beta$ and $\\theta$ parameters with scaling $\\frac{1}{2}$",
        cited_urls=[url],
        source_url=url,
    )

    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.action_taken == ClaimAction.RETAINED.value
    assert r"$\beta=0.1$" in rec.rationale
    assert r"$\theta$" in rec.rationale
    assert r"$\frac{1}{2}$" in rec.rationale
    # Ensure backspace and formfeed control characters did not corrupt the mathematical commands
    assert "\x08" not in rec.rationale
    assert "\x0c" not in rec.rationale


# ---------------------------------------------------------------------------
# Phase 2P — Evidence Passage Selection and Claim Context Tests
# ---------------------------------------------------------------------------

def test_phase2p_acronym_defined_immediately_before_supporting_sentence(tmp_path):
    """1. Acronym defined immediately before the relevant supporting sentence is preserved in evidence passage."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2402.10038"
    captured_text = (
        "We evaluate Hyperparameter Optimization (HPO). The technique yields a 15% increase in throughput "
        "across all benchmark suites."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="HPO Evaluation")))

    claim = AtomicClaim(
        claim_id="claim_2p_01",
        claim_text="HPO yields a 15% increase in throughput across all benchmark suites.",
        cited_urls=[url],
        source_url=url,
    )
    passage, pid, rec = find_passage_for_url_and_query(url, claim.claim_text, store)
    assert passage is not None
    # Context definition of HPO is retained alongside the empirical sentence
    assert "Hyperparameter Optimization" in passage
    assert "15% increase in throughput" in passage

    mock_llm = MagicMock()
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Passage defines HPO and proves 15% throughput improvement."}'
    verifier = ClaimVerifier(llm=mock_llm)
    record = verifier.verify_claim(claim, store)
    assert record.verdict == ClaimVerdict.SUPPORTED
    assert record.supporting_urls == [url]


def test_phase2p_acronym_defined_in_different_section_of_same_source(tmp_path):
    """2. Acronym defined in a different section of the same source is enriched via context extraction."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2402.10039"
    captured_text = (
        "# 1. Introduction\n"
        "In this work, we introduce Rank-Stabilized Direct Preference Optimization (RS-DPO) to address "
        "training instability in direct alignment for large language models.\n\n"
        "# 4. Experimental Results\n"
        "Our proposed method reduces memory overhead during fine-tuning compared to standard DPO by 30%.\n"
        "Across various batch sizes, throughput is consistently improved."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="RS-DPO Paper")))

    claim = AtomicClaim(
        claim_id="claim_2p_02",
        claim_text="RS-DPO reduces memory overhead during fine-tuning compared to standard DPO.",
        cited_urls=[url],
        source_url=url,
    )
    passage, pid, rec = find_passage_for_url_and_query(url, claim.claim_text, store)
    assert passage is not None
    # Definition from section 1 is attached as [Context: ...]
    assert "[Context:" in passage
    assert "Rank-Stabilized Direct Preference Optimization (RS-DPO)" in passage
    assert "reduces memory overhead during fine-tuning" in passage

    mock_llm = MagicMock()
    mock_llm.call.return_value = '{"verdict": "supported", "rationale": "Context establishes RS-DPO and passage confirms 30% memory reduction."}'
    verifier = ClaimVerifier(llm=mock_llm)
    record = verifier.verify_claim(claim, store)
    assert record.verdict == ClaimVerdict.SUPPORTED
    assert record.supporting_urls == [url]


def test_phase2p_relevant_passage_with_insufficient_context_retains_unverified(tmp_path):
    """3. A relevant passage with insufficient context (e.g. undefined acronym or ambiguous referent) retains insufficient evidence."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2402.10040"
    # Source mentions an improvement by 'our method', but nowhere defines or mentions RS-DPO
    captured_text = (
        "# 4. Experimental Results\n"
        "Our proposed method reduces memory overhead during fine-tuning compared to standard DPO by 30%.\n"
        "Across various batch sizes, throughput is consistently improved."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="Unknown Method")))

    claim = AtomicClaim(
        claim_id="claim_2p_03",
        claim_text="RS-DPO reduces memory overhead during fine-tuning compared to standard DPO.",
        cited_urls=[url],
        source_url=url,
    )
    passage, pid, rec = find_passage_for_url_and_query(url, claim.claim_text, store)
    assert passage is not None
    # No RS-DPO definition exists in the document, so context cannot be prepended
    assert "RS-DPO" not in passage

    mock_llm = MagicMock()
    mock_llm.call.return_value = '{"verdict": "insufficient_evidence", "rationale": "Passage discusses a proposed method but does not identify or define RS-DPO."}'
    verifier = ClaimVerifier(llm=mock_llm)
    record = verifier.verify_claim(claim, store)
    assert record.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert record.supporting_urls is None
    assert "does not identify or define RS-DPO" in record.rationale


def test_phase2p_title_or_author_metadata_snippet_rejected_as_insufficient(tmp_path):
    """4. A title or author-metadata snippet does not substantiate technical claims and is classified as insufficient."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_snippet = "https://arxiv.org/abs/2310.12345"
    url_full = "https://arxiv.org/abs/2310.67890"

    # Case A: SNIPPET_ONLY containing only paper title and authors
    snippet_meta = "RS-DPO: A Novel Method for Preference Optimization. Alice Smith, Bob Jones, Carol White. MIT CSAIL, Stanford University. arxiv:2310.12345"
    store.add(EvidenceRecord.from_fetch(url=url_snippet, captured_text=snippet_meta, retrieval_status=RetrievalStatus.SNIPPET_ONLY))

    # Case B: FULL_TEXT containing only Title and Authors header lines
    full_meta = "Title: Hyperparameter Optimization for Large Language Models.\nAuthors: Jane Doe and John Smith.\nPublished in ICML 2024."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_full, full_meta, title="HPO")))

    claim1 = AtomicClaim(
        claim_id="claim_2p_04a",
        claim_text="RS-DPO stabilizes gradient dynamics in large language models",
        cited_urls=[url_snippet],
        source_url=url_snippet,
    )
    claim2 = AtomicClaim(
        claim_id="claim_2p_04b",
        claim_text="Hyperparameter Optimization improves convergence rate by 50%",
        cited_urls=[url_full],
        source_url=url_full,
    )

    verifier = ClaimVerifier(action_policy="label")

    # Both must be rejected as insufficient evidence without relying on LLM
    rec1 = verifier.verify_claim(claim1, store)
    assert rec1.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec1.supporting_urls is None

    rec2 = verifier.verify_claim(claim2, store)
    assert rec2.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec2.supporting_urls is None

    # Even if directly passed to _evaluate_passage, metadata is rejected
    v, rat = verifier._evaluate_passage(claim1.claim_text, snippet_meta, url_snippet)
    assert v == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "metadata" in rat.lower() or "title" in rat.lower()


def test_phase2p_compound_claim_requiring_two_passages_from_same_source(tmp_path):
    """5. A compound claim whose clauses are supported by two distinct passages from the same source is verified with provenance."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290"
    captured_text = (
        "# 2. Method\n"
        "DPO eliminates the need for training a separate reward model by directly optimizing the language model policy.\n\n"
        "# 5. Wall-Clock Efficiency\n"
        "DPO achieves a 2.5x speedup in wall-clock training time compared to standard PPO algorithms."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="DPO Paper")))

    claim = AtomicClaim(
        claim_id="claim_2p_05",
        claim_text="DPO eliminates the need for training a separate reward model, while achieving a 2.5x speedup in training time compared to PPO.",
        cited_urls=[url],
        source_url=url,
        constituent_clauses=[
            "DPO eliminates the need for training a separate reward model",
            "while achieving a 2.5x speedup in training time compared to PPO",
        ],
    )

    def mock_eval(clause, passage, src_url):
        has_rm = "eliminates the need" in clause
        has_speedup = "2.5x speedup" in clause
        if has_rm and has_speedup:
            if "eliminates the need" in passage and "2.5x speedup" in passage:
                return ClaimVerdict.SUPPORTED, "Passage confirms both clauses."
            return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Single passage does not support both clauses."
        if has_rm and "eliminates the need" in passage:
            return ClaimVerdict.SUPPORTED, "Passage confirms DPO removes separate reward model."
        if has_speedup and "2.5x speedup" in passage:
            return ClaimVerdict.SUPPORTED, "Passage confirms 2.5x wall-clock speedup."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Incomplete evidence."

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    record = verifier.verify_claim(claim, store)

    assert record.verdict == ClaimVerdict.SUPPORTED
    assert record.supporting_urls == [url]
    assert f"[{url}]" in record.passage_text
    assert "eliminates the need for training a separate reward model" in record.passage_text
    assert "2.5x speedup in wall-clock training time" in record.passage_text
    assert "multiple passages from https://arxiv.org/abs/2305.18290" in record.rationale


def test_phase2p_compound_claim_requiring_two_distinct_sources(tmp_path):
    """6. A compound claim supported across two distinct sources preserves attribution for all clauses."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2305.18290"
    url2 = "https://arxiv.org/abs/2402.10038"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "DPO eliminates the need for training a separate reward model.",
        title="DPO Paper"
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "RS-DPO stabilizes preference learning for long responses by scaling the regularization parameter.",
        title="RS-DPO Paper"
    )))

    claim = AtomicClaim(
        claim_id="claim_2p_06",
        claim_text="DPO eliminates the need for a separate reward model, whereas RS-DPO stabilizes preference learning for long responses.",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "DPO eliminates the need for a separate reward model",
            "whereas RS-DPO stabilizes preference learning for long responses",
        ],
    )

    def mock_eval(clause, passage, src_url):
        if "separate reward model" in clause and "separate reward model" in passage:
            return ClaimVerdict.SUPPORTED, "Source 1 entails reward model elimination."
        if "stabilizes preference learning" in clause and "stabilizes preference learning" in passage:
            return ClaimVerdict.SUPPORTED, "Source 2 entails stabilization."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No support."

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    record = verifier.verify_claim(claim, store)

    assert record.verdict == ClaimVerdict.SUPPORTED
    assert record.supporting_urls == [url1, url2]
    assert f"[{url1}]" in record.passage_text
    assert f"[{url2}]" in record.passage_text


def test_phase2p_claim_where_one_substantive_clause_remains_unsupported(tmp_path):
    """7. When one substantive clause of a compound claim remains unsupported, verdict is insufficient with explicit missing details."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url,
        "DPO eliminates the need for training a separate reward model.",
        title="DPO Paper"
    )))

    claim = AtomicClaim(
        claim_id="claim_2p_07",
        claim_text="DPO eliminates the need for a separate reward model, and DPO achieves 100% win rates across all alignment benchmarks.",
        cited_urls=[url],
        source_url=url,
        constituent_clauses=[
            "DPO eliminates the need for a separate reward model",
            "and DPO achieves 100% win rates across all alignment benchmarks",
        ],
    )

    def mock_eval(clause, passage, src_url):
        has_rm = "eliminates the need" in clause
        has_winrate = "100% win rates" in clause
        if has_rm and has_winrate:
            if "eliminates the need" in passage and "100% win rates" in passage:
                return ClaimVerdict.SUPPORTED, "Passage confirms both clauses."
            return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Single passage does not support both clauses."
        if has_rm and "eliminates the need" in passage:
            return ClaimVerdict.SUPPORTED, "Supported by DPO passage."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence of 100% win rate."

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    record = verifier.verify_claim(claim, store)

    assert record.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert record.action_taken == ClaimAction.LABELED_UNVERIFIED.value
    # Rationale explicitly describes which clause lacks supporting evidence and which was supported
    assert "Compound claim is partially unsupported" in record.rationale
    assert "100% win rates" in record.rationale
    assert "separate reward model" in record.rationale


def test_phase2p_preservation_of_source_urls_and_passage_provenance(tmp_path):
    """8. Preservation of source URLs, passage IDs, and retrieval provenance in all verification records."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1, "Transformers process input sequences concurrently using multi-head self-attention.", title="Transformers"
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2, "FlashAttention computes exact attention with sub-quadratic memory.", title="FlashAttention"
    )))

    claim = AtomicClaim(
        claim_id="claim_2p_08",
        claim_text="Transformers process input sequences concurrently, while FlashAttention computes exact attention with sub-quadratic memory",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Transformers process input sequences concurrently",
            "while FlashAttention computes exact attention with sub-quadratic memory",
        ],
    )

    verifier = ClaimVerifier(action_policy="label")
    record = verifier.verify_claim(claim, store)

    assert record.verdict == ClaimVerdict.SUPPORTED
    assert record.supporting_urls == [url1, url2]
    assert record.source_url == url1
    assert record.passage_id is not None
    assert f"[{url1}]" in record.passage_text
    assert f"[{url2}]" in record.passage_text
    d = record.to_dict()
    assert d["supporting_urls"] == [url1, url2]
    assert d["verdict"] == "supported"


def test_phase2p_contradiction_and_verdict_thresholds_strictly_preserved(tmp_path):
    """9. Contradiction thresholds are strictly preserved: any contradictory passage immediately verdicts CONTRADICTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url,
        "DPO requires quadratic memory scaling with sequence length and cannot operate in linear time.",
        title="DPO Complexity"
    )))

    claim = AtomicClaim(
        claim_id="claim_2p_09",
        claim_text="DPO operates with strict linear computational complexity in sequence length.",
        cited_urls=[url],
        source_url=url,
    )

    def mock_eval(eval_text, passage, src_url):
        if "linear" in eval_text and "quadratic" in passage:
            return ClaimVerdict.CONTRADICTED, "Passage directly asserts quadratic complexity, refuting linear complexity."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No match."

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    record = verifier.verify_claim(claim, store)

    assert record.verdict == ClaimVerdict.CONTRADICTED
    assert record.action_taken == ClaimAction.LABELED_CONTRADICTED.value
    assert "directly asserts quadratic complexity" in record.rationale


# ---------------------------------------------------------------------------
# Phase 2S — Claim Verification Integrity Fix Tests
# ---------------------------------------------------------------------------

def test_phase2s_task1_reproduce_defect_compound_claim_partial_support(tmp_path):
    """Task 1: Compound claim whose evidence supports only one conjunct cannot receive supported.

    Claim: 'Both Method A and Method B improve alignment.'
    Source passage: 'Method A improves alignment significantly by aligning language models with human preferences.'
    Omitted: Method B.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2401.00001"
    passage = "Method A improves alignment significantly by aligning language models with human preferences."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Method A Paper")))

    claim = AtomicClaim(
        claim_id="claim_2s_01",
        claim_text="Both Method A and Method B improve alignment.",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    # 1. Complete compound claim must not receive supported
    assert rec.verdict != ClaimVerdict.SUPPORTED
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE

    # 2. Rationale must identify the unsupported clause (Method B)
    assert "Method B" in rec.rationale or "Method B improve alignment" in rec.rationale

    # 3. Evidence for Method A must not be treated as evidence for Method B
    assert rec.supporting_urls is None or url not in (rec.supporting_urls or [])


def test_phase2s_task1_phase2r_exact_example_rlhf_and_dpo(tmp_path):
    """Task 1 / 5: Exact Phase 2R Topic A false-support defect revalidation.

    Compound claim asserting facts about both RLHF and DPO when cited passage only discusses RLHF.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2409.14836"
    passage = (
        "RLHF aligns language model policies by training an explicit reward model "
        "and optimizing via proximal policy optimization with KL divergence penalties."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="RLHF Survey")))

    claim = AtomicClaim(
        claim_id="claim_2s_phase2r_01",
        claim_text="Both RLHF and DPO eliminate the need for training separate reward models.",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    # Must be rejected because DPO is absent from the passage and RLHF actually trains an explicit reward model
    assert rec.verdict in (ClaimVerdict.INSUFFICIENT_EVIDENCE, ClaimVerdict.CONTRADICTED)
    assert rec.verdict != ClaimVerdict.SUPPORTED
    assert "DPO" in rec.rationale


def test_phase2s_task2_joint_entailment_across_multiple_sources(tmp_path):
    """Task 2: Evidence from multiple sources jointly supports a compound claim when combined passages genuinely entail all clauses."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2401.00001"
    url2 = "https://arxiv.org/abs/2401.00002"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Method A reduces inference latency by caching intermediate key-value projections across attention heads.",
        title="Method A"
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "Method B stabilizes gradient updates by applying adaptive clipping to policy parameter updates.",
        title="Method B"
    )))

    claim = AtomicClaim(
        claim_id="claim_2s_02",
        claim_text="Method A reduces inference latency, while Method B stabilizes gradient updates",
        cited_urls=[url1, url2],
        source_url=url1,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.action_taken == ClaimAction.RETAINED.value
    assert rec.supporting_urls == [url1, url2]
    assert f"[{url1}]" in rec.passage_text
    assert f"[{url2}]" in rec.passage_text


def test_phase2s_task2_one_missing_clause_among_several_supported_clauses(tmp_path):
    """Task 2 / 4: One missing clause among several otherwise supported clauses prevents full support."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2401.00001"
    url2 = "https://arxiv.org/abs/2401.00002"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Method A reduces inference latency by caching intermediate projections.",
        title="Method A"
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "Method B stabilizes gradient updates using adaptive clipping.",
        title="Method B"
    )))

    # Compound claim with 3 clauses, where Method C is completely unsupported
    claim = AtomicClaim(
        claim_id="claim_2s_03",
        claim_text="Method A reduces inference latency, while Method B stabilizes gradient updates",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Method A reduces inference latency",
            "while Method B stabilizes gradient updates",
            "while Method C guarantees zero memory leakage",
        ],
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.verdict != ClaimVerdict.SUPPORTED
    assert "Method C" in rec.rationale
    assert rec.action_taken == ClaimAction.LABELED_UNVERIFIED.value


def test_phase2s_task2_passing_mention_does_not_establish_technical_property(tmp_path):
    """Task 2: A source that merely mentions a method or lists its name does not establish a technical property."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2401.00003"
    # Merely mentions the name in a list of baselines
    passage = "We evaluate our proposed system against existing baselines including Method A, Method B, and Method C."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Baseline Survey")))

    claim = AtomicClaim(
        claim_id="claim_2s_04",
        claim_text="Method B achieves sub-quadratic memory complexity in long context processing.",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.verdict != ClaimVerdict.SUPPORTED


def test_phase2s_task3_paper_titles_cannot_independently_substantiate_claims(tmp_path):
    """Task 3: Standalone paper titles cannot substitute for passages establishing technical claims."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2305.18290"
    # The snippet captured in store is ONLY the paper title
    pure_title = "Direct Preference Optimization: Your Language Model is Secretly a Reward Model"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, pure_title, title=pure_title)))

    claim = AtomicClaim(
        claim_id="claim_2s_05",
        claim_text="Direct Preference Optimization optimizes language models without training an explicit reward model.",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    # Must be marked insufficient because title alone cannot establish technical claims
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.verdict != ClaimVerdict.SUPPORTED


def test_phase2s_task3_author_lists_affiliations_and_bibliography_blocked(tmp_path):
    """Task 3: Author lists, affiliations, publication metadata, and bibliography entries cannot substantiate claims."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2305.18290"
    url2 = "https://arxiv.org/abs/2205.14135"

    bib_entry = "[1] Rafailov, R., Sharma, A., Mitchell, E., Ermon, S., Manning, C. D., & Finn, C. (2023). Direct preference optimization: Your language model is secretly a reward model. NeurIPS 2023."
    author_metadata = "Authors: Tri Dao, Daniel Y. Fu, Stefano Ermon, Atri Rudra, Christopher Re. Department of Computer Science, Stanford University."

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, bib_entry, title="Rafailov Bibliography")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url2, author_metadata, title="Dao Metadata")))

    claim1 = AtomicClaim(
        claim_id="claim_2s_06a",
        claim_text="Direct preference optimization provides closed-form policy optimization.",
        cited_urls=[url1],
        source_url=url1,
    )
    claim2 = AtomicClaim(
        claim_id="claim_2s_06b",
        claim_text="FlashAttention computes IO-aware exact attention tiling on GPU SRAM.",
        cited_urls=[url2],
        source_url=url2,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec1 = verifier.verify_claim(claim1, store)
    rec2 = verifier.verify_claim(claim2, store)

    assert rec1.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec2.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE


def test_phase2s_task3_heading_and_acronym_context_preserved(tmp_path):
    """Task 3: Preserve useful heading and acronym context from Phase 2P when substantive evidence follows."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2402.10038"
    captured_text = (
        "[Context: Hyperparameter Optimization (HPO) refers to algorithmic tuning of hyperparameters.]\n\n"
        "The technique yields a 15% increase in throughput across all benchmark suites."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, captured_text, title="HPO Study")))

    claim = AtomicClaim(
        claim_id="claim_2s_07",
        claim_text="HPO yields a 15% increase in throughput across all benchmark suites.",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.action_taken == ClaimAction.RETAINED.value


def test_phase2s_task4_compound_claim_with_contradiction(tmp_path):
    """Task 4: A compound claim with a genuine contradiction in one clause must evaluate to CONTRADICTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/2401.00001"
    url2 = "https://arxiv.org/abs/2401.00002"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Method A enhances alignment efficiency across multi-turn dialog tasks.",
        title="Method A"
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "Method B increases memory complexity to cubic time rather than quadratic time.",
        title="Method B"
    )))

    claim = AtomicClaim(
        claim_id="claim_2s_08",
        claim_text="Method A enhances alignment efficiency, while Method B maintains quadratic memory complexity",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Method A enhances alignment efficiency",
            "while Method B maintains quadratic memory complexity",
        ],
    )

    def mock_judge(c_text, p_text, u):
        if "quadratic" in c_text and "cubic" in p_text:
            return ClaimVerdict.CONTRADICTED, "Passage refutes quadratic complexity: states cubic time."
        if "enhances alignment" in c_text and "enhances alignment" in p_text:
            return ClaimVerdict.SUPPORTED, "Passage confirms alignment enhancement."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "No evidence."

    verifier = ClaimVerifier(verifier_fn=mock_judge, action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.CONTRADICTED
    assert rec.action_taken == ClaimAction.LABELED_CONTRADICTED.value
    assert "refutes quadratic complexity" in rec.rationale


def test_phase2s_task4_provenance_and_clause_level_attribution(tmp_path):
    """Task 4: Preservation of source URLs, passage IDs, and clause-level provenance."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.org/abs/1706.03762"
    url2 = "https://arxiv.org/abs/2205.14135"

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1,
        "Transformers process input sequences concurrently using multi-head self-attention mechanisms.",
        title="Vaswani et al."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2,
        "FlashAttention achieves exact attention computation with sub-quadratic memory footprint via GPU tiling.",
        title="Dao et al."
    )))

    claim = AtomicClaim(
        claim_id="claim_2s_09",
        claim_text="Transformers process input sequences concurrently, while FlashAttention achieves exact attention with sub-quadratic memory",
        cited_urls=[url1, url2],
        source_url=url1,
        constituent_clauses=[
            "Transformers process input sequences concurrently",
            "while FlashAttention achieves exact attention with sub-quadratic memory",
        ],
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url1, url2]
    assert rec.source_url == url1
    assert rec.passage_id is not None
    assert rec.passage_text is not None
    assert f"[{url1}]" in rec.passage_text
    assert f"[{url2}]" in rec.passage_text
    assert "supported by" in rec.rationale


# ---------------------------------------------------------------------------
# Phase 2T.2: Proposition-Level Verification & Evidence Completeness Regressions
# ---------------------------------------------------------------------------

def test_phase2t2_parenthetical_conjunction_single_component_supported(tmp_path):
    """Task 2 / 5: Parenthetical conjunction where only one component is supported (Topic B claim_008 reproduction).

    Cited source establishes PostgreSQL + TimescaleDB benchmarks against MongoDB,
    but completely omits JSONB columns. Must return INSUFFICIENT_EVIDENCE.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://github.com/austin-starks/MongoDB-vs-Timescale-DB"
    passage = (
        "[MongoDB vs PostgreSQL/TimescaleDB Performance Benchmark] Direct performance and architectural comparisons "
        "between PostgreSQL with TimescaleDB extension and MongoDB reveal distinct operational trade-offs for production workloads."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="MongoDB vs TimescaleDB")))

    claim = AtomicClaim(
        claim_id="claim_2t2_008_single",
        claim_text=(
            "Direct performance and architectural comparisons between PostgreSQL "
            "(including extensions like TimescaleDB and JSONB columns) and MongoDB "
            "reveal distinct operational trade-offs for production workloads"
        ),
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "JSONB" in rec.rationale
    assert rec.supporting_urls is None
    assert rec.action_taken == ClaimAction.LABELED_UNVERIFIED.value


def test_phase2t2_parenthetical_conjunction_both_components_supported_multi_source(tmp_path):
    """Task 2 / 5: Parenthetical conjunction where both components are supported across cited sources."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://github.com/austin-starks/MongoDB-vs-Timescale-DB"
    url2 = "https://github.com/zhalok/mongodb-bson-vs-postgres-jsonb-benchmarking"

    passage1 = (
        "Direct performance comparisons between PostgreSQL with TimescaleDB extension and MongoDB "
        "reveal distinct operational trade-offs for production workloads."
    )
    passage2 = (
        "Direct architectural and performance comparisons between PostgreSQL JSONB columns and MongoDB "
        "reveal distinct operational trade-offs across query and storage behavior."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url1, passage1, title="TimescaleDB vs Mongo")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url2, passage2, title="JSONB vs Mongo")))

    claim = AtomicClaim(
        claim_id="claim_2t2_008_both",
        claim_text=(
            "Direct performance and architectural comparisons between PostgreSQL "
            "(including extensions like TimescaleDB and JSONB columns) and MongoDB "
            "reveal distinct operational trade-offs for production workloads"
        ),
        cited_urls=[url1, url2],
        source_url=url1,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url1, url2]
    assert rec.action_taken == ClaimAction.RETAINED.value
    assert url1 in rec.passage_text
    assert url2 in rec.passage_text


def test_phase2t2_nested_punctuation_and_technical_names_not_split():
    """Task 2 / 5: Technical idioms, ranges, and nested punctuation must not be incorrectly split."""
    from research_crew.evidence.verifier import _decompose_claim_clauses

    # 1. Technical idioms with "and"
    claim_pnp = "Systems employing Plug and Play frameworks achieve zero-configuration deployment (Plug and Play)."
    decomposed_pnp = _decompose_claim_clauses(claim_pnp)
    assert len(decomposed_pnp) == 1
    assert "Plug and Play" in decomposed_pnp[0]

    # 2. Modifiers like "and later"
    claim_ver = "PostgreSQL (version 14 and later) provides improved performance for buffer management."
    decomposed_ver = _decompose_claim_clauses(claim_ver)
    assert len(decomposed_ver) == 1

    # 3. Numeric ranges
    claim_range = "Configurations with cache sizes (between 10MB and 20MB) were evaluated under load."
    decomposed_range = _decompose_claim_clauses(claim_range)
    assert len(decomposed_range) == 1

    # 4. Nested punctuation (properly splits outer parenthetical entities)
    claim_nested = "Databases (such as PostgreSQL (v14) and MongoDB (v6)) demonstrate distinct memory models."
    decomposed_nested = _decompose_claim_clauses(claim_nested)
    assert len(decomposed_nested) == 2
    assert "PostgreSQL (v14)" in decomposed_nested[0]
    assert "MongoDB (v6)" in decomposed_nested[1]


def test_phase2t2_passage_omits_substantive_clause_fails_verification(tmp_path):
    """Task 3 / 5: Regression where the selected passage omits one substantive clause (claim_014 snippet-deficit reproduction).

    Passage details weak durability persist primitive and avoiding anomalies, but OMITS throughput.
    Verifier must reject the claim as INSUFFICIENT_EVIDENCE.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/html/2110.01465v1"
    truncated_passage = (
        "We present AciKV , a transactional system whose design is centered around weak durability. "
        "AciKV exposes to users the normal transactional interface, but what sets it apart from others "
        "is a new “persist” primitive that decouples durability from commit. AciKV is a middle ground "
        "between systems that perform fast atomic operations, and ones that support transactions; "
        "this middle ground is useful as it provides similar performance to the former, while prevents "
        "isolation and consistency anomalies like the latter."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, truncated_passage, title="AciKV Truncated")))

    claim = AtomicClaim(
        claim_id="claim_2t2_014_truncated",
        claim_text=(
            "By introducing a weak durability design via a 'persist' primitive, transactional systems "
            "can decouple durability from commit, avoiding isolation and consistency anomalies while achieving "
            "significantly higher transaction throughput"
        ),
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "throughput" in rec.rationale.lower()
    assert rec.supporting_urls is None
    assert rec.action_taken == ClaimAction.LABELED_UNVERIFIED.value


def test_phase2t2_full_text_selects_complete_passage_with_all_clauses(tmp_path):
    """Task 3 / 5: Full-text source where a complete passage covering all substantive clauses is selected and supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/html/2110.01465v1"
    full_captured_text = (
        "Weakly Durable High-Performance Transactions\n"
        "Abstract\n"
        "We present AciKV , a transactional system whose design is centered around weak durability. "
        "AciKV exposes to users the normal transactional interface, but what sets it apart from others "
        "is a new “persist” primitive that decouples durability from commit. AciKV is a middle ground "
        "between systems that perform fast atomic operations, and ones that support transactions; "
        "this middle ground is useful as it provides similar performance to the former, while prevents "
        "isolation and consistency anomalies like the latter. Our evaluation using the YCSB benchmark "
        "shows that AciKV , under workloads that involve write requests, exhibits more than two orders of "
        "magnitude higher throughput than existing strongly durable systems.\n"
        "1 Introduction\n"
        "Existing disk-based database systems often sit on the two ends of the performance spectrum."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, full_captured_text, title="AciKV Full")))

    claim = AtomicClaim(
        claim_id="claim_2t2_014_full",
        claim_text=(
            "By introducing a weak durability design via a 'persist' primitive, transactional systems "
            "can decouple durability from commit, avoiding isolation and consistency anomalies while achieving "
            "significantly higher transaction throughput"
        ),
        cited_urls=[url],
        source_url=url,
    )

    # 1. Verify candidate passage selection contains throughput assertion
    passage, pid, rec_obj = find_passage_for_url_and_query(url, claim.claim_text, store)
    assert passage is not None
    assert "throughput" in passage.lower()
    assert "persist" in passage.lower()

    # 2. Verify complete claim evaluation evaluates to SUPPORTED
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url]
    assert "throughput" in rec.passage_text.lower()
    assert "persist" in rec.passage_text.lower()
    assert rec.action_taken == ClaimAction.RETAINED.value


def test_phase2t2_unresolved_anaphoric_referent_rejected(tmp_path):
    """Task 4 / 5: Unresolved anaphoric referent 'these dimensions' without definition must evaluate to INSUFFICIENT_EVIDENCE (claim_012 reproduction)."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://ar5iv.labs.arxiv.org/html/2309.15217"
    passage = (
        "With Ragas, we put forward a suite of metrics which can be used to evaluate these different "
        "dimensions without having to rely on ground truth human annotations."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Ragas Paper")))

    claim = AtomicClaim(
        claim_id="claim_2t2_012_unresolved",
        claim_text="Furthermore, automated suites like Ragas provide metrics to evaluate these dimensions without requiring ground-truth human annotations",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "unresolved anaphoric referent" in rec.rationale.lower()
    assert "these dimensions" in rec.rationale.lower()
    assert rec.supporting_urls is None
    assert rec.action_taken == ClaimAction.LABELED_UNVERIFIED.value


def test_phase2t2_resolved_referent_supported(tmp_path):
    """Task 4 / 5: Explicitly resolved referents with adequate evidence must evaluate to SUPPORTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://ar5iv.labs.arxiv.org/html/2309.15217"
    passage = (
        "Evaluating RAG architectures is challenging because there are several dimensions to consider: "
        "the ability of the retrieval system to identify relevant and focused context passages, "
        "the ability of the LLM to exploit such passages in a faithful way, or the quality of the generation itself. "
        "With Ragas, we put forward a suite of metrics which can be used to evaluate these different dimensions "
        "without having to rely on ground truth human annotations."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Ragas Paper")))

    claim = AtomicClaim(
        claim_id="claim_2t2_012_resolved",
        claim_text=(
            "Ragas evaluates retrieval relevance and answer faithfulness, providing metrics to evaluate "
            "these dimensions without requiring ground-truth human annotations"
        ),
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url]
    assert rec.action_taken == ClaimAction.RETAINED.value


def test_phase2t2_contradiction_and_negation_handling(tmp_path):
    """Task 5: Contradiction handling and negation detection."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2401.00001"
    passage = "Standard multi-head self-attention exhibits quadratic computational complexity in sequence length."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Attention Study")))

    # Contradiction: claim asserts linear complexity
    claim = AtomicClaim(
        claim_id="claim_2t2_contra_01",
        claim_text="Standard multi-head self-attention exhibits linear computational complexity in sequence length",
        cited_urls=[url],
        source_url=url,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.CONTRADICTED
    assert rec.action_taken == ClaimAction.LABELED_CONTRADICTED.value
    assert rec.supporting_urls is None


# ===========================================================================
# Phase 2T.3 Adversarial Stress-Testing Suite (Groups A through E)
# ===========================================================================

# --- Test Group A: Parenthetical Decomposition ---

def test_phase2t3_group_a_both_parenthetical_items_supported(tmp_path):
    """Group A.1: Both parenthetical items supported across sources -> SUPPORTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_ts = "https://github.com/austin-starks/MongoDB-vs-Timescale-DB"
    passage_ts = (
        "PostgreSQL with TimescaleDB extensions and MongoDB exhibit distinct performance trade-offs "
        "across relational and time-series benchmark workloads."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_ts, passage_ts, title="TimescaleDB vs MongoDB")))

    url_jsonb = "https://github.com/zhalok/mongodb-bson-vs-postgres-jsonb-benchmarking"
    passage_jsonb = (
        "PostgreSQL with JSONB columns and MongoDB exhibit distinct performance trade-offs "
        "across relational and document-oriented benchmark workloads."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_jsonb, passage_jsonb, title="JSONB vs MongoDB")))

    claim = AtomicClaim(
        claim_id="claim_2t3_a01",
        claim_text="PostgreSQL (including extensions like TimescaleDB and JSONB columns) and MongoDB exhibit distinct performance trade-offs",
        cited_urls=[url_ts, url_jsonb],
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert sorted(rec.supporting_urls) == sorted([url_ts, url_jsonb])
    assert rec.action_taken == ClaimAction.RETAINED.value


def test_phase2t3_group_a_only_first_item_supported(tmp_path):
    """Group A.2: Only first parenthetical item supported -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_ts = "https://github.com/austin-starks/MongoDB-vs-Timescale-DB"
    passage_ts = (
        "PostgreSQL with TimescaleDB extensions and MongoDB exhibit distinct performance trade-offs "
        "across relational and time-series benchmark workloads."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_ts, passage_ts, title="TimescaleDB vs MongoDB")))

    claim = AtomicClaim(
        claim_id="claim_2t3_a02",
        claim_text="PostgreSQL (including extensions like TimescaleDB and JSONB columns) and MongoDB exhibit distinct performance trade-offs",
        cited_urls=[url_ts],
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "jsonb" in rec.rationale.lower()
    assert rec.supporting_urls is None


def test_phase2t3_group_a_only_second_item_supported(tmp_path):
    """Group A.3: Only second parenthetical item supported -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_jsonb = "https://github.com/zhalok/mongodb-bson-vs-postgres-jsonb-benchmarking"
    passage_jsonb = (
        "PostgreSQL with JSONB columns and MongoDB exhibit distinct performance trade-offs "
        "across relational and document-oriented benchmark workloads."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_jsonb, passage_jsonb, title="JSONB vs MongoDB")))

    claim = AtomicClaim(
        claim_id="claim_2t3_a03",
        claim_text="PostgreSQL (including extensions like TimescaleDB and JSONB columns) and MongoDB exhibit distinct performance trade-offs",
        cited_urls=[url_jsonb],
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "timescaledb" in rec.rationale.lower()
    assert rec.supporting_urls is None


def test_phase2t3_group_a_neither_item_supported(tmp_path):
    """Group A.4: Neither parenthetical item supported -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_sqlite = "https://sqlite.org/benchmarks.html"
    passage_sqlite = "SQLite provides an embedded relational database engine optimized for lightweight local storage."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_sqlite, passage_sqlite, title="SQLite")))

    claim = AtomicClaim(
        claim_id="claim_2t3_a04",
        claim_text="PostgreSQL (including extensions like TimescaleDB and JSONB columns) and MongoDB exhibit distinct performance trade-offs",
        cited_urls=[url_sqlite],
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.supporting_urls is None


def test_phase2t3_group_a_three_item_list(tmp_path):
    """Group A.5: Three-item parenthetical list: all 3 supported vs 1 missing."""
    store = EvidenceStore.create(output_dir=tmp_path)
    u1 = "https://source1.org/ts"
    p1 = "PostgreSQL with TimescaleDB extensions and MongoDB exhibit distinct performance trade-offs."
    u2 = "https://source2.org/citus"
    p2 = "PostgreSQL with Citus extensions and MongoDB exhibit distinct performance trade-offs."
    u3 = "https://source3.org/jsonb"
    p3 = "PostgreSQL with JSONB columns and MongoDB exhibit distinct performance trade-offs."

    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(u1, p1, title="TimescaleDB")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(u2, p2, title="Citus")))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(u3, p3, title="JSONB")))

    claim_all = AtomicClaim(
        claim_id="claim_2t3_a05_all",
        claim_text="PostgreSQL (including extensions like TimescaleDB, Citus, and JSONB columns) and MongoDB exhibit distinct performance trade-offs",
        cited_urls=[u1, u2, u3],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec_all = verifier.verify_claim(claim_all, store)
    assert rec_all.verdict == ClaimVerdict.SUPPORTED
    assert sorted(rec_all.supporting_urls) == sorted([u1, u2, u3])

    # Missing Citus
    claim_partial = AtomicClaim(
        claim_id="claim_2t3_a05_partial",
        claim_text="PostgreSQL (including extensions like TimescaleDB, Citus, and JSONB columns) and MongoDB exhibit distinct performance trade-offs",
        cited_urls=[u1, u3],
    )
    rec_partial = verifier.verify_claim(claim_partial, store)
    assert rec_partial.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "citus" in rec_partial.rationale.lower()


def test_phase2t3_group_a_nested_parentheses_and_versions():
    """Group A.6: Nested parentheses and version numbers preserved cleanly."""
    text = "PostgreSQL (v14.2 (released in 2021) and v15.1) maintains compatibility with legacy SQL workloads."
    props = _decompose_parenthetical_propositions(text)
    assert len(props) == 2
    assert "v14.2 (released in 2021)" in props[0]
    assert "v15.1" in props[1]
    assert "legacy SQL workloads" in props[0]
    assert "legacy SQL workloads" in props[1]


def test_phase2t3_group_a_technical_names_and_idioms():
    """Group A.7 & A.8: Technical terms with 'and' and idioms must NOT be decomposed."""
    cas_text = "The system (using Compare and Swap operations) guarantees lock-free synchronization."
    cas_props = _decompose_parenthetical_propositions(cas_text)
    assert cas_props == [cas_text]

    idiom_texts = [
        "The architecture (featuring plug and play modules) supports dynamic reconfiguration.",
        "The system (optimizing read and write workloads) achieves sub-millisecond latencies.",
        "The optimizer (using divide and conquer strategies) partitions execution graphs.",
        "Network protocols (employing stop and wait ARQ) exhibit lower throughput.",
        "The storage engine (indexing keys and values in memory) reduces disk I/O.",
    ]
    for text in idiom_texts:
        props = _decompose_parenthetical_propositions(text)
        assert props == [text], f"Idiom unexpectedly split: {text} -> {props}"


def test_phase2t3_group_a_ranges_and_alternatives_not_split():
    """Group A.9: Parentheticals expressing ranges or alternatives must NOT be decomposed."""
    range_texts = [
        "Latency remains low (between 10 ms and 50 ms) under peak traffic.",
        "Execution time varies (between 10% and 50%) depending on cache hit rates.",
        "The engine operates (either synchronous or asynchronous) depending on durability settings.",
        "Throughput scales (from 100 to 500 requests per second) linearly.",
    ]
    for text in range_texts:
        props = _decompose_parenthetical_propositions(text)
        assert props == [text], f"Range/alternative unexpectedly split: {text} -> {props}"


def test_phase2t3_group_a_negation_and_qualifiers_preserved():
    """Group A.10: Negation/qualifiers attached to parentheticals must be preserved on all propositions."""
    text = "The engine (excluding TimescaleDB and JSONB columns) relies on standard B-tree storage."
    props = _decompose_parenthetical_propositions(text)
    assert len(props) == 2
    assert props[0] == "The engine (excluding TimescaleDB) relies on standard B-tree storage."
    assert props[1] == "The engine (excluding JSONB columns) relies on standard B-tree storage."


# --- Test Group B: Morphological Matching & Fallback Verification ---

def test_phase2t3_group_b_legitimate_morphological_variants(tmp_path):
    """Group B.1: Legitimate morphological variants match correctly."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://ragas.io/docs"
    passage = "Ragas evaluates retrieval relevance and answer faithfulness without human ground truth."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Ragas")))

    claim = AtomicClaim(
        claim_id="claim_2t3_b01",
        claim_text="Ragas evaluates relevant context and faithfulness without human ground truth",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.SUPPORTED


def test_phase2t3_group_b_unrelated_words_sharing_apparent_stems_rejected(tmp_path):
    """Group B.2: Unrelated words sharing apparent prefix/stem must NOT cause false support."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://unrelated.org/report"
    passage = "The morning train departs daily, and the local hospital treats patients with stationary equipment."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Unrelated Passage")))

    claim = AtomicClaim(
        claim_id="claim_2t3_b02",
        claim_text="The system provides extensive department oversight and treatment planning",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE


def test_phase2t3_group_b_negation_and_antonyms_detected_as_contradiction(tmp_path):
    """Group B.3: Explicit negation or antonyms must be detected as CONTRADICTED, never SUPPORTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://benchmark.org/reliability"
    passage = "The architecture is not reliable and unstable under heavy load."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Reliability Study")))

    claim = AtomicClaim(
        claim_id="claim_2t3_b03",
        claim_text="The architecture is reliable and stable under heavy load",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.CONTRADICTED


def test_phase2t3_group_b_antonym_prefixes_not_treated_as_matches(tmp_path):
    """Group B.4: Antonym prefixes (supervised vs unsupervised, labeled vs unlabeled) must not match."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://ml.org/learning"
    passage = "The algorithm performs unsupervised learning on unlabeled datasets without supervisory signals."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="ML Study")))

    claim = AtomicClaim(
        claim_id="claim_2t3_b04",
        claim_text="The algorithm performs supervised learning on labeled datasets",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict in (ClaimVerdict.CONTRADICTED, ClaimVerdict.INSUFFICIENT_EVIDENCE)
    assert rec.verdict != ClaimVerdict.SUPPORTED


def test_phase2t3_group_b_lexical_overlap_without_substantive_finding_rejected(tmp_path):
    """Group B.5: High lexical overlap without asserting the substantive finding must be rejected."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://benchmark.org/eval"
    passage = "PostgreSQL and MongoDB were compared on write throughput across several benchmark workloads."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Comparison Study")))

    claim = AtomicClaim(
        claim_id="claim_2t3_b05",
        claim_text="PostgreSQL achieves 10x higher write throughput than MongoDB",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE


# --- Test Group C: Passage Completeness ---

def test_phase2t3_group_c_all_claim_clauses_in_one_passage(tmp_path):
    """Group C.1: All clauses present in a single complete passage -> SUPPORTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2110.01465"
    passage = (
        "AciKV provides a weak durability persist primitive that decouples transaction commit from persistence "
        "to non-volatile memory. Our evaluation using the YCSB benchmark shows that AciKV exhibits more than two "
        "orders of magnitude higher throughput than existing strongly durable systems while preserving consistency."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="AciKV Paper")))

    claim = AtomicClaim(
        claim_id="claim_2t3_c01",
        claim_text=(
            "AciKV demonstrates that a weak durability persist primitive can decouple transaction commit from "
            "non-volatile memory persistence, providing significantly higher transaction throughput while preserving consistency"
        ),
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url]


def test_phase2t3_group_c_missing_final_clause_fails(tmp_path):
    """Group C.2: A truncated passage omitting the final performance clause -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2110.01465"
    truncated_passage = (
        "AciKV provides a weak durability persist primitive that decouples transaction commit from persistence "
        "to non-volatile memory while preserving consistency guarantees."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, truncated_passage, title="AciKV Truncated")))

    claim = AtomicClaim(
        claim_id="claim_2t3_c02",
        claim_text=(
            "AciKV demonstrates that a weak durability persist primitive can decouple transaction commit from "
            "non-volatile memory persistence, providing significantly higher transaction throughput while preserving consistency"
        ),
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "throughput" in rec.rationale.lower()


def test_phase2t3_group_c_evidence_split_across_adjacent_passages(tmp_path):
    """Group C.3: Evidence split across two passages from the same source -> SUPPORTED via multi-passage provenance."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://systems.org/storage"
    full_text = (
        "Section 1: Mechanism.\n"
        "The system decouples transaction commits from background logging to durable storage.\n\n"
        "Section 5: Performance Evaluation.\n"
        "Experimental results demonstrate that this decoupling achieves significantly higher transaction throughput under write contention."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, full_text, title="Storage Systems Paper")))

    claim = AtomicClaim(
        claim_id="claim_2t3_c03",
        claim_text=(
            "The system decouples transaction commits from background logging, "
            "providing significantly higher transaction throughput under write contention"
        ),
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert rec.supporting_urls == [url]


def test_phase2t3_group_c_mechanism_supported_performance_unsupported(tmp_path):
    """Group C.4: Mechanism supported but performance assertion unsupported anywhere -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2110.01465"
    mechanism_only_text = (
        "AciKV introduces a weak durability persist primitive that decouples transaction commit from non-volatile memory persistence. "
        "We discuss theoretical consistency guarantees in Section 3."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, mechanism_only_text, title="AciKV Mechanism")))

    claim = AtomicClaim(
        claim_id="claim_2t3_c04",
        claim_text=(
            "AciKV decouples transaction commit from persistence, "
            "achieving a 50x speedup over state-of-the-art key-value stores"
        ),
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "speedup" in rec.rationale.lower() or "50x" in rec.rationale.lower()


# --- Test Group D: Anaphoric References ---

def test_phase2t3_group_d_unresolved_referent_rejected(tmp_path):
    """Group D.1: Unresolved anaphoric demonstrative ('these dimensions', 'such metrics') -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://ragas.io/eval"
    passage = "Ragas provides metrics to evaluate these different dimensions without ground-truth annotations."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Ragas")))

    claim = AtomicClaim(
        claim_id="claim_2t3_d01",
        claim_text="Automated suites like Ragas provide metrics to evaluate these dimensions without ground truth",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "unresolved anaphoric referent" in rec.rationale.lower()


def test_phase2t3_group_d_clearly_resolved_reference_in_same_claim(tmp_path):
    """Group D.2: Explicitly grounded dimensions within the claim text -> SUPPORTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://ragas.io/eval"
    passage = (
        "Ragas evaluates retrieval relevance and answer faithfulness, "
        "providing metrics for these dimensions without requiring ground truth annotations."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Ragas")))

    claim = AtomicClaim(
        claim_id="claim_2t3_d02",
        claim_text="Ragas evaluates retrieval relevance and answer faithfulness, providing metrics for these dimensions without ground truth",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.SUPPORTED


def test_phase2t3_group_d_unrelated_keywords_do_not_ground_referent(tmp_path):
    """Group D.3: Incidental words like 'performance' or 'quality' must NOT count as antecedents."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://hpc.org/paper"
    passage = "The benchmark evaluates metrics in high performance computing environments."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="HPC Benchmark")))

    # 'performance' appears in 'high performance computing', not as a defined dimension
    claim_hpc = AtomicClaim(
        claim_id="claim_2t3_d03_hpc",
        claim_text="The benchmark evaluates these dimensions in high performance computing environments",
        cited_urls=[url],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec_hpc = verifier.verify_claim(claim_hpc, store)
    assert rec_hpc.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "unresolved anaphoric referent" in rec_hpc.rationale.lower()

    # 'quality' appears in 'code quality team', not as a defined metric
    claim_qa = AtomicClaim(
        claim_id="claim_2t3_d03_qa",
        claim_text="Our code quality team evaluates these metrics without manual intervention",
        cited_urls=[url],
    )
    rec_qa = verifier.verify_claim(claim_qa, store)
    assert rec_qa.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "unresolved anaphoric referent" in rec_qa.rationale.lower()


def test_phase2t3_group_d_concept_in_different_source_not_cited_source(tmp_path):
    """Group D.4: Concept exists in Source B, but claim only cites Source A which omits it -> INSUFFICIENT_EVIDENCE."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_a = "https://source-a.org/overview"
    passage_a = "Source A introduces automated pipeline runners for batch execution."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_a, passage_a, title="Source A")))

    url_b = "https://source-b.org/metrics"
    passage_b = "Source B defines retrieval relevance and answer faithfulness benchmarks."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_b, passage_b, title="Source B")))

    # Claim cites Source A only, but asserts Ragas metrics
    claim = AtomicClaim(
        claim_id="claim_2t3_d04",
        claim_text="Automated suites evaluate retrieval relevance and answer faithfulness across these dimensions",
        cited_urls=[url_a],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE


# --- Test Group E: Existing Invariants ---

def test_phase2t3_group_e_existing_invariants(tmp_path):
    """Group E: Verify historical RLHF/DPO false support, joint multi-source, and contradiction invariants."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url_rlhf = "https://arxiv.org/abs/1706.03741"
    passage_rlhf = "Reinforcement Learning from Human Feedback (RLHF) is a standard alignment technique for large language models."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_rlhf, passage_rlhf, title="RLHF Paper")))

    url_dpo = "https://arxiv.org/abs/2305.18290"
    passage_dpo = "Direct Preference Optimization (DPO) is a standard alignment technique for large language models."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_dpo, passage_dpo, title="DPO Paper")))

    # 1. Historical RLHF/DPO false support: only RLHF source cited -> INSUFFICIENT_EVIDENCE
    claim_rlhf_dpo_partial = AtomicClaim(
        claim_id="claim_2t3_e01_partial",
        claim_text="RLHF and DPO are standard alignment techniques for large language models",
        cited_urls=[url_rlhf],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec_partial = verifier.verify_claim(claim_rlhf_dpo_partial, store)
    assert rec_partial.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "dpo" in rec_partial.rationale.lower()

    # 2. Joint multi-source support: both RLHF and DPO sources cited -> SUPPORTED
    claim_rlhf_dpo_joint = AtomicClaim(
        claim_id="claim_2t3_e01_joint",
        claim_text="RLHF and DPO are standard alignment techniques for large language models",
        cited_urls=[url_rlhf, url_dpo],
    )
    rec_joint = verifier.verify_claim(claim_rlhf_dpo_joint, store)
    assert rec_joint.verdict == ClaimVerdict.SUPPORTED
    assert sorted(rec_joint.supporting_urls) == sorted([url_rlhf, url_dpo])

    # 3. Contradiction: claim states linear, passage states quadratic
    url_contra = "https://arxiv.org/abs/2401.99999"
    passage_contra = "Self-attention computational complexity is quadratic in sequence length."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_contra, passage_contra, title="Complexity")))

    claim_contra = AtomicClaim(
        claim_id="claim_2t3_e02_contra",
        claim_text="Self-attention computational complexity is linear in sequence length",
        cited_urls=[url_contra],
    )
    rec_contra = verifier.verify_claim(claim_contra, store)
    assert rec_contra.verdict == ClaimVerdict.CONTRADICTED
    assert rec_contra.action_taken == ClaimAction.LABELED_CONTRADICTED.value


# ===========================================================================
# Phase 2U — Targeted Verification Failure Correction Regression Tests
# ===========================================================================

def test_phase2u_entity_filter_verbs_and_adverbs():
    """Reproduce Phase 2T.5 false rejections on non-entity verbs, adverbs, and generic nouns,
    and verify that genuine entity mismatches remain rejected.
    """
    # 1. Topic A claim_005: 'architecture indicates'
    c1 = "Ablation of this architecture indicates that token masking alone produces a 0.28 drop in score."
    p1 = "Table 4 shows removing token masking reduces score by 0.28 across validation sets."
    ok1, msg1 = _check_entities_present(c1, p1)
    assert ok1, f"Failed on 'architecture indicates': {msg1}"

    # 2. Topic B claim_003: 'systems often'
    c2 = "Modern database systems often incorporate JSON data types to handle semi-structured data."
    p2 = "Relational database systems incorporate JSON data types for semi-structured data handling."
    ok2, msg2 = _check_entities_present(c2, p2)
    assert ok2, f"Failed on 'systems often': {msg2}"

    # 3. Topic B claim_017: 'model designs'
    c3 = "Different database model designs trade write latency for read throughput."
    p3 = "Relational and document architectures trade write latency for read throughput."
    ok3, msg3 = _check_entities_present(c3, p3)
    assert ok3, f"Failed on 'model designs': {msg3}"

    # 4. Topic A claim_012 & claim_024: 'model extrapolation'
    c4 = "When tested under model extrapolation scenarios, the accuracy fell from 91% to 64%."
    p4 = "Under context extrapolation testing, model accuracy fell from 91% to 64%."
    ok4, msg4 = _check_entities_present(c4, p4)
    assert ok4, f"Failed on 'model extrapolation': {msg4}"

    # Verify deterministic fallback path with equivalent paraphrase
    verifier = ClaimVerifier(action_policy="label")
    p1_fb = "Ablation of this architecture demonstrates that token masking alone produces a 0.28 drop in score."
    fb_verdict, fb_rat = verifier._heuristic_fallback_verify(c1, p1_fb)
    assert fb_verdict == ClaimVerdict.SUPPORTED
    assert "indicates" not in fb_rat

    # NEGATIVE TEST: Missing required entity identifier 'Method B' must still be rejected!
    c_neg = "Method B outperforms baseline approaches on long-context benchmarks."
    p_neg = "Method A achieves superior scores on long-context benchmarks."
    ok_neg, msg_neg = _check_entities_present(c_neg, p_neg)
    assert not ok_neg, "Should have rejected missing entity Method B"
    assert "B" in msg_neg
    fb_neg_verdict, fb_neg_rat = verifier._heuristic_fallback_verify(c_neg, p_neg)
    assert fb_neg_verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "B" in fb_neg_rat


def test_phase2u_entity_filter_compounds_and_inflections():
    """Reproduce Phase 2T.5 false rejections on hyphenated compounds, acronym expansions,
    and plural inflections, and verify that genuine entity mismatches remain rejected.
    """
    # 1. Topic C claim_004: 'RAG-generated' compound token
    c1 = "The benchmark evaluates RAG-generated answers across multiple domains."
    p1 = "We evaluate RAG systems, analyzing generated answers across 10 distinct domains."
    ok1, msg1 = _check_entities_present(c1, p1)
    assert ok1, f"Failed on 'RAG-generated': {msg1}"

    # 2. Topic C claim_021: 'Mean Reciprocal Rank [MRR]' grounded by acronym
    c2 = "The system achieves a Mean Reciprocal Rank [MRR] of 0.82 across queries."
    p2 = "The system achieves an MRR of 0.82 across all evaluated queries."
    ok2, msg2 = _check_entities_present(c2, p2)
    assert ok2, f"Failed on 'Mean Reciprocal Rank [MRR]': {msg2}"

    # 3. Topic C claim_026: 'Wilcoxon Signed-Rank Tests' (plural vs singular)
    c3 = "Statistical significance was confirmed using Wilcoxon Signed-Rank Tests."
    p3 = "Statistical significance was confirmed using the Wilcoxon Signed-Rank test (p < 0.01)."
    ok3, msg3 = _check_entities_present(c3, p3)
    assert ok3, f"Failed on 'Wilcoxon Signed-Rank Tests': {msg3}"

    verifier = ClaimVerifier(action_policy="label")
    fb_verdict, fb_rat = verifier._heuristic_fallback_verify(c3, p3)
    assert fb_verdict == ClaimVerdict.SUPPORTED

    # NEGATIVE TEST: Missing technical entity PostgreSQL when passage mentions MongoDB
    c_neg = "PostgreSQL provides ACID transactions with serializable snapshot isolation."
    p_neg = "MongoDB is a document store with tunable consistency models."
    ok_neg, msg_neg = _check_entities_present(c_neg, p_neg)
    assert not ok_neg, "Should have rejected missing technical entity PostgreSQL"
    assert "PostgreSQL" in msg_neg
    fb_neg_verdict, fb_neg_rat = verifier._heuristic_fallback_verify(c_neg, p_neg)
    assert fb_neg_verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "PostgreSQL" in fb_neg_rat


def test_phase2u_alphanumeric_citation_resolution(tmp_path):
    """Reproduce Phase 2T.5 Topic B failure to resolve named alphanumeric citation markers,
    and verify that unmapped citations and unsupported claims remain unverified.
    """
    report_text = """# Database Architecture
Relational engines provide ACID guarantees [zhalok-jsonb].
MongoDB provides flexible document models [austin-starks-timescale].
The retriever achieves 0.85 Mean Reciprocal Rank [MRR] on benchmark data [1].

## References
* [zhalok-jsonb]: https://github.com/zhalok/mongodb-bson-vs-postgres-jsonb-benchmarking
* [austin-starks-timescale]: https://github.com/austin-starks/timescaledb-vs-mongodb
* [1]: https://arxiv.org/abs/2401.00001
"""
    mapping = extract_report_source_mapping(report_text)
    assert "[zhalok-jsonb]" in mapping or "zhalok-jsonb" in mapping
    assert "[austin-starks-timescale]" in mapping or "austin-starks-timescale" in mapping
    assert "[1]" in mapping or "1" in mapping

    claims = extract_atomic_claims(report_text, mapping)
    c_zhalok = next(c for c in claims if "Relational engines" in c.claim_text)
    assert c_zhalok.source_url == "https://github.com/zhalok/mongodb-bson-vs-postgres-jsonb-benchmarking"
    assert "[zhalok-jsonb]" not in c_zhalok.claim_text

    c_mrr = next(c for c in claims if "Mean Reciprocal Rank" in c.claim_text)
    assert c_mrr.source_url == "https://arxiv.org/abs/2401.00001"
    assert "[MRR]" in c_mrr.claim_text  # Acronym tag preserved in claim statement

    # NEGATIVE TEST: Missing citation marker in bibliography yields unresolved citation
    unresolved_res = _resolve_citations("Claim cites missing source [missing-key].", mapping)
    assert unresolved_res[0][1] == ""

    # NEGATIVE TEST: Claim citing resolved source with unsupported clause remains INSUFFICIENT_EVIDENCE
    store = EvidenceStore.create(output_dir=tmp_path)
    url_zhalok = "https://github.com/zhalok/mongodb-bson-vs-postgres-jsonb-benchmarking"
    passage_zhalok = "PostgreSQL JSONB columns store semi-structured binary JSON representations."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_zhalok, passage_zhalok, title="Zhalok Benchmarks")))

    unsupported_claim = AtomicClaim(
        claim_id="claim_2u_unsupported",
        claim_text="PostgreSQL JSONB supports indexing, whereas MongoDB provides multi-master active replication across five continents.",
        cited_urls=[url_zhalok],
    )
    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(unsupported_claim, store)
    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.verdict != ClaimVerdict.SUPPORTED


def test_phase2u_passage_selection_and_window_expansion(tmp_path):
    """Reproduce Phase 2T.5 Topic C passage truncation defect where 800-token candidate limit
    and lack of empirical weighting caused 'TREC 2024' passage to truncate before findings.
    """
    full_text = """Support Evaluation for the TREC 2024 RAG Track: Comparing Human versus LLM Judges
Abstract
Retrieval-augmented generation (RAG) enables large language models (LLMs) to generate answers with citations from source documents containing ground truth, thereby reducing system hallucinations.
A crucial factor in RAG evaluation is support—whether the information in the cited documents supports the answer.
To this end, we conducted a large-scale comparative study of 45 participant submissions on 36 topics to the TREC 2024 RAG Track, comparing an automatic LLM judge (GPT-4o) against human judges for support assessment.
We considered two conditions: (1) fully manual assessments from scratch and (2) manual assessments with post-editing of LLM predictions.
Our results indicate that for 56% of the manual from-scratch assessments, human and GPT-4o predictions match perfectly (on a three-level scale), increasing to 72% in the manual with post-editing condition.
Furthermore, by carefully analyzing the disagreements in an unbiased study, we found that an independent human judge correlates better with GPT-4o than a human judge, suggesting that LLM judges can be a reliable alternative for support assessment.

1 Introduction and Prior Work
Prior work on support evaluation in the RAG literature relies on an automatic judge, i.e., an LLM as a proxy judge.
However, it is unknown whether an LLM judge can potentially replace a human judge for support evaluation.
This paper examines results from the TREC 2024 RAG Track, assessing 45 participant systems on 36 information-based queries.
"""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2504.15205"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, full_text, title="TREC 2024 Study")))

    query = "TREC 2024 demonstrates GPT-4o aligns closely with humans"
    passage, pid, rec = find_passage_for_url_and_query(url, query, store)
    assert passage is not None
    assert "GPT-4o" in passage
    assert "Our results indicate" in passage or "match perfectly" in passage
    assert not passage.startswith("Prior work")

    # NEGATIVE TEST: Zero matching query tokens must NOT be selected even if text has empirical keywords
    passage_none, _, _ = find_passage_for_url_and_query(url, "Quantum computing decoherence times exceed 100 milliseconds", store)
    assert passage_none is None


def test_phase2u_llm_verification_mock_and_invariant_preservation(tmp_path):
    """Test both Mesh LLM mock verification and preservation of multi-clause and contradiction invariants."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/abs/2401.00010"
    passage = "Ablation of this architecture indicates that token masking alone produces a 0.28 drop in score."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage, title="Ablation Study")))

    claim = AtomicClaim(
        claim_id="claim_2u_mesh_01",
        claim_text="Ablation of this architecture indicates that token masking alone produces a 0.28 drop in score.",
        cited_urls=[url],
    )

    # 1. Configured Mesh LLM path returning valid JSON support
    def mock_llm_fn(prompt: str) -> str:
        if "attention masking" in prompt:
            return '{"verdict": "insufficient_evidence", "rationale": "Passage does not mention attention masking or zero latency overhead."}'
        if "Token masking decreased" in prompt:
            return '{"verdict": "contradicted", "rationale": "Claim states decreased by 0.28 but passage shows increased by 0.28."}'
        return '{"verdict": "supported", "rationale": "Passage empirically reports 0.28 drop from token masking."}'

    mock_llm = MagicMock()
    mock_llm.call = MagicMock(side_effect=mock_llm_fn)
    verifier = ClaimVerifier(llm=mock_llm, action_policy="label")

    # Claim with slight paraphrase engages LLM verification rather than verbatim match
    claim_llm = AtomicClaim(
        claim_id="claim_2u_mesh_01",
        claim_text="Ablation of this architecture indicates that token masking alone causes a 0.28 score drop.",
        cited_urls=[url],
    )
    rec = verifier.verify_claim(claim_llm, store)
    assert rec.verdict == ClaimVerdict.SUPPORTED
    assert "0.28 drop" in rec.rationale

    # 2. Multi-clause invariant: if claim adds unsupported second clause, must remain INSUFFICIENT
    compound_claim = AtomicClaim(
        claim_id="claim_2u_mesh_02",
        claim_text="Ablation of this architecture indicates that token masking alone produces a 0.28 drop in score, whereas attention masking yields zero latency overhead.",
        cited_urls=[url],
    )
    rec_compound = verifier.verify_claim(compound_claim, store)
    assert rec_compound.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "partially unsupported" in rec_compound.rationale

    # 3. Contradiction invariant: if passage states increased instead of drop -> CONTRADICTED
    url_contra = "https://arxiv.org/abs/2401.00020"
    passage_contra = "Ablation of this architecture shows token masking increased the score by 0.28."
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_contra, passage_contra, title="Contra Study")))
    claim_contra = AtomicClaim(
        claim_id="claim_2u_mesh_03",
        claim_text="Token masking decreased the score by 0.28",
        cited_urls=[url_contra],
    )
    rec_contra = verifier.verify_claim(claim_contra, store)
    assert rec_contra.verdict == ClaimVerdict.CONTRADICTED


def test_phase2u1_false_support_protections(tmp_path):
    """Phase 2U.1 Task 3: Comprehensive negative tests protecting against false support:
    1. Valid citation pointing to same topic but unsupporting source -> INSUFFICIENT_EVIDENCE
    2. Compound claim with one supported clause but missing another -> INSUFFICIENT_EVIDENCE
    3. Similar terminology belonging to a different method/technology -> INSUFFICIENT_EVIDENCE
    4. Citation marker resembling an acronym ([MRR]) that is not in bibliography -> not treated as citation
    5. Named citation marker in bibliography pointing to irrelevant evidence -> INSUFFICIENT_EVIDENCE
    6. Refuted claim retains CONTRADICTED verdict
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    verifier = ClaimVerifier(action_policy="label")

    # 1. Valid citation pointing to a source on same topic but not supporting the claim
    url_topic = "https://arxiv.org/abs/2401.iot_market"
    text_topic = (
        "We perform a market analysis on relational and NoSQL database systems. "
        "We compare MySQL, PostgreSQL, Cassandra, and MongoDB based on functional and non-functional criteria "
        "to provide a best practice guide for IoT database selection."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_topic, text_topic, title="IoT DB Market Analysis")))
    c1 = AtomicClaim(
        claim_id="neg_c1",
        claim_text="PostgreSQL achieves 10x higher write throughput than MongoDB under distributed ACID transactions.",
        cited_urls=[url_topic],
    )
    r1 = verifier.verify_claim(c1, store)
    assert r1.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert r1.verdict != ClaimVerdict.SUPPORTED

    # 2. Compound claim: passage contains one supported clause but missing another
    url_compound = "https://arxiv.org/abs/2404.16792"
    text_compound = (
        "We propose EXPO (model extrapolation) to expedite LLMs' alignment with human preferences "
        "without additional training overhead."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_compound, text_compound, title="EXPO Paper")))
    c2 = AtomicClaim(
        claim_id="neg_c2",
        claim_text="EXPO expedites LLM alignment with human preferences, whereas attention masking yields zero latency overhead.",
        cited_urls=[url_compound],
    )
    # Heuristic path catches missing entity 'masking' or 'latency' immediately
    r2_heuristic = verifier.verify_claim(c2, store)
    assert r2_heuristic.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE

    # LLM path evaluates each decomposed clause and flags partially unsupported
    mock_llm_compound = MagicMock()
    mock_llm_compound.call = MagicMock(side_effect=lambda prompt: (
        '{"verdict": "insufficient_evidence", "rationale": "Passage contains no data on attention masking"}'
        if "attention masking" in prompt
        else '{"verdict": "supported", "rationale": "EXPO expedites alignment"}'
    ))
    verifier_compound = ClaimVerifier(llm=mock_llm_compound, action_policy="label")
    r2_llm = verifier_compound.verify_claim(c2, store)
    assert r2_llm.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "partially unsupported" in r2_llm.rationale

    # 3. Similar terminology belonging to a different method or technology
    url_method = "https://arxiv.org/abs/2305.18290"
    text_method = (
        "Direct Preference Optimization (DPO) optimizes language model policies directly from preference data "
        "using an implicit reward formulation with a reference policy."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_method, text_method, title="DPO Paper")))
    c3 = AtomicClaim(
        claim_id="neg_c3",
        claim_text="SimPO eliminates the reference policy by using length-normalized sequence probabilities as the implicit reward.",
        cited_urls=[url_method],
    )
    r3 = verifier.verify_claim(c3, store)
    assert r3.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "SimPO" in r3.rationale or "does not mention required" in r3.rationale

    # 4. Citation marker resembling an acronym ([MRR]) that is not a bibliography reference
    report_text = (
        "# Evaluation\n"
        "The retriever achieves 0.82 Mean Reciprocal Rank [MRR] on benchmark queries [1].\n"
        "Another metric evaluates Normalized Discounted Cumulative Gain [nDCG] without citation.\n\n"
        "## Sources\n"
        "[1] https://arxiv.org/abs/2401.retrieval\n"
    )
    mapping = extract_report_source_mapping(report_text)
    assert "[1]" in mapping
    assert "[MRR]" not in mapping
    assert "[nDCG]" not in mapping

    extracted = extract_atomic_claims(report_text, mapping)
    mrr_claim = next(c for c in extracted if "Mean Reciprocal Rank" in c.claim_text)
    assert mrr_claim.citation_marker == "[1]"
    assert mrr_claim.source_url == "https://arxiv.org/abs/2401.retrieval"
    assert "[MRR]" in mrr_claim.claim_text  # Acronym is preserved in text, not stripped as citation

    # If claim text only contains [MRR] without bibliography entry, it is NOT parsed as a citation
    unmapped = _resolve_citations("Testing Mean Reciprocal Rank [MRR] alone.", mapping)
    assert len(unmapped) == 0  # [MRR] is not recognized as a citation marker

    # When verified, an ungrounded claim with only [MRR] remains unverified due to lack of citation
    no_cite_claim = AtomicClaim(
        claim_id="neg_no_cite",
        claim_text="Mean Reciprocal Rank [MRR] exceeds 0.85 across test queries.",
        cited_urls=[],
    )
    r_no_cite = verifier.verify_claim(no_cite_claim, store)
    assert r_no_cite.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "lacks a valid, resolvable source URL" in r_no_cite.rationale

    # 5. Named citation marker that exists in bibliography but points to irrelevant evidence
    url_named = "https://github.com/milvus-io/benchmarks"
    text_irrelevant = (
        "This repository contains shell scripts and helm charts to deploy Milvus on Kubernetes clusters "
        "and monitor cluster memory consumption."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_named, text_irrelevant, title="Milvus Scripts")))
    report_named = (
        "# Indexing\n"
        "Hierarchical quantization accelerates multi-vector search [milvus-benchmark].\n\n"
        "## Sources\n"
        f"[milvus-benchmark] {url_named}\n"
    )
    map_named = extract_report_source_mapping(report_named)
    named_claims = extract_atomic_claims(report_named, map_named)
    r5 = verifier.verify_claim(named_claims[0], store)
    assert r5.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert r5.verdict != ClaimVerdict.SUPPORTED

    # 6. Contradictions must remain CONTRADICTED
    url_contra = "https://arxiv.org/abs/2504.15205"
    text_contra = (
        "Our empirical results indicate that automated LLM judges exhibit substantial disagreement "
        "(matching human judgments in less than 20% of cases) on support assessment."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url_contra, text_contra, title="TREC Disagreement Study")))
    c6 = AtomicClaim(
        claim_id="neg_c6",
        claim_text="Automated LLM judges align closely (achieving over 80% agreement) with human assessors on support evaluation tasks.",
        cited_urls=[url_contra],
    )
    # Using mock LLM that classifies contradiction
    mock_contra_llm = MagicMock()
    mock_contra_llm.call = MagicMock(return_value='{"verdict": "contradicted", "rationale": "Passage states agreement is less than 20%, directly contradicting over 80% agreement claim."}')
    verifier_contra = ClaimVerifier(llm=mock_contra_llm, action_policy="label")
    r6 = verifier_contra.verify_claim(c6, store)
    assert r6.verdict == ClaimVerdict.CONTRADICTED


# ===========================================================================
# Phase 2V.2 Regression Tests: Targeted Retrieval & Passage Selection Fixes
# ===========================================================================

def test_phase2v2_colon_capitalized_verbs_not_treated_as_entities():
    """Phase 2V.2 Issue 1: Words following colons/bullets (e.g. 'Utilizes', 'Employs', 'Combines')
    must not be treated as proper-noun entities and cause false rejection when LLM confirms support.
    """
    mock_llm = MagicMock()
    mock_llm.call = MagicMock(return_value='{"verdict": "supported", "rationale": "Directly supported by passage."}')
    verifier = ClaimVerifier(llm=mock_llm, action_policy="label")

    # Claim using bullet structure 'STree: Utilizes...'
    claim_text = (
        "STree: Utilizes accumulated state transition matrices to execute speculative tree decoding across SSMs, "
        "resolving the challenges of explicit state management."
    )
    # Passage uses synonym 'exploits' instead of 'Utilizes'
    passage_text = (
        "We propose STree, a scalable algorithm for tree-based speculative decoding in state-space models (SSMs). "
        "We exploit the structure of accumulated state transition matrices to facilitate speculative tree decoding "
        "with minimal overhead."
    )

    verdict, rationale = verifier._verify_via_mesh_llm(
        claim_text=claim_text,
        passage_text=passage_text,
        source_url="https://arxiv.org/html/2505.14969v2",
    )
    assert verdict == ClaimVerdict.SUPPORTED
    assert "Passage does not mention required technical entity 'Utilizes'" not in rationale

    # Test entity checker directly
    ok, msg = _check_entities_present(claim_text, passage_text)
    assert ok is True, f"Entity checker should pass but failed with: {msg}"


def test_phase2v2_acronym_defined_in_claim_with_lowercase_passage():
    """Phase 2V.2 Issue 2: When claim defines an acronym alongside full term (e.g. 'indirect object identification (IOI)'),
    and passage contains the lowercase full term ('indirect object identification'), acronym must be grounded.
    """
    mock_llm = MagicMock()
    mock_llm.call = MagicMock(return_value='{"verdict": "supported", "rationale": "Circuits for indirect object identification confirmed."}')
    verifier = ClaimVerifier(llm=mock_llm, action_policy="label")

    claim_text = (
        "Furthermore, architectural design dictates circuit localization: models utilizing grouped-query attention (GQA) "
        "yield circuits that concentrate tightly into one or two heads for tasks like induction, indirect object identification (IOI), "
        "and factual recall."
    )
    passage_text = (
        "We compare Pythia (Biderman et al., 2023), which uses standard Multi-Head Attention (MHA) throughout, "
        "against Qwen2.5 (Yang et al., 2024), which uses Grouped Query Attention (GQA) throughout. "
        "We test three circuit types (indirect object identification, induction heads and factual recall) across six benchmarks."
    )

    verdict, rationale = verifier._verify_via_mesh_llm(
        claim_text=claim_text,
        passage_text=passage_text,
        source_url="https://arxiv.org/html/2605.08853",
    )
    assert verdict == ClaimVerdict.SUPPORTED
    assert "Passage does not mention required technical entity 'IOI'" not in rationale

    ok, msg = _check_entities_present(claim_text, passage_text)
    assert ok is True, f"Entity checker should ground IOI but failed with: {msg}"


def test_phase2v2_benchmark_table_with_author_email_not_rejected_as_boilerplate(tmp_path):
    """Phase 2V.2 Issue 3: Substantive empirical tables containing numbers and metric units
    must not be discarded as boilerplate metadata merely because an author email appears in the document.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://github.com/saidineshpola/Indexes-comparision/blob/main/README.md"
    text = (
        "FAISS Indexes Comparison on SIFT1M\n"
        "This project benchmarks and compares popular FAISS indexing strategies using the standard SIFT1M dataset "
        "for approximate nearest neighbor search.\n"
        "All experiments, code, and results are inside the Jupyter Notebook. Open in Colab.\n"
        "Covered Index Types\n"
        "IndexFlatL2 - Brute-force baseline\n"
        "IndexLSH - Locality Sensitive Hashing\n"
        "IndexHNSWFlat - Hierarchical Navigable Small World graph\n"
        "IndexIVFFlat - Inverted File Index\n"
        "Metrics Compared: Build Time, Search Time, Recall@1, Queries Per Second (QPS), Memory Usage.\n"
        "Dataset: We use the standard SIFT1M dataset (1 million vectors, 128-D) which can be downloaded from IRISA ftp.\n"
        "Results Summary: Includes a performance table and conclusion section highlighting trade-offs across indexing methods. "
        "Ideal for understanding real-world performance of vector search indexes.\n"
        "Benchmark Summary (SIFT1M)\n"
        "Method | Build Time | Search Time | QPS | Recall@1 | Memory Usage\n"
        "IndexFlatL2 | 0.18 s | 21.07 s | 474 | 0.9914 | 488.28 MB\n"
        "IndexLSH | 4.65 s | 22.02 s | 454 | 0.4421 | 122.57 MB\n"
        "HNSW (ef=32) | 108.46 s | 0.426 s | 23,448 | 0.9553 | 991.88 MB\n"
        "IVFFlat (nprobe=16) | 18.40 s | 1.05 s | 9,532 | 0.8885 | 497.94 MB\n"
        "Read the Notebook.\n"
        "For questions or collaboration: saidines12@gmail.com\n"
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, text, title="FAISS Benchmark")))

    claim_query = "HNSW reaches 23,448 QPS with a strong recall@1 of 0.9553 on the SIFT1M dataset"
    passage, pid, rec = find_passage_for_url_and_query(url, claim_query, store)

    assert passage is not None, "Passage must not be None for benchmark query"
    assert "23,448" in passage, f"Passage should contain '23,448': {passage}"
    assert "0.9553" in passage, f"Passage should contain '0.9553': {passage}"


def test_phase2v2_newline_separated_bullet_list_passage_selection(tmp_path):
    """Phase 2V.2 Issue 4: Documents with single-newline bullet points without terminal periods
    must be cleanly segmented so findings at the end of the document are not dropped.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://github.com/ever-works/awesome-vector-databases/blob/HEAD/details/hnsw-if.md"
    text = (
        "Overview\n"
        "HNSW-IF is Vespa's cost-efficient hybrid method for approximate nearest neighbor search.\n"
        "Architecture\n"
        "Indexing Process\n"
        "Vectors that are not cluster centroids search for k closest centroids in HNSW graph\n"
        "Index closest centroid IDs using inverted indexes\n"
        "Inverted index dictionary maps centroid IDs to posting lists of non-centroid vector IDs\n"
        "Query Process\n"
        "Search centroid vectors using HNSW for k closest centroids\n"
        "Search inverted index using logical disjunction\n"
        "Performance Advantages\n"
        "Dramatic cost reduction for billion-scale deployments\n"
        "Scalable to billions of vectors\n"
        "Billion-scale vector search on budget"
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, text, title="HNSW-IF Details")))

    claim_query = "scaling efficiently to billions of vectors"
    passage, pid, rec = find_passage_for_url_and_query(url, claim_query, store)

    assert passage is not None, "Passage must not be None"
    assert "billions of vectors" in passage.lower(), f"Passage should contain 'billions of vectors': {passage}"


def test_phase2v2_negative_false_support_protections():
    """Phase 2V.2 Negative Tests: Ensure verifier protections against false support remain intact:
    1. Genuinely absent technical entity (e.g. 'Markov') must be rejected.
    2. Factually wrong metric (e.g. '99,999 QPS') must not match evidence stating '23,448 QPS'.
    3. Factual contradictions must remain CONTRADICTED.
    """
    # 1. Truly absent technical entity
    claim_absent = (
        "Speculative tree decoding extends to state-space models by exploiting accumulated transition matrices "
        "to manage explicit Markov states with minimal overhead."
    )
    passage_without_markov = (
        "We propose tree-based speculative decoding in state-space models (SSMs) by exploiting the structure "
        "of accumulated state transition matrices with minimal overhead."
    )
    ok, msg = _check_entities_present(claim_absent, passage_without_markov)
    assert ok is False, "Entity check must fail when 'Markov' is absent from passage"
    assert "Markov" in msg

    # 2. Factually wrong metric must not be supported
    mock_llm_metric = MagicMock()
    mock_llm_metric.call = MagicMock(return_value='{"verdict": "insufficient_evidence", "rationale": "Passage states 23,448 QPS, not 99,999 QPS."}')
    verifier_metric = ClaimVerifier(llm=mock_llm_metric, action_policy="label")

    claim_wrong_num = "HNSW achieves 99,999 QPS on SIFT1M."
    passage_num = "HNSW (ef=32) achieves 23,448 QPS on SIFT1M."
    verdict, rat = verifier_metric._verify_via_mesh_llm(claim_wrong_num, passage_num, "https://example.com/metric")
    assert verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "23,448" in rat or "99,999" in rat

    # 3. Direct contradiction must remain CONTRADICTED
    mock_llm_contra = MagicMock()
    mock_llm_contra.call = MagicMock(return_value='{"verdict": "contradicted", "rationale": "GPU CUDA is listed under future roadmap, not implemented."}')
    verifier_contra = ClaimVerifier(llm=mock_llm_contra, action_policy="label")

    claim_contra = "The system implements GPU-accelerated CUDA kernels."
    passage_contra = "GPU acceleration (CUDA) is listed under Future Optimization Directions."
    v_contra, r_contra = verifier_contra._verify_via_mesh_llm(claim_contra, passage_contra, "https://example.com/gpu")
    assert v_contra == ClaimVerdict.CONTRADICTED


def test_lsmvec_claim_does_not_inherit_spfresh_update_limitation(tmp_path):
    """A property attributed to SPFresh must not be transferred to LSM-VEC."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/pdf/2505.17152"
    passage = (
        "SPFresh employs in-place updates, limiting its efficiency in handling "
        "high-throughput insertions and deletions under dynamic workloads. "
        "This paper presents LSM-VEC, a disk-based dynamic vector index that "
        "integrates hierarchical graph indexing with LSM-tree storage. "
        "LSM-VEC supports out-of-place vector updates and reduces I/O without "
        "requiring global reconstruction."
    )
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, passage, title="LSM-VEC")
    ))

    claim = AtomicClaim(
        claim_id="lsmvec_attribution_regression",
        claim_text=(
            "LSM-VEC integrates hierarchical graph indexing with LSM-tree "
            "storage to support high-throughput insertions and deletions "
            "under dynamic workloads without global reconstruction"
        ),
        cited_urls=[url],
        source_url=url,
    )

    result = ClaimVerifier(action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert result.verdict != ClaimVerdict.SUPPORTED


def test_lsmvec_directly_supported_architecture_claim_remains_supported(tmp_path):
    """Positive Control 1: Valid assertions about LSM-VEC's graph indexing, out-of-place updates,
    and reduced I/O must remain SUPPORTED even when a competing system (SPFresh) is in the passage.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/pdf/2505.17152"
    passage = (
        "SPFresh employs in-place updates, limiting its efficiency in handling "
        "high-throughput insertions and deletions under dynamic workloads. "
        "This paper presents LSM-VEC, a disk-based dynamic vector index that "
        "integrates hierarchical graph indexing with LSM-tree storage. "
        "LSM-VEC supports out-of-place vector updates and reduces I/O without "
        "requiring global reconstruction."
    )
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, passage, title="LSM-VEC")
    ))

    claim = AtomicClaim(
        claim_id="lsmvec_valid_architecture",
        claim_text=(
            "LSM-VEC integrates hierarchical graph indexing with LSM-tree "
            "storage to support out-of-place vector updates and reduce I/O "
            "without requiring global reconstruction"
        ),
        cited_urls=[url],
        source_url=url,
    )

    result = ClaimVerifier(action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.SUPPORTED
    assert "reconstruction" in result.passage_text.lower()


def test_lsmvec_high_throughput_updates_supported_when_explicitly_attributed(tmp_path):
    """Positive Control 2: High-throughput update assertions must be accepted as SUPPORTED
    when the evidence passage explicitly attributes them to LSM-VEC.
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/pdf/2505.17152"
    passage = (
        "SPFresh employs in-place updates. "
        "In contrast, LSM-VEC supports high-throughput insertions and deletions "
        "under dynamic workloads without requiring global reconstruction."
    )
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, passage, title="LSM-VEC Attributed")
    ))

    claim = AtomicClaim(
        claim_id="lsmvec_explicitly_attributed",
        claim_text=(
            "LSM-VEC supports high-throughput insertions and deletions "
            "under dynamic workloads without requiring global reconstruction"
        ),
        cited_urls=[url],
        source_url=url,
    )

    result = ClaimVerifier(action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.SUPPORTED


def test_attribution_comparative_claim_referencing_both_systems_supported(tmp_path):
    """Claims explicitly comparing both named systems are not falsely flagged as attribution errors."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.org/pdf/2505.17152"
    passage = (
        "SPFresh employs in-place updates, limiting its efficiency. "
        "In contrast, LSM-VEC achieves lower I/O latency than SPFresh by "
        "utilizing out-of-place vector updates without global reconstruction."
    )
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, passage, title="LSM-VEC Comparison")
    ))

    claim = AtomicClaim(
        claim_id="lsmvec_comparison",
        claim_text=(
            "LSM-VEC achieves lower I/O latency than SPFresh by "
            "utilizing out-of-place vector updates without global reconstruction"
        ),
        cited_urls=[url],
        source_url=url,
    )

    result = ClaimVerifier(action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.SUPPORTED


def test_attribution_guard_in_llm_backed_verification():
    """Ensure that if an LLM incorrectly reports support for a misattributed claim,
    the attribution guard intercepts and converts the verdict to INSUFFICIENT_EVIDENCE.
    """
    mock_llm = MagicMock()
    mock_llm.call = MagicMock(return_value='{"verdict": "supported", "rationale": "All words appear in passage."}')
    verifier = ClaimVerifier(llm=mock_llm, action_policy="label")

    passage = (
        "SPFresh employs in-place updates, limiting its efficiency in handling "
        "high-throughput insertions and deletions under dynamic workloads. "
        "This paper presents LSM-VEC, a disk-based dynamic vector index that "
        "integrates hierarchical graph indexing with LSM-tree storage."
    )
    claim = (
        "LSM-VEC integrates hierarchical graph indexing with LSM-tree "
        "storage to support high-throughput insertions and deletions "
        "under dynamic workloads"
    )

    verdict, rationale = verifier._verify_via_mesh_llm(claim, passage, "https://arxiv.org/pdf/2505.17152")

    assert verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "SPFresh" in rationale or "attributed" in rationale


# ---------------------------------------------------------------------------
# Phase 2V.4: bold heading extraction and heading-aware clause decomposition
# ---------------------------------------------------------------------------

def test_phase2v4_bold_heading_colon_inside_stripped_from_extracted_claim():
    """A colon inside a bold heading ('**Title:** Statement') must be stripped from claims."""
    report = (
        "# Key Insights\n\n"
        "- **HNSW (Hierarchical Navigable Small World):** Operates as an in-memory "
        "graph-structured index that enables approximate nearest-neighbor search in "
        "high-dimensional spaces, resembling a multi-layer skip list [6].\n"
    )
    claims = extract_atomic_claims(report, {"6": "https://arxiv.example/abs/1603.09320"})

    assert len(claims) == 1
    assert claims[0].claim_text == (
        "Operates as an in-memory graph-structured index that enables approximate "
        "nearest-neighbor search in high-dimensional spaces, resembling a multi-layer skip list"
    )


def test_phase2v4_bold_heading_colon_inside_stripped_from_numbered_claim():
    """Numbered bold headings with an internal colon ('2. **Title:** Statement') must be stripped."""
    report = (
        "# Key Insights\n\n"
        "2. **Quantization and Re-Rank Trade-Offs:** Quiver-style quantization mechanisms "
        "utilize compact codes to approximate distances, then re-rank a shortlist with "
        "full-precision distances [4].\n"
    )
    claims = extract_atomic_claims(report, {"4": "https://arxiv.example/abs/2407.00004"})

    assert len(claims) == 1
    assert claims[0].claim_text == (
        "Quiver-style quantization mechanisms utilize compact codes to approximate "
        "distances, then re-rank a shortlist with full-precision distances"
    )


def test_phase2v4_existing_title_prefix_formats_still_stripped():
    """Previously supported title-prefix formats remain stripped after the fix."""
    cases = [
        (
            "- **DiskANN**: A graph-based index achieves high recall with low latency [6].",
            "A graph-based index achieves high recall with low latency",
        ),
        (
            "1. **Quiver** - Quantization mechanisms accelerate retrieval with compact codes [6].",
            "Quantization mechanisms accelerate retrieval with compact codes",
        ),
        (
            "*Caliby*: A vector store uses hierarchical navigable small world graphs [6].",
            "A vector store uses hierarchical navigable small world graphs",
        ),
    ]
    for line, expected in cases:
        claims = extract_atomic_claims(
            "# Key Insights\n\n" + line + "\n",
            {"6": "https://arxiv.example/abs/2407.00006"},
        )
        assert len(claims) == 1, line
        assert claims[0].claim_text == expected, line


def test_phase2v4_decompose_does_not_split_heading_fragment_into_clause():
    """A heading fragment glued to a statement must not be emitted as a standalone conjoined-subject clause."""
    text = (
        "Quantization and Re-Rank Trade-Offs: Quiver-style quantization mechanisms "
        "utilize compact codes to approximate distances, then re-rank a shortlist "
        "with full-precision distances"
    )
    clauses = _decompose_claim_clauses(text)

    assert clauses == [text]


def test_phase2v4_decompose_still_splits_genuine_conjoined_subjects():
    """Genuine conjoined-subject claims remain decomposed into per-subject propositions."""
    text = "HNSW and DiskANN utilize graph structures to accelerate similarity search"
    clauses = _decompose_claim_clauses(text)

    assert clauses == [
        "HNSW utilize graph structures to accelerate similarity search.",
        "DiskANN utilize graph structures to accelerate similarity search.",
    ]


def test_phase2v4_compound_claim_partially_supported_is_insufficient(tmp_path):
    """A genuinely compound claim is INSUFFICIENT when any clause lacks support."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.00010"
    passage = (
        "HNSW provides logarithmic search complexity for high-dimensional data. "
        "The index maintains a multi-layer graph."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage)))

    claim = AtomicClaim(
        claim_id="phase2v4_compound_partial",
        claim_text=(
            "HNSW provides logarithmic search complexity; "
            "DiskANN reduces memory footprint through quantization"
        ),
        cited_urls=[url],
        source_url=url,
    )

    def fake_judge(prompt: str) -> str:
        if "HNSW provides logarithmic search complexity" in prompt:
            return '{"verdict": "supported", "rationale": "Passage states HNSW logarithmic complexity."}'
        return '{"verdict": "insufficient_evidence", "rationale": "Passage does not mention DiskANN memory footprint."}'

    llm = MagicMock()
    llm.call = MagicMock(side_effect=fake_judge)
    result = ClaimVerifier(llm=llm, action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "unsupported" in result.rationale.lower()


def test_phase2v4_compound_claim_fully_supported_when_all_clauses_supported(tmp_path):
    """A genuinely compound claim remains SUPPORTED when every clause is supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.00011"
    passage = (
        "HNSW provides logarithmic search complexity for high-dimensional data. "
        "DiskANN reduces memory footprint through quantization of graph edges."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage)))

    claim = AtomicClaim(
        claim_id="phase2v4_compound_full",
        claim_text=(
            "HNSW provides logarithmic search complexity; "
            "DiskANN reduces memory footprint through quantization"
        ),
        cited_urls=[url],
        source_url=url,
    )

    def fake_judge(prompt: str) -> str:
        return '{"verdict": "supported", "rationale": "Passage states the asserted clause."}'

    llm = MagicMock()
    llm.call = MagicMock(side_effect=fake_judge)
    result = ClaimVerifier(llm=llm, action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.SUPPORTED
    assert "fully supported" in result.rationale.lower()


def test_phase2v4_quiver_claim_supported_after_heading_extraction(tmp_path):
    """Heading-glued Quiver-style claims are extracted cleanly and verify as SUPPORTED."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.00012"
    passage = (
        "Quiver-style quantization mechanisms utilize compact codes to approximate "
        "distances, then re-rank a shortlist of k x rerank_factor candidates with "
        "full-precision distances."
    )
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, passage, title="Quiver Re-Rank Notes")
    ))

    report = (
        "# Key Insights\n\n"
        "2. **Quantization and Re-Rank Trade-Offs:** Quiver-style quantization mechanisms "
        "utilize compact codes to approximate distances, then re-rank a shortlist with "
        "full-precision distances [4].\n"
    )
    claims = extract_atomic_claims(report, {"4": url})

    assert len(claims) == 1
    claim = claims[0]
    assert "Trade-Offs" not in claim.claim_text
    assert claim.claim_text.startswith("Quiver-style quantization mechanisms")

    seen_prompts = []

    def fake_judge(prompt: str) -> str:
        seen_prompts.append(prompt)
        return '{"verdict": "supported", "rationale": "Passage states the Quiver re-rank flow."}'

    llm = MagicMock()
    llm.call = MagicMock(side_effect=fake_judge)
    result = ClaimVerifier(llm=llm, action_policy="label").verify_claim(claim, store)

    assert result.verdict == ClaimVerdict.SUPPORTED
    assert all("Trade-Offs" not in p for p in seen_prompts)


# ===========================================================================
# Task 11 — Identifier Underscore Preservation & Compound Provenance Retention
# ===========================================================================

def test_task11_clean_claim_preserves_quantization_identifiers():
    """Identifier underscores (Q4_K_M, Q8_0, n_ctx) survive claim cleaning."""
    cleaned = _clean_claim_statement(
        "The `AVA-v2-Q4_K_M` file and the Q5_K_M, Q8_0, and n_ctx settings are compared **here**."
    )

    assert "Q4_K_M" in cleaned
    assert "Q5_K_M" in cleaned
    assert "Q8_0" in cleaned
    assert "n_ctx" in cleaned
    assert "Q4KM" not in cleaned
    assert "Q80" not in cleaned
    assert "`" not in cleaned
    assert "*" not in cleaned


def test_task11_clean_claim_still_strips_markdown_underscore_emphasis():
    """Paired markdown underscore emphasis is still removed by claim cleaning."""
    cleaned = _clean_claim_statement("This _emphasized_ word and __bold__ text remain intact.")

    assert cleaned == "This emphasized word and bold text remain intact"
    assert "_" not in cleaned


def test_task11_extract_atomic_claims_preserves_q4km_identifiers():
    """Extracted claim text retains quantization identifiers for citation tracing."""
    report = (
        "# Quantization Trade-Offs\n\n"
        "An `AVA-v2-Q4_K_M` file is 1.19 GB while the Q5_K_M variant is 1.31 GB on disk [1].\n\n"
        "## Sources\n\n"
        "[1] https://huggingface.example/NAME0x0/AVA-v2-GGUF\n"
    )

    claims = extract_atomic_claims(report)

    assert len(claims) == 1
    assert "Q4_K_M" in claims[0].claim_text
    assert "Q5_K_M" in claims[0].claim_text
    assert "`" not in claims[0].claim_text


def test_task11_compound_insufficient_retains_evaluated_clause_passage(tmp_path):
    """A compound-insufficient record keeps the passage and rationale evaluated for the unsupported clause."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2408.00001"
    passage = (
        "Method A reduces inference latency by caching intermediate projections. "
        "Method B stabilizes gradient updates on clean datasets, but no benchmark numbers are reported."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage)))

    claim = AtomicClaim(
        claim_id="task11_compound_provenance",
        claim_text="Method A reduces inference latency, while Method B stabilizes gradient updates",
        cited_urls=[url],
        source_url=url,
        constituent_clauses=[
            "Method A reduces inference latency",
            "while Method B stabilizes gradient updates",
        ],
    )

    def mock_eval(eval_text, passage_text, src_url):
        if "Method B" in eval_text and "no benchmark numbers" in passage_text:
            return (
                ClaimVerdict.INSUFFICIENT_EVIDENCE,
                "No benchmark data substantiates the Method B stabilization claim.",
            )
        if "Method A" in eval_text and "Method B" not in eval_text:
            return ClaimVerdict.SUPPORTED, "Passage states Method A caching reduces latency."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Passage does not jointly establish both clauses."

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "Compound claim is partially unsupported" in rec.rationale
    assert rec.passage_id is not None
    assert rec.passage_text is not None
    assert "Method B stabilizes gradient updates" in rec.passage_text
    assert "No benchmark data substantiates" in rec.rationale


def test_task11_multi_source_insufficient_retains_combined_evidence(tmp_path):
    """A multi-source insufficient record keeps the combined passage and judge rationale that were evaluated."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://arxiv.example/abs/2408.00002"
    url2 = "https://arxiv.example/abs/2408.00003"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1, "The unified deployment pipeline improves alignment robustness across evaluation suites."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2, "A unified deployment pipeline records alignment robustness improvements across evaluation suites."
    )))

    claim = AtomicClaim(
        claim_id="task11_multi_source_provenance",
        claim_text="The unified deployment pipeline improves alignment robustness across evaluation suites",
        cited_urls=[url1, url2],
        source_url=url1,
    )

    def mock_eval(eval_text, passage_text, src_url):
        return (
            ClaimVerdict.INSUFFICIENT_EVIDENCE,
            "Passages describe the pipeline but do not quantify robustness improvements.",
        )

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "None of the 2 cited sources" in rec.rationale
    assert "do not quantify robustness improvements" in rec.rationale
    assert rec.passage_id is not None
    assert rec.passage_text is not None
    assert f"[{url1}]" in rec.passage_text
    assert f"[{url2}]" in rec.passage_text


def test_task11_multi_source_insufficient_without_evaluated_passage_keeps_none_provenance(tmp_path):
    """No passage evaluated means no provenance is fabricated on the insufficient record."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url1 = "https://recipes.example/sourdough"
    url2 = "https://recipes.example/baguette"
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url1, "Mix flour, water, salt, and active sourdough starter, then ferment for hours."
    )))
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(
        url2, "Shape the dough, proof it, and bake at high temperature until golden."
    )))

    claim = AtomicClaim(
        claim_id="task11_multi_source_no_passage",
        claim_text="The unified deployment pipeline improves alignment robustness across evaluation suites.",
        cited_urls=[url1, url2],
        source_url=url1,
    )

    verifier = ClaimVerifier(action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert rec.passage_id is None
    assert rec.passage_text is None


# ===========================================================================
# Task 12 — Parenthetical-Safe Compound-Claim Splitting
# ===========================================================================

def test_task12_decompose_does_not_split_inside_parenthetical_identifier_list():
    """Claim-010 regression: 'and' between Q4_K_M/Q5_K_M inside parentheses must not split the clause."""
    text = "GGUF format quantization levels (such as Q4_K_M and Q5_K_M) allow deployment across diverse hardware profiles."
    clauses = _decompose_claim_clauses(text)

    assert len(clauses) == 2
    for clause in clauses:
        assert clause.count("(") == clause.count(")"), clause
        assert "allow deployment across diverse hardware profiles" in clause, clause
        assert " and " not in clause, clause
    assert "Q4_K_M" in clauses[0]
    assert "Q5_K_M" in clauses[1]


def test_task12_comma_and_clause_inside_parentheses_not_split():
    """A ', and' clause boundary must never be created inside balanced parentheses."""
    text = (
        "Profiling shows caching wins when the cache stays warm "
        "(the resident set fits, and the reuse distance is short) across workloads."
    )
    clauses = _decompose_claim_clauses(text)

    assert all(clause.count("(") == clause.count(")") for clause in clauses), clauses
    joined = " ".join(clauses)
    assert "the resident set fits" in joined
    assert "the reuse distance is short" in joined


def test_task12_contrast_conjunction_inside_parentheses_not_split():
    """A ', while' contrast boundary must never be created inside balanced parentheses."""
    text = "The engine holds weights resident (small quants stay fast, while full precision streams) under low VRAM."
    clauses = _decompose_claim_clauses(text)

    assert len(clauses) == 1
    assert clauses[0].count("(") == clauses[0].count(")") == 1
    assert clauses[0].startswith("The engine holds weights resident (small quants stay fast")


def test_task12_top_level_compound_clauses_still_split():
    """Parenthetical safety must not disable normal top-level clause splitting."""
    contrast = _decompose_claim_clauses(
        "The cache stays warm (when the lap is short) while the loader prefetches weights."
    )
    assert len(contrast) == 2
    assert contrast[0].count("(") == contrast[0].count(")") == 1
    assert "loader prefetches" in contrast[1]

    finite_verbs = _decompose_claim_clauses(
        "The retriever uses multiple sources, and it also provides relevance scores."
    )
    assert len(finite_verbs) == 2
    assert "uses multiple sources" in finite_verbs[0]
    assert "provides relevance scores" in finite_verbs[1]


def test_task12_nested_and_unbalanced_parentheses_safe():
    """Nested and unbalanced parentheses are handled without crashing or dropping conjunction text."""
    nested = "Compression tables show trade-offs (level 1 (fast) versus level 9 (compact)) for archival workloads."
    assert _decompose_claim_clauses(nested) == [nested]

    unmatched_open = "The model loads (weights and metadata in a single file."
    result = _decompose_claim_clauses(unmatched_open)
    assert len(result) == 1
    assert " and " in result[0]


def test_task12_sentence_clause_split_retains_parenthetical_conjunction():
    """Intra-sentence citation splitting must not drop a conjunction that sits inside parentheses."""
    sentence = (
        "We contrast retention modes (resident vectors cached [1], "
        "while swapped pages stream from disk [2]) under load."
    )
    splits = _split_sentence_clauses(sentence, {})
    texts = [t for t, _ in splits]

    assert len(texts) == 2
    assert texts[0].startswith("We contrast retention modes (resident vectors cached")
    assert "while swapped pages stream from disk" in texts[1]


def test_task12_verify_claim_parenthetical_claim_confined_to_balanced_clauses(tmp_path):
    """verify_claim must evaluate identifier-balanced clauses for the claim-010 sentence shape."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2409.00001"
    passage = (
        "GGUF format quantization levels (such as Q4_K_M and Q5_K_M) allow deployment across "
        "diverse hardware profiles. VRAM footprints scale with the chosen tier."
    )
    store.add(EvidenceRecord.from_tool_result(ToolFetchResult.success(url, passage)))

    claim = AtomicClaim(
        claim_id="task12_paren_claim",
        claim_text="GGUF format quantization levels (such as Q4_K_M and Q5_K_M) allow deployment across diverse hardware profiles",
        cited_urls=[url],
        source_url=url,
    )

    def mock_eval(eval_text, passage_text, src_url):
        if "(such as Q4_K_M)" in eval_text:
            return ClaimVerdict.SUPPORTED, "Passage directly states Q4_K_M deployment across hardware profiles."
        if "(such as Q5_K_M)" in eval_text:
            return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Passage does not quantify Q5_K_M deployment tiers."
        return ClaimVerdict.INSUFFICIENT_EVIDENCE, "Passage does not resolve the quantization tier list."

    verifier = ClaimVerifier(verifier_fn=mock_eval, action_policy="label")
    rec = verifier.verify_claim(claim, store)

    assert rec.verdict == ClaimVerdict.INSUFFICIENT_EVIDENCE
    assert "Compound claim is partially unsupported" in rec.rationale
    assert "(such as Q4_K_M allow" not in rec.rationale
    assert "'Q5_K_M) allow" not in rec.rationale
    assert rec.passage_id is not None
    assert rec.passage_text is not None

