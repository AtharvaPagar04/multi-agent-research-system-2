"""
test_report_audit.py
--------------------
Unit tests for deterministic evidence report citation auditing:
- Auditing reports with verified full_text and snippet_only sources
- Flagging unrecorded / hallucinated URLs
- Flagging thin evidence (insufficient sources or zero full-text)
- Flagging citations of failed retrievals
- Verifying boundary: programmatic audit checks provenance, not semantic factual truth
"""

from research_crew.evidence.model import EvidenceRecord, RetrievalStatus
from research_crew.evidence.store import EvidenceStore
from research_crew.evidence.validator import (
    ReportAuditSummary,
    audit_report_citations,
    audit_report_metrics,
    check_report_foundations,
)
from research_crew.tools.tool_result import ToolFetchResult


def test_audit_report_citations_matching_all_sources(tmp_path):
    """Report citing valid store URLs matches counts accurately."""
    store = EvidenceStore.create(output_dir=tmp_path)

    # Add 2 full_text and 1 snippet_only sources
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success("https://arxiv.org/abs/1706.03762", "Vaswani 2017 text", title="Vaswani")
    )
    r2 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success("https://arxiv.org/abs/2205.14135", "Dao 2022 FlashAttention", title="Dao")
    )
    r3 = EvidenceRecord.from_tool_result(
        ToolFetchResult.snippet("https://pytorch.org/docs/stable/nn.html", "PyTorch nn docs snippet", title="PyTorch Docs")
    )
    store.add(r1)
    store.add(r2)
    store.add(r3)

    report_text = (
        "# Research Summary: Transformer Attention\n\n"
        "1. Attention mechanisms were introduced in 2017 [1].\n"
        "   *Source: https://arxiv.org/abs/1706.03762*\n\n"
        "2. FlashAttention speeds up exact attention [2].\n"
        "   *Source: https://arxiv.org/abs/2205.14135*\n\n"
        "3. Core implementations are available in PyTorch [3].\n"
        "   *Source: https://pytorch.org/docs/stable/nn.html*\n"
    )

    audit = audit_report_citations(report_text, store, min_sources=3)

    assert isinstance(audit, ReportAuditSummary)
    assert audit.total_citations_found == 3
    assert audit.matched_sources_count == 3
    assert audit.unmatched_sources_count == 0
    assert audit.full_text_sources_count == 2
    assert audit.snippet_only_sources_count == 1
    assert audit.failed_sources_count == 0
    assert audit.is_thin_evidence is False
    assert len(audit.warnings) == 0


def test_audit_report_citations_flags_unmatched_url(tmp_path):
    """Report citing an unrecorded URL is detected and flagged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success("https://arxiv.org/abs/1706.03762", "Vaswani text")
    )
    store.add(r1)

    report_text = (
        "Foundational work is at https://arxiv.org/abs/1706.03762, "
        "while an unrecorded blog was cited at https://random-blog.com/attention."
    )

    audit = audit_report_citations(report_text, store, min_sources=1)

    assert audit.total_citations_found == 2
    assert audit.matched_sources_count == 1
    assert audit.unmatched_sources_count == 1
    assert "https://random-blog.com/attention" in audit.unmatched_urls
    assert any("was not found in the run's evidence store" in w for w in audit.warnings)

def test_numeric_normalization_handles_thousands_separators():
    from research_crew.evidence.validator import _normalize_numeric_tokens

    assert _normalize_numeric_tokens("~20,000 hours") == ["20000"]
    assert _normalize_numeric_tokens("20 000 hours") == ["20000"]
    assert _normalize_numeric_tokens("61.4%") == ["61.4"]
    assert _normalize_numeric_tokens("512x speedup") == ["512"]
    assert _normalize_numeric_tokens("100ms latency") == ["100"]
    assert _normalize_numeric_tokens("50GB memory") == ["50"]
    assert _normalize_numeric_tokens("+15% boost") == ["15"]


def test_numeric_token_is_present_prevents_accidental_substring_matches():
    from research_crew.evidence.validator import _numeric_token_is_present

    # '20' must not match '20,000' or '20000'
    assert _numeric_token_is_present("20", "Model trained on approximately 20,000 hours.") is False
    assert _numeric_token_is_present("20", "Model trained on approximately 20000 hours.") is False
    assert _numeric_token_is_present("20", "Model trained on approximately 20 000 hours.") is False

    # '20000' must match '20,000', '20000', or '20 000'
    assert _numeric_token_is_present("20000", "Model trained on approximately 20,000 hours.") is True
    assert _numeric_token_is_present("20000", "Model trained on approximately 20000 hours.") is True
    assert _numeric_token_is_present("20000", "Model trained on approximately 20 000 hours.") is True

    # '512' must match '512 GPUs' or '512x'
    assert _numeric_token_is_present("512", "Trained on 512 GPUs.") is True
    assert _numeric_token_is_present("512", "Achieved 512x speedup.") is True

    # '99.9' must not match '61.4%'
    assert _numeric_token_is_present("99.9", "Achieved 61.4% accuracy.") is False


def test_audit_report_metrics_accepts_formatted_thousands(tmp_path):
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/tts",
                "MahaTTS-v2 was trained on approximately 20,000 hours of speech.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Training Scale | ~20,000 hours | Training dataset size; [Baseline unspecified in source text] | https://example.com/tts |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.untraceable_metrics_count == 0
def test_audit_report_citations_flags_thin_evidence(tmp_path):
    """Reports with fewer than min_sources or zero full-text sources are flagged as thin."""
    store = EvidenceStore.create(output_dir=tmp_path)
    # Only snippet sources
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.snippet("https://arxiv.org/abs/1706.03762", "snippet 1")
    )
    store.add(r1)

    report_text = "See https://arxiv.org/abs/1706.03762 for brief info."
    audit = audit_report_citations(report_text, store, min_sources=3)

    assert audit.is_thin_evidence is True
    assert any("Evidence coverage is thin" in w for w in audit.warnings)


def test_audit_report_citations_flags_failed_retrieval_citation(tmp_path):
    """Citations pointing to FAILED retrieval records trigger a specific warning."""
    store = EvidenceStore.create(output_dir=tmp_path)
    r_failed = EvidenceRecord.from_tool_result(
        ToolFetchResult.failure("https://broken-site.com/paper.pdf", error_message="404 Not Found")
    )
    store.add(r_failed)

    report_text = "See paper at https://broken-site.com/paper.pdf."
    audit = audit_report_citations(report_text, store, min_sources=1)

    assert audit.failed_sources_count == 1
    assert any("FAILED retrieval status" in w for w in audit.warnings)


def test_check_report_foundations():
    """Foundational mechanism section detection."""
    report_with_foundations = (
        "# Research Summary: Transformer Attention\n\n"
        "## Core Foundations & Mechanism\n"
        "Scaled dot-product attention computes softmax(QK^T / sqrt(d_k))V [1].\n"
    )
    assert check_report_foundations(report_with_foundations) is True

    report_without_foundations = (
        "# Research Summary: Transformer Attention\n\n"
        "## Key Insights\n"
        "FlashAttention is fast [1].\n"
    )
    assert check_report_foundations(report_without_foundations) is False


def test_audit_report_metrics_traceable_and_baseline_complete(tmp_path):
    """Quantitative metrics matching source text with complete baselines pass without warnings."""
    store = EvidenceStore.create(output_dir=tmp_path)
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            "https://arxiv.org/abs/2205.14135",
            "FlashAttention yields a 2.4x speedup on LRA benchmarks and 0.7 better perplexity on GPT-2.",
        )
    )
    store.add(r1)

    report = (
        "# Research Summary: Transformer Attention\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup          | 2.4x  | vs standard self-attention    | https://arxiv.org/abs/2205.14135 |\n"
        "| GPT-2 Perplexity     | 0.7   | vs baseline PyTorch attention | [1] |\n\n"
        "## Sources\n"
        "[1] FlashAttention paper\n"
        "    https://arxiv.org/abs/2205.14135\n"
    )

    metrics_audit = audit_report_metrics(report, store)
    assert metrics_audit.total_metrics_found == 2
    assert metrics_audit.traceable_metrics_count == 2
    assert metrics_audit.untraceable_metrics_count == 0
    assert metrics_audit.missing_baseline_count == 0
    assert len(metrics_audit.warnings) == 0


def test_audit_report_metrics_flags_missing_baseline(tmp_path):
    """Metrics rows missing comparison baseline or context trigger a warning."""
    store = EvidenceStore.create(output_dir=tmp_path)
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success("https://arxiv.org/abs/2205.14135", "Achieves 2.4x speedup.")
    )
    store.add(r1)

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup          | 2.4x  | -                             | https://arxiv.org/abs/2205.14135 |\n"
    )

    metrics_audit = audit_report_metrics(report, store)
    assert metrics_audit.total_metrics_found == 1
    assert metrics_audit.missing_baseline_count == 1
    assert any("Comparison baseline is missing or empty" in w for w in metrics_audit.warnings)


def test_audit_report_metrics_discloses_unspecified_baseline(tmp_path):
    """An explicit disclosure is distinct from a verified baseline and remains visible."""
    store = EvidenceStore.create(output_dir=tmp_path)
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success("https://arxiv.org/abs/2205.14135", "Reported 2.4x speedup.")
    )
    store.add(r1)

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup          | 2.4x  | [Baseline unspecified in source text] | https://arxiv.org/abs/2205.14135 |\n"
    )

    metrics_audit = audit_report_metrics(report, store)
    assert metrics_audit.total_metrics_found == 1
    assert metrics_audit.missing_baseline_count == 0
    assert metrics_audit.specified_baseline_count == 0
    assert metrics_audit.explicitly_unspecified_baseline_count == 1
    assert metrics_audit.traceable_metrics_count == 1
    assert metrics_audit.rows[0].baseline_status == "explicitly_unspecified"
    assert any("explicitly unspecified" in w for w in metrics_audit.warnings)


def test_audit_report_metrics_flags_untraceable_number(tmp_path):
    """Metric values whose numerical quantities do not occur in the source are flagged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    r1 = EvidenceRecord.from_tool_result(
        ToolFetchResult.success("https://arxiv.org/abs/2205.14135", "Achieves 61.4% accuracy.")
    )
    store.add(r1)

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Accuracy             | 99.9% | vs prior SOTA                 | https://arxiv.org/abs/2205.14135 |\n"
    )

    metrics_audit = audit_report_metrics(report, store)
    assert metrics_audit.total_metrics_found == 1
    assert metrics_audit.traceable_metrics_count == 0
    assert metrics_audit.untraceable_metrics_count == 1
    assert any("not found in captured text" in w for w in metrics_audit.warnings)


def test_synthesizer_instructions_enforce_foundations_and_metric_requirements():
    """Verify synthesis task prompt and agent instructions enforce core foundations and metric baseline requirements."""
    from crewai import Agent, Task, LLM
    from research_crew.tasks.summary_task import build_summary_task
    from research_crew.agents.synthesizer_agent import build_synthesizer_agent

    dummy_llm = LLM(model="gpt-4o", api_key="fake")
    synthesizer_agent = build_synthesizer_agent(dummy_llm)
    dummy_prev_task = Task(description="Prev extraction", expected_output="[]", agent=synthesizer_agent)

    task = build_summary_task(synthesizer_agent, dummy_prev_task, topic="Explain transformer attention")

    # Task prompt requirements
    assert "Core Foundations & Mechanism" in task.description
    assert "Query, Key, and Value" in task.description
    assert "Scaled Dot-Product Attention" in task.description
    assert "Softmax Normalization" in task.description
    assert "Multi-Head Attention" in task.description
    assert "Context & Comparison Baseline" in task.description
    assert "[Baseline unspecified in source text]" in task.description

    # Agent goal & backstory requirements
    assert "Core Foundations & Mechanism" in synthesizer_agent.goal
    assert "Query/Key/Value" in synthesizer_agent.goal
    assert "comparison baseline" in synthesizer_agent.goal
    assert "foundational clarity" in synthesizer_agent.backstory


# ---------------------------------------------------------------------------
# Adversarial Offline Report-Audit Tests
# ---------------------------------------------------------------------------

def test_adversarial_report_with_fabricated_numerical_values(tmp_path):
    """Adversarial case 1: Fabricated numerical value not in source is detected."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/2205.14135",
                "FlashAttention yields a 2.4x speedup on LRA benchmarks.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Core Foundations & Mechanism\n"
        "Mechanism overview [1].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup          | 99.4x | vs standard attention         | https://arxiv.org/abs/2205.14135 |\n\n"
        "## Sources\n"
        "[1] https://arxiv.org/abs/2205.14135\n"
    )

    audit = audit_report_citations(report, store)
    assert audit.metrics_summary is not None
    assert audit.metrics_summary.untraceable_metrics_count == 1
    assert audit.metrics_summary.traceable_metrics_count == 0
    assert any("99.4" in w for w in audit.metrics_summary.warnings)


def test_adversarial_heuristic_limitation_number_present_but_different_metric(tmp_path):
    """Adversarial case 2: Documents the heuristic boundary.

    When a number ('512') exists in the source text in an unrelated context (512 GPUs),
    deterministic token matching confirms substring occurrence and marks it traceable.
    It does NOT evaluate semantic alignment (GPU count vs speedup multiplier).
    """
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/1909.08053",
                "Megatron-LM was trained across 512 GPUs with 8.3 billion parameters.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Core Foundations & Mechanism\n"
        "Model parallelism overview [1].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value        | Context & Comparison Baseline | Source |\n"
        "|----------------------|--------------|-------------------------------|--------|\n"
        "| Training Speedup     | 512x speedup | vs single GPU baseline        | https://arxiv.org/abs/1909.08053 |\n\n"
        "## Sources\n"
        "[1] https://arxiv.org/abs/1909.08053\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    # HEURISTIC BOUNDARY: Substring token check matches '512' in source text
    row = audit.rows[0]
    assert row.is_traceable is True
    # Audit does not claim semantic entailment or fact-checking
    # Relevance heuristic detects hardware count vs speedup mismatch
    assert row.relevance_status == "possible_mismatch"
    assert audit.possible_mismatch_count == 1
    assert any("mismatch" in w.lower() for w in row.warnings)


def test_adversarial_citation_url_not_in_evidence_store(tmp_path):
    """Adversarial case 3: Unrecorded / fabricated citation URL is detected and flagged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success("https://arxiv.org/abs/1706.03762", "Legitimate paper text.")
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Core Foundations & Mechanism\n"
        "Transformer attention was introduced at https://arxiv.org/abs/1706.03762.\n\n"
        "## Key Insights\n"
        "A fabricated claim cites https://fabricated-domain.org/fake-paper.pdf.\n"
    )

    audit = audit_report_citations(report, store, min_sources=1)
    assert audit.unmatched_sources_count == 1
    assert "https://fabricated-domain.org/fake-paper.pdf" in audit.unmatched_urls
    assert any("was not found in the run's evidence store" in w for w in audit.warnings)


def test_adversarial_metric_with_no_baseline_or_ambiguity_flag(tmp_path):
    """Adversarial case 4: Metrics lacking comparison baselines and ambiguity flags are flagged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success("https://arxiv.org/abs/2205.14135", "Perplexity gain of 0.7.")
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Perplexity gain      | 0.7   |                               | https://arxiv.org/abs/2205.14135 |\n"
        "| Speedup              | 2.4x  | -                             | https://arxiv.org/abs/2205.14135 |\n"
    )

    metrics_audit = audit_report_metrics(report, store)
    assert metrics_audit.total_metrics_found == 2
    assert metrics_audit.missing_baseline_count == 2
    assert any("Comparison baseline is missing or empty" in w for w in metrics_audit.warnings)


def test_adversarial_missing_required_foundations_section():
    """Adversarial case 5: Report missing foundational mechanism section is detected."""
    report_missing_foundations = (
        "# Research Summary: Transformer Attention\n\n"
        "## Key Insights\n"
        "1. FlashAttention accelerates exact attention [1].\n\n"
        "## Methodology Overview\n"
        "IO-aware tiling optimizes GPU SRAM [1].\n"
    )

    assert check_report_foundations(report_missing_foundations) is False


def test_valid_report_end_to_end_audit_passes(tmp_path):
    """Adversarial case 6: Fully compliant report passes all deterministic audit checks."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/1706.03762",
                "Self-attention uses Query, Key, Value vectors with scaled dot-product attention.",
            )
        )
    )
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/2205.14135",
                "FlashAttention achieves 2.4x speedup on LRA benchmarks.",
            )
        )
    )
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/2212.14052",
                "Hybrid H3 model shows 1.0 PPL improvement.",
            )
        )
    )

    valid_report = (
        "# Research Summary: Explain transformer attention\n\n"
        "## Core Foundations & Mechanism\n"
        "Transformer attention relies on Query, Key, and Value vectors [1]. "
        "Scaled dot-product attention computes softmax(QK^T / sqrt(d_k))V [1]. "
        "Multi-head attention projects representations into parallel subspaces [1].\n\n"
        "## Key Insights\n"
        "1. **Exact Hardware Optimization**\n"
        "   FlashAttention speeds up attention via IO-aware tiling [2].\n"
        "   *Source: [2]*\n\n"
        "2. **Alternative State Space Hybrids**\n"
        "   H3 hybrid models improve token recall [3].\n"
        "   *Source: [3]*\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value   | Context & Comparison Baseline          | Source |\n"
        "|----------------------|---------|----------------------------------------|--------|\n"
        "| LRA Speedup          | 2.4x    | vs standard PyTorch self-attention     | [2]    |\n"
        "| Hybrid Model PPL     | 1.0 PPL | [Baseline unspecified in source text]  | [3]    |\n\n"
        "## Sources\n"
        "[1] Attention Is All You Need\n"
        "    https://arxiv.org/abs/1706.03762\n\n"
        "[2] FlashAttention\n"
        "    https://arxiv.org/abs/2205.14135\n\n"
        "[3] Hungry Hungry Hippos\n"
        "    https://arxiv.org/abs/2212.14052\n"
    )

    audit = audit_report_citations(valid_report, store, min_sources=3)

    assert audit.has_foundations_section is True
    assert audit.total_citations_found == 3
    assert audit.matched_sources_count == 3
    assert audit.unmatched_sources_count == 0
    assert audit.full_text_sources_count == 3
    assert audit.failed_sources_count == 0
    assert audit.is_thin_evidence is False
    assert len(audit.warnings) == 0

    assert audit.metrics_summary is not None
    assert audit.metrics_summary.total_metrics_found == 2
    assert audit.metrics_summary.traceable_metrics_count == 2
    assert audit.metrics_summary.untraceable_metrics_count == 0
    assert audit.metrics_summary.missing_baseline_count == 0
    assert audit.metrics_summary.specified_baseline_count == 1
    assert audit.metrics_summary.explicitly_unspecified_baseline_count == 1
    assert audit.metrics_summary.rows[1].baseline_status == "explicitly_unspecified"
    assert any(
        "explicitly unspecified" in warning
        for warning in audit.metrics_summary.warnings
    )


def test_report_metadata_preserves_all_audit_warnings(tmp_path):
    """Confirm report metadata footer explicitly displays all warnings so they cannot silently vanish."""
    from research_crew.main import _build_report_metadata

    store = EvidenceStore.create(output_dir=tmp_path)
    # Store with one snippet source
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.snippet("https://arxiv.org/abs/1706.03762", "Vaswani snippet")
        )
    )

    # Deficient report: missing foundations, unrecorded URL, missing baseline, untraceable number
    deficient_report = (
        "# Research Summary: Transformer Attention\n\n"
        "## Key Insights\n"
        "See https://unrecorded-hallucination.com/paper.pdf.\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Speedup              | 99.9x |                               | https://arxiv.org/abs/1706.03762 |\n"
    )

    audit = audit_report_citations(deficient_report, store, min_sources=3)

    metadata = _build_report_metadata(
        audit=audit,
        store=store,
        elapsed=12.5,
        model="test-model",
    )

    # Confirm all warning classes appear explicitly in the metadata footer
    assert "Warning: Report is missing the required 'Core Foundations & Mechanism' section." in metadata
    assert "cited source URL(s) were not found in the evidence store" in metadata
    assert "Evidence coverage flagged as thin for this run." in metadata
    assert "omit both a comparison baseline and an explicit disclosure" in metadata
    assert "could not be traced to captured source text" in metadata
    assert "Provenance matching verifies retrieval records; it does not establish semantic factual truth" in metadata


# ---------------------------------------------------------------------------
# Claim-to-Evidence Relevance Regression Tests
# ---------------------------------------------------------------------------

def test_claim_relevance_matching_training_scale(tmp_path):
    """Claim-to-evidence relevance: matching training-scale claim and source."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/tts",
                "The model was trained on approximately 20,000 hours of speech data.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Training Scale | ~20,000 hours | Training dataset size; [Baseline unspecified in source text] | https://example.com/tts |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.supported_relevance_count == 1
    assert audit.possible_mismatch_count == 0
    assert audit.insufficient_evidence_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"


def test_claim_relevance_false_speedup_gpu_count(tmp_path):
    """Claim-to-evidence relevance: false speedup claim where number appears only as GPU count."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/1909.08053",
                "Megatron-LM was trained across 512 GPUs with 8.3 billion parameters.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Training Speedup | 512x speedup | vs single GPU baseline | https://arxiv.org/abs/1909.08053 |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.possible_mismatch_count == 1
    assert audit.supported_relevance_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("mismatch" in w.lower() for w in row.warnings)


def test_claim_relevance_wer_mismatch_dataset_size_source(tmp_path):
    """Claim-to-evidence relevance: WER claim cited to dataset-size-only source."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/speech-corpus",
                "The speech evaluation corpus covers 50 languages and 1,000 speakers with 12 annotators.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| WER improvement | 12 points | vs prior baseline | https://example.com/speech-corpus |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.possible_mismatch_count == 1
    assert audit.supported_relevance_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("mismatch" in w.lower() for w in row.warnings)


def test_claim_relevance_latency_claim_matches_inference_latency_source(tmp_path):
    """Claim-to-evidence relevance: latency claim whose source explicitly discusses inference latency."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/system-latency",
                "The system achieves 100 ms inference latency on standard server hardware.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Speech latency | 100 ms | vs streaming baseline | https://example.com/system-latency |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.supported_relevance_count == 1
    assert audit.possible_mismatch_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"


def test_claim_relevance_insufficient_or_snippet_only_evidence(tmp_path):
    """Claim-to-evidence relevance: snippet-only or missing evidence returns insufficient_evidence."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.snippet(
                "https://example.com/snippet-source",
                "Search result snippet stating 2.4x speedup on benchmarks.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup | 2.4x | vs baseline | https://example.com/snippet-source |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.insufficient_evidence_count == 1
    assert audit.supported_relevance_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "insufficient_evidence"
    assert any("snippet-only" in w.lower() or "insufficient" in w.lower() for w in row.warnings)


def test_adversarial_dataset_hours_incorrectly_treated_as_speech_quality(tmp_path):
    """Adversarial test: A dataset-hours value incorrectly treated as a speech-quality benchmark."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/tts",
                "MahaTTS-v2 was trained on approximately 20,000 hours of speech.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Speech Quality | ~20,000 hours | vs prior baseline | https://example.com/tts |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.performance_metrics_count == 1
    assert audit.dataset_scale_metrics_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.metric_type == "performance_benchmark"
    assert row.relevance_status == "possible_mismatch"
    assert audit.possible_mismatch_count == 1
    assert any("mischaracterized" in w.lower() or "dataset" in w.lower() for w in row.warnings)


def test_adversarial_comparative_claim_with_no_reported_experimental_result(tmp_path):
    """Adversarial test: 'Significantly improves' with no reported experimental result."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/tts",
                "The new model achieves good speech synthesis output.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Speech Quality | significantly improves | vs prior baseline | https://example.com/tts |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 0
    assert audit.untraceable_metrics_count == 1
    row = audit.rows[0]
    assert row.is_traceable is False
    assert row.relevance_status == "possible_mismatch"
    assert audit.possible_mismatch_count == 1
    assert any("comparative" in w.lower() or "result" in w.lower() for w in row.warnings)


def test_adversarial_baseline_named_in_prose_not_linked_to_reported_metric(tmp_path):
    """Adversarial test: A baseline named in prose but not linked to the reported metric."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/abs/2205.14135",
                "FlashAttention yields a 2.4x speedup on LRA benchmarks.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "We evaluate FlashAttention against standard self-attention described in prose [1].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup | 2.4x | Baseline mentioned in prose | https://arxiv.org/abs/2205.14135 |\n\n"
        "## Sources\n"
        "[1] https://arxiv.org/abs/2205.14135\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.baseline_status == "unlinked_prose_baseline"
    assert row.has_baseline_or_flag is False
    assert audit.specified_baseline_count == 0
    assert audit.unlinked_prose_baseline_count == 1
    assert any("prose" in w.lower() or "not explicitly tied" in w.lower() for w in row.warnings)


def test_adversarial_source_snippet_correct_number_unrelated_context(tmp_path):
    """Adversarial test: A source snippet containing the correct number but unrelated context."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.snippet(
                "https://arxiv.org/abs/123",
                "See Section 2.4 for review of previous background work.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| LRA Speedup | 2.4x | vs baseline | https://arxiv.org/abs/123 |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.evidence_depth == "snippet_only"
    assert row.is_full_text_verified is False
    assert row.relevance_status == "insufficient_evidence"
    assert audit.snippet_only_supported_count == 1
    assert any("snippet-only" in w.lower() or "insufficient" in w.lower() for w in row.warnings)


def test_adversarial_valid_dataset_scale_statistic_traceable_not_performance_result(tmp_path):
    """Adversarial test: A valid dataset-scale statistic remains traceable without being labeled a performance result."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/tts",
                "The model was trained on 20,000 hours of speech data.",
            )
        )
    )

    report = (
        "# Research Summary\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Training Scale | ~20,000 hours | Training dataset size; [Baseline unspecified in source text] | https://example.com/tts |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.traceable_metrics_count == 1
    assert audit.dataset_scale_metrics_count == 1
    assert audit.performance_metrics_count == 0
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.metric_type == "dataset_scale"
    assert row.relevance_status == "supported_by_heuristic"
    assert row.evidence_depth == "full_text"
    assert row.is_full_text_verified is True


# ---------------------------------------------------------------------------
# Live-Run Audit Defects Regression Tests (Run cef2ae770a82443a86cd17b0778b57b1)
# ---------------------------------------------------------------------------

def test_regression_dataset_scale_hours_speakers_languages_classification(tmp_path):
    """Defect 1: 1,704 hours, 10,496 speakers, and 22 languages must all be classified as dataset-scale statistics."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/corpus",
                "The multilingual corpus comprises 1,704 hours of recorded speech across 10,496 speakers in 22 languages.",
            )
        )
    )

    report = (
        "# Multilingual Speech Corpus Report\n\n"
        "## Core Foundations & Mechanism\n"
        "This dataset provides diverse audio for multilingual speech synthesis.\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Audio Duration       | 1,704 hours | Total speech audio; [Baseline unspecified in source text] | https://example.com/corpus |\n"
        "| Speaker Diversity    | 10,496 speakers | Distinct speaker count; [Baseline unspecified in source text] | https://example.com/corpus |\n"
        "| Language Coverage    | 22 languages | Supported language diversity; [Baseline unspecified in source text] | https://example.com/corpus |\n"
    )

    audit = audit_report_citations(report, store)
    metrics = audit.metrics_summary
    assert metrics is not None
    assert metrics.total_metrics_found == 3
    # All 3 must be dataset-scale statistics, 0 performance benchmarks
    assert metrics.dataset_scale_metrics_count == 3
    assert metrics.performance_metrics_count == 0

    assert metrics.rows[0].metric_type == "dataset_scale"
    assert metrics.rows[1].metric_type == "dataset_scale"
    assert metrics.rows[2].metric_type == "dataset_scale"


def test_regression_evidence_depth_labels_distinguish_source_and_metric_populations(tmp_path):
    """Defect 2: Citation audit (1 full-text, 4 snippets) vs metric footer (3 full-text, 0 snippet metric rows).

    Ensure fields and footer labels clearly distinguish citation-level evidence depth from
    metric-row evidence depth so counts refer explicitly to distinct populations without contradiction.
    """
    from research_crew.main import _build_report_metadata

    store = EvidenceStore.create(output_dir=tmp_path)
    # 1 full-text source
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/primary-paper",
                "Primary paper content with 1,704 hours, 10,496 speakers, and 22 languages.",
            )
        )
    )
    # 4 snippet-only sources
    for i in range(1, 5):
        store.add(
            EvidenceRecord.from_tool_result(
                ToolFetchResult.snippet(
                    f"https://example.com/snippet-{i}",
                    f"Snippet observation text {i}",
                )
            )
        )

    # Report cites all 5 sources across the text, but all 3 metric rows cite the primary paper
    report = (
        "# Multilingual Speech Synthesis Report\n\n"
        "## Core Foundations & Mechanism\n"
        "Foundational methods are described in [1](https://example.com/primary-paper) "
        "and related works [2](https://example.com/snippet-1), [3](https://example.com/snippet-2), "
        "[4](https://example.com/snippet-3), [5](https://example.com/snippet-4).\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Audio Duration       | 1,704 hours | Total speech hours; [Baseline unspecified in source text] | https://example.com/primary-paper |\n"
        "| Speaker Diversity    | 10,496 speakers | Diverse speakers; [Baseline unspecified in source text] | https://example.com/primary-paper |\n"
        "| Language Coverage    | 22 languages | Language count; [Baseline unspecified in source text] | https://example.com/primary-paper |\n"
    )

    audit = audit_report_citations(report, store)
    metrics = audit.metrics_summary
    assert metrics is not None

    # Population 1: Cited source URLs
    assert audit.total_citations_found == 5
    assert audit.matched_sources_count == 5
    assert audit.full_text_sources_count == 1
    assert audit.snippet_only_sources_count == 4

    # Population 2: Metric table rows
    assert metrics.total_metrics_found == 3
    assert metrics.full_text_metric_rows_count == 3
    assert metrics.snippet_only_metric_rows_count == 0
    # Backward-compatible aliases
    assert metrics.full_text_supported_count == 3
    assert metrics.snippet_only_supported_count == 0

    # Build report metadata footer and check explicit population labels
    metadata = _build_report_metadata(audit, store, elapsed=15.0)

    # Citation-level population label
    assert "Citation-level evidence depth: 5/5 cited source URLs verified in store (1 full-text sources, 4 snippet-only sources)." in metadata
    # Metric-row population label
    assert "Metric-row evidence depth: 3/3 metric rows cite full-text sources (0 metric rows cite snippet-only sources)." in metadata


def test_regression_prose_comparative_claims_without_quantitative_metrics(tmp_path):
    """Defect 3: Detect and warn about comparative prose claims unsupported by quantitative metric benchmarks."""
    from research_crew.main import _build_report_metadata

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/speech-system",
                "Speech system trained on 1,704 hours, 10,496 speakers, and 22 languages.",
            )
        )
    )

    # Report prose includes unsupported comparative claims: "superior zero-shot speaker generalization" and "significantly enhanced"
    # Metrics table has only dataset-scale statistics (0 performance benchmarks)
    report = (
        "# Next-Generation Speech Synthesis\n\n"
        "## Core Foundations & Mechanism\n"
        "The model architecture demonstrates superior zero-shot speaker generalization across unseen speakers. "
        "Furthermore, audio synthesis quality is significantly enhanced compared to baseline architectures.\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Audio Duration       | 1,704 hours | Total audio; [Baseline unspecified in source text] | https://example.com/speech-system |\n"
        "| Speakers             | 10,496 speakers | Speaker count; [Baseline unspecified in source text] | https://example.com/speech-system |\n"
        "| Languages            | 22 languages | Language coverage; [Baseline unspecified in source text] | https://example.com/speech-system |\n"
    )

    audit = audit_report_citations(report, store)
    assert len(audit.prose_comparative_warnings) >= 2
    assert any("superior zero-shot speaker generalization" in w for w in audit.prose_comparative_warnings)
    assert any("significantly enhanced" in w for w in audit.prose_comparative_warnings)

    # Check that audit.warnings also contains these prose warnings
    assert any("superior zero-shot speaker generalization" in w for w in audit.warnings)
    assert any("significantly enhanced" in w for w in audit.warnings)

    # Metadata footer includes warnings and explicit limitation note
    metadata = _build_report_metadata(audit, store, elapsed=10.0)
    assert "superior zero-shot speaker generalization" in metadata
    assert "significantly enhanced" in metadata
    assert "Prose audit limitation:" in metadata
    assert "does not perform full natural language semantic verification or entailment" in metadata


def test_regression_live_run_53b5bd95c3ff_prose_comparative_claims(tmp_path):
    """Regression test reproducing live run 53b5bd95c3ff4919819390cbba44fef6.

    Verifies:
    1. Surfaces warnings for exact phrases:
       - 'improves zero-shot speaker generalization'
       - 'enhance speech synthesis quality'
       - 'significantly enhanced'
    2. Warns that the metrics table contains no corresponding quantitative evaluation result.
    3. Avoids false positives for neutral statements ('the corpus contains 22 languages')
       and architectural descriptions ('uses conditional flow matching', 'speech enhancement models').
    4. Suppresses baseline-unspecified warnings for pure dataset-scale statistics.
    """
    from research_crew.main import _build_report_metadata

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/html/2409.05356",
                "IndicVoices-R contains 1,704 hours across 10,496 speakers and 22 languages.",
            )
        )
    )
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/html/2505.18609v1/",
                "Rasmalai provides 13,000 hours of speech with 24 million text descriptions across 23 languages.",
            )
        )
    )
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://huggingface.co/papers/2508.14049",
                "MahaTTS-v2 trained on 20K hours of training data.",
            )
        )
    )

    report = (
        "# Research Summary: Indian Language TTS Models\n\n"
        "## Core Foundations & Mechanism\n"
        "Pipelines leverage cross-lingual generalization of denoising and speech enhancement models [1]. "
        "The architecture uses conditional flow matching for semantics-to-melspectrogram generation [3]. "
        "The corpus contains 22 languages [1].\n\n"
        "## Key Insights\n"
        "Fine-tuning an English pre-trained model on IV-R improves zero-shot speaker generalization [1]. "
        "Alternative frameworks utilize flow models to enhance speech synthesis quality for low-resource languages [3]. "
        "Audio synthesis quality is significantly enhanced compared to baseline architectures [3].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Training Data Size (IndicVoices-R) | 1,704 hours of speech across 10,496 speakers and 22 languages | [Baseline unspecified in source text] | https://arxiv.org/html/2409.05356 |\n"
        "| Training Data Size (Rasmalai) | 13,000 hours of speech across 23 languages | [Baseline unspecified in source text] | https://arxiv.org/html/2505.18609v1/ |\n"
        "| Training Data Size (MahaTTS-v2) | 20K hours of training data | [Baseline unspecified in source text] | https://huggingface.co/papers/2508.14049 |\n"
    )

    audit = audit_report_citations(report, store)
    metrics = audit.metrics_summary
    assert metrics is not None

    # All 3 metrics are dataset-scale statistics, 0 performance benchmarks
    assert metrics.dataset_scale_metrics_count == 3
    assert metrics.performance_metrics_count == 0

    # 1. Exactly identifies the target comparative claims
    assert any("improves zero-shot speaker generalization" in w for w in audit.prose_comparative_warnings)
    assert any("enhance speech synthesis quality" in w for w in audit.prose_comparative_warnings)
    assert any("significantly enhanced" in w for w in audit.prose_comparative_warnings)

    # 2. Explains that the metrics table contains no corresponding quantitative evaluation result
    target_warnings = [
        w for w in audit.prose_comparative_warnings
        if any(target in w for target in (
            "improves zero-shot speaker generalization",
            "enhance speech synthesis quality",
            "significantly enhanced",
        ))
    ]
    assert len(target_warnings) == 3
    for w in target_warnings:
        assert "without a corresponding quantitative evaluation result in the metrics table" in w

    # 3. Avoids false positives for neutral statements and architectural descriptions
    assert not any("contains 22 languages" in w for w in audit.warnings)
    assert not any("conditional flow matching" in w for w in audit.warnings)
    assert not any("speech enhancement models" in w for w in audit.warnings)

    # 4. Suppresses baseline-unspecified warnings for pure dataset-scale statistics
    assert metrics.explicitly_unspecified_baseline_count == 3
    assert metrics.explicitly_unspecified_performance_baseline_count == 0
    assert not any("Comparison baseline is explicitly unspecified" in w for w in metrics.warnings)

    metadata = _build_report_metadata(audit, store, elapsed=25.0)
    # The metadata footer does NOT warn about unspecified baselines for dataset statistics
    assert "performance metric(s) explicitly disclose that the comparison baseline is unspecified" not in metadata
    assert "metric(s) explicitly disclose that the comparison baseline is unspecified" not in metadata
    # The metadata footer DOES disclose the unsupported comparative claims
    assert "improves zero-shot speaker generalization" in metadata
    assert "enhance speech synthesis quality" in metadata
    assert "significantly enhanced" in metadata
    # The metadata footer explicitly preserves the limitation disclaimer
    assert "Prose audit limitation:" in metadata
    assert "does not perform full natural language semantic verification or entailment" in metadata


def test_regression_performance_comparison_preserves_baseline_verification(tmp_path):
    """Ensure baseline verification is strictly preserved for performance benchmark comparisons."""
    from research_crew.main import _build_report_metadata

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/eval",
                "The system achieves a WER of 4.2% on the test split.",
            )
        )
    )

    report = (
        "# Evaluation Report\n\n"
        "## Core Foundations & Mechanism\n"
        "Speech synthesis and recognition evaluation.\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Word Error Rate (WER) | 4.2% | [Baseline unspecified in source text] | https://example.com/eval |\n"
    )

    audit = audit_report_citations(report, store)
    metrics = audit.metrics_summary
    assert metrics is not None

    # Classified as a performance benchmark
    assert metrics.performance_metrics_count == 1
    assert metrics.dataset_scale_metrics_count == 0
    assert metrics.explicitly_unspecified_performance_baseline_count == 1

    # Warning MUST be present for performance benchmark with unspecified baseline
    assert any("Comparison baseline is explicitly unspecified" in w for w in metrics.warnings)

    metadata = _build_report_metadata(audit, store, elapsed=5.0)
    assert "1 performance metric(s) explicitly disclose that the comparison baseline is unspecified" in metadata


def test_report_dimension_coverage_complete():
    """Verify that a report addressing all 8 dimensions passes coverage audit with zero gaps."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    report = (
        "# Research Summary: Indian Language Speech Synthesis\n\n"
        "## Core Foundations & Mechanism\n"
        "The model architecture uses conditional flow matching with a conformer acoustic model and neural vocoder.\n"
        "The system provides native Hindi, Marathi, and Tamil language support.\n"
        "Training leverages the IndicTTS dataset and Common Voice corpus with 2,500 hours of speech data.\n"
        "Cross-lingual transfer learning enables zero-shot speaker adaptation for low-resource Indian dialects.\n\n"
        "## Benchmarks & Evaluation\n"
        "Quality benchmarks show a MOS of 4.35 and WER of 3.8% across evaluation benchmarks.\n"
        "Inference latency is 45 ms achieving a real-time factor (RTF) of 0.12 on test audio.\n"
        "Models were trained using 8 NVIDIA A100 GPUs with CUDA acceleration and 80GB VRAM.\n"
        "Code and weights are open-source and released under the Apache 2.0 license for commercial use.\n"
    )

    cov = audit_report_dimension_coverage(report)
    assert cov.total_dimensions == 8
    assert cov.covered_dimensions_count == 8
    assert cov.gap_dimensions_count == 0
    assert len(cov.warnings) == 0

    expected_dims = [
        "architecture",
        "hindi_marathi_tamil_support",
        "datasets",
        "low_resource_adaptation",
        "quality_benchmarks",
        "latency",
        "hardware",
        "licensing",
    ]
    for dim in expected_dims:
        assert dim in cov.covered_dimensions
        assert cov.dimension_results[dim].is_covered is True
        assert cov.dimension_results[dim].status == "covered"
        assert len(cov.dimension_results[dim].matched_snippets) > 0


def test_report_dimension_coverage_gaps_marked_without_fabrication():
    """Verify that absent dimensions are marked explicitly as gaps without fabricating content."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    report = (
        "# Research Summary: Minimal Pipeline\n\n"
        "## Core Foundations & Mechanism\n"
        "The transformer architecture uses multi-head attention.\n"
        "The system was evaluated on a small corpus dataset.\n"
    )

    cov = audit_report_dimension_coverage(report)
    assert cov.total_dimensions == 8
    assert cov.covered_dimensions_count == 2
    assert cov.gap_dimensions_count == 6

    assert "architecture" in cov.covered_dimensions
    assert "datasets" in cov.covered_dimensions

    expected_gaps = [
        "hindi_marathi_tamil_support",
        "low_resource_adaptation",
        "quality_benchmarks",
        "latency",
        "hardware",
        "licensing",
    ]
    for gap in expected_gaps:
        assert gap in cov.gap_dimensions
        res = cov.dimension_results[gap]
        assert res.is_covered is False
        assert res.status == "gap"
        assert res.matched_snippets == []
        assert any(gap in w.lower() or res.display_name.lower() in w.lower() for w in cov.warnings)


# ---------------------------------------------------------------------------
# Phase 2A — Metric Classification and Baseline Auditing Regression Tests
# ---------------------------------------------------------------------------

def test_regression_phase2a_exact_match_gain_metric_classification_with_dataset_baseline(tmp_path):
    """Regression test 1: Exact Match Gain is classified as a performance metric when baseline mentions dataset."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/rag-eval",
                "Our model achieves +13 points Exact Match Gain compared with the vanilla RAG baseline on the ICR2 dataset.",
            )
        )
    )

    report = (
        "# Evaluation Report\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Exact Match Gain     | +13 points | compared with the vanilla RAG baseline on the ICR2 dataset | https://example.com/rag-eval |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.total_metrics_found == 1
    assert audit.performance_metrics_count == 1
    assert audit.dataset_scale_metrics_count == 0
    row = audit.rows[0]
    assert row.metric_type == "performance_benchmark"
    assert row.is_traceable is True
    # Baseline was specified; ensure no false missing-baseline warnings
    assert not any("missing" in w.lower() for w in row.warnings)


def test_regression_phase2a_performance_metrics_classification_table_driven():
    """Regression test 2: Representative performance metrics classified as performance_benchmark."""
    from research_crew.evidence.validator import _classify_metric_type

    test_cases = [
        ("Exact Match Gain", "+13 points", "compared with the vanilla RAG baseline on the ICR2 dataset"),
        ("Exact Match", "85.2%", "evaluated on SQuAD 2.0 dataset"),
        ("F1 Score", "89.1", "compared to baseline on benchmark dataset"),
        ("Accuracy", "94.5%", "evaluated on ImageNet test dataset"),
        ("Recall", "88.0%", "BM25 baseline on MS MARCO dataset"),
        ("Precision", "91.2%", "BM25 baseline on search dataset"),
        ("Latency", "24ms", "50ms on dataset benchmark"),
        ("Throughput", "350 tokens/s", "200 tokens/s on evaluation dataset"),
        ("Tokens per second", "150 tok/s", "baseline on benchmark dataset"),
        ("Win Rate", "64.5%", "compared to GPT-4 on test dataset"),
        ("ROUGE-L", "42.1", "compared to standard baseline on CNN/DM dataset"),
        ("BLEU", "31.4", "baseline model on WMT dataset"),
        ("Word Error Rate (WER)", "4.2%", "baseline on LibriSpeech dataset"),
    ]

    for metric, value, baseline in test_cases:
        result = _classify_metric_type(metric, value, baseline)
        assert result == "performance_benchmark", (
            f"Expected 'performance_benchmark' for metric='{metric}', value='{value}', "
            f"baseline='{baseline}', but got '{result}'"
        )


def test_regression_phase2a_dataset_scale_metrics_classification_table_driven():
    """Regression test 3: Representative dataset-scale statistics remain classified as dataset_scale."""
    from research_crew.evidence.validator import _classify_metric_type

    test_cases = [
        ("Number of documents", "1.5M documents", "prior corpus"),
        ("Number of documents", "1.5M", "unspecified"),
        ("Number of records", "50,000 records", "v1 dataset"),
        ("Number of records", "50,000", "none"),
        ("Number of examples", "10,000 examples", "evaluation split"),
        ("Dataset size", "250 GB", "uncompressed text"),
        ("Dataset Size", "10,000 documents", "N/A"),
        ("Corpus size", "500M tokens", "Wikipedia dump"),
        ("Number of tokens in a corpus", "500B tokens", "common crawl dataset"),
        ("Tokens in a corpus", "2.5 trillion tokens", "training corpus"),
        ("Accuracy Benchmark Dataset Size", "10,000 examples", "ICR2 evaluation set"),
        ("Evaluation Benchmark Examples", "5,000 examples", "benchmark test split"),
        ("Evaluation Benchmark Document Count", "2,500 documents", "curated benchmark"),
        ("Audio Duration", "1,704 hours", "Total speech audio"),
        ("Speaker Diversity", "10,496 speakers", "Distinct speaker count"),
        ("Language Coverage", "22 languages", "Supported language diversity"),
        ("Training Scale", "~20,000 hours", "Training dataset size"),
    ]

    for metric, value, baseline in test_cases:
        result = _classify_metric_type(metric, value, baseline)
        assert result == "dataset_scale", (
            f"Expected 'dataset_scale' for metric='{metric}', value='{value}', "
            f"baseline='{baseline}', but got '{result}'"
        )


def test_regression_phase2a_comparative_performance_metric_missing_baseline_triggers_warning(tmp_path):
    """Regression test 4: Comparative performance metric without baseline triggers missing-baseline warning."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/eval",
                "Exact Match Gain reaches +13 points on the evaluation benchmark.",
            )
        )
    )

    report = (
        "# Evaluation Report\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Exact Match Gain     | +13 points | - | https://example.com/eval |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.performance_metrics_count == 1
    assert audit.dataset_scale_metrics_count == 0
    assert audit.missing_performance_baseline_count == 1

    row = audit.rows[0]
    assert any("Comparison baseline is missing or empty" in w for w in row.warnings)


def test_regression_phase2a_comparative_performance_metric_valid_baseline_no_false_warning(tmp_path):
    """Regression test 5: Comparative performance metric with valid baseline does not trigger false warning."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/eval",
                "Achieves 89.2% Accuracy compared to 82.0% for prior baseline on ImageNet dataset.",
            )
        )
    )

    report = (
        "# Evaluation Report\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Accuracy             | 89.2% | 82.0% for prior baseline on ImageNet dataset | https://example.com/eval |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.performance_metrics_count == 1
    assert audit.missing_performance_baseline_count == 0
    assert audit.specified_baseline_count == 1

    row = audit.rows[0]
    assert not any("Comparison baseline is missing or empty" in w for w in row.warnings)
    assert not any("Comparison baseline is explicitly unspecified" in w for w in row.warnings)


def test_regression_phase2a_dataset_scale_metric_does_not_trigger_performance_baseline_warning(tmp_path):
    """Regression test 6: Dataset-scale metric does not trigger performance-baseline warning."""
    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://example.com/corpus",
                "The corpus contains 1.5M documents across the entire archive.",
            )
        )
    )

    report = (
        "# Dataset Report\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|----------------------|-------|-------------------------------|--------|\n"
        "| Number of documents  | 1.5M  | [Baseline unspecified in source text] | https://example.com/corpus |\n"
    )

    audit = audit_report_metrics(report, store)
    assert audit.dataset_scale_metrics_count == 1
    assert audit.performance_metrics_count == 0
    assert audit.missing_performance_baseline_count == 0
    assert audit.explicitly_unspecified_performance_baseline_count == 0

    row = audit.rows[0]
    assert row.metric_type == "dataset_scale"
    assert not any("numeric traceability does not verify the comparison" in w for w in row.warnings)
    assert not any("Comparison baseline is missing or empty" in w for w in row.warnings)


def test_regression_phase2a_accuracy_dataset_boundary():
    """Classification boundary: Accuracy on Training Dataset vs Accuracy Benchmark Dataset Size."""
    from research_crew.evidence.validator import _classify_metric_type

    # 1. "Accuracy on Training Dataset" (92%) must be classified as performance_benchmark
    res_perf = _classify_metric_type(
        "Accuracy on Training Dataset", "92%", "[Baseline unspecified in source text]"
    )
    assert res_perf == "performance_benchmark"

    # 2. "Accuracy Benchmark Dataset Size" (10,000 examples) must be classified as dataset_scale
    res_scale = _classify_metric_type(
        "Accuracy Benchmark Dataset Size", "10,000 examples", "ICR2 evaluation set"
    )
    assert res_scale == "dataset_scale"


def test_regression_phase2a_classification_precedence_boundary():
    """Classification precedence boundary: benchmark performance vs dataset/model scale."""
    from research_crew.evidence.validator import _classify_metric_type

    test_cases = [
        ("Accuracy on Training Dataset", "92%", "[Baseline unspecified in source text]", "performance_benchmark"),
        ("Accuracy Benchmark Dataset Size", "10,000 examples", "ICR2 evaluation set", "dataset_scale"),
        ("Number of documents", "1.5M documents", "prior corpus", "dataset_scale"),
        ("Language Model Accuracy", "91%", "evaluated on benchmark dataset", "performance_benchmark"),
        ("Model Size", "7B parameters", "N/A", "dataset_scale"),
    ]

    for metric, value, baseline, expected in test_cases:
        actual = _classify_metric_type(metric, value, baseline)
        assert actual == expected, (
            f"Precedence classification failed for metric='{metric}', value='{value}', "
            f"baseline='{baseline}': expected '{expected}', got '{actual}'"
        )


# ===========================================================================
# Phase 2B Regression Tests: Topic-Aware Coverage & Baseline Auditing
# ===========================================================================

def test_phase2b_topic_aware_coverage_rag_vs_long_context():
    """Verify that RAG vs. Long-Context research produces domain-relevant dimensions without speech synthesis."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Comparative evaluation of retrieval-augmented generation (RAG) versus long-context LLMs"
    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Retrieval-Augmented Generation (RAG) uses dense retrieval with vector index embeddings and chunking.\n"
        "RAG top-k chunk retrieval minimizes irrelevant context.\n"
        "Long-context LLM architecture leverages extended context windows and RoPE rotary position embeddings.\n"
        "Long-context transformers process million-token prompts with flash-attention.\n\n"
        "## Benchmarks & Evaluation\n"
        "Needle-in-a-haystack (NIAH) benchmarks demonstrate retrieval accuracy degrades at extended context lengths.\n"
        "Evaluation results show retrieval recall of 94% on passkey tests.\n"
        "Inference latency and time-to-first-token (TTFT) grow with context window size, raising token cost.\n"
        "Computational cost for long prompts exceeds RAG query costs.\n"
        "Context faithfulness metrics show RAG reduces hallucination rates compared to pure parametric recall.\n"
        "Hallucination mitigation is enhanced through grounded citations.\n"
        "Hybrid approach combines routing and adaptive retrieval to select between RAG and long-context models.\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)

    assert cov.is_topic_aware is True
    assert cov.total_dimensions == 6
    assert cov.gap_dimensions_count == 0
    assert cov.covered_dimensions_count == 6

    # Verify RAG vs Long-Context dimensions
    expected_dims = {
        "rag_architecture",
        "long_context_architecture",
        "retrieval_benchmarks",
        "computational_cost_latency",
        "hallucination_faithfulness",
        "hybrid_architectures",
    }
    assert set(cov.covered_dimensions) == expected_dims

    # Verify NO spurious speech-synthesis dimensions are present
    speech_keys = {
        "hindi_marathi_tamil_support",
        "conformer/vocoder",
        "quality_benchmarks",
        "datasets",
    }
    for sk in ["hindi_marathi_tamil_support", "conformer", "vocoder", "indictts"]:
        assert sk not in cov.dimension_results


def test_phase2b_topic_aware_coverage_distributed_databases():
    """Verify that distributed databases research produces database dimensions without speech synthesis."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Distributed database sharding and consensus protocols"
    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Data sharding and consistent hashing partition write workloads across database shards.\n"
        "Hash partitioning ensures uniform data distribution across cluster nodes.\n"
        "The system uses Raft consensus protocol for state machine replication and leader election.\n"
        "Quorum consensus guarantees consistency across replicated logs.\n"
        "Fault tolerance mechanisms provide high availability and partition resilience during network splits.\n"
        "Heartbeat failover detects node failure in under 500ms.\n\n"
        "## Performance & Benchmarks\n"
        "Throughput benchmarks on YCSB show 85,000 transactions per second (TPS) with read-write latency under 5ms.\n"
        "IOPS benchmarks demonstrate linear scaling.\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)

    assert cov.is_topic_aware is True
    assert cov.total_dimensions == 5

    # Covered: sharding, consensus, fault tolerance, throughput benchmarks
    assert "data_sharding" in cov.covered_dimensions
    assert "consensus_protocols" in cov.covered_dimensions
    assert "fault_tolerance" in cov.covered_dimensions
    assert "throughput_benchmarks" in cov.covered_dimensions

    # Missing: distributed transactions (2PC / ACID) is absent from the report
    assert "distributed_transactions" in cov.gap_dimensions
    assert cov.gap_dimensions_count == 1
    assert any("Distributed Transactions" in w for w in cov.warnings)

    # Strictly verify NO speech dimensions
    for k in cov.dimension_results:
        assert "hindi" not in k.lower()
        assert "speech" not in k.lower()


def test_phase2b_topic_aware_coverage_third_topic_generality():
    """Verify generality on a third unrelated domain: RLHF vs. DPO preference optimization."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Reinforcement Learning from Human Feedback (RLHF) versus Direct Preference Optimization (DPO)"
    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "RLHF trains a reward model followed by PPO proximal policy optimization with actor-critic value functions.\n"
        "The PPO policy gradient updates the policy using empirical reward feedback.\n"
        "Direct Preference Optimization (DPO) optimizes an implicit reward formulation without a separate reward model.\n"
        "DPO derives the preference loss directly using a reference model.\n\n"
        "## Benchmarks & Evaluation\n"
        "Alignment benchmarks on MT-Bench and AlpacaEval report win-rate comparisons against human evaluation baselines.\n"
        "Preference benchmark evaluations show competitive alignment scores.\n"
        "Mode collapse and reward hacking are mitigated through KL divergence penalties and regularization drift control.\n"
        "Training stability is improved by avoiding policy drift.\n"
        "Compute efficiency analysis shows DPO saves 40% GPU hours and reduces memory footprint by avoiding reward model overhead.\n"
        "Throughput increases by 1.8x during alignment training.\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)

    assert cov.is_topic_aware is True
    assert cov.total_dimensions == 5
    assert cov.gap_dimensions_count == 0
    assert cov.covered_dimensions_count == 5

    expected_dims = {
        "rlhf_architecture",
        "dpo_architecture",
        "alignment_benchmarks",
        "training_stability_collapse",
        "compute_efficiency",
    }
    assert set(cov.covered_dimensions) == expected_dims
    assert "hindi_marathi_tamil_support" not in cov.dimension_results


def test_phase2b_coverage_levels_covered_partial_missing():
    """Verify distinct identification of covered (>=2), partially covered (1), and missing (0) dimensions."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Distributed database sharding and consensus protocols"
    # Report contains:
    # - sharding mentioned 3 times (covered)
    # - consensus mentioned exactly once in passing (partial)
    # - distributed transactions: 0 mentions (missing/gap)
    # - fault tolerance: 0 mentions (missing/gap)
    # - throughput benchmarks: 2 mentions (covered)
    report = (
        "# Research Summary: Distributed Databases\n\n"
        "## Sharding Architecture\n"
        "Data sharding distributes keys across nodes. Hash partitioning and rebalancing maintain shard balance.\n\n"
        "## Replication\n"
        "A consensus protocol is used for replication.\n\n"
        "## Performance\n"
        "Throughput reached 10,000 TPS. High throughput was verified.\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)

    assert cov.total_dimensions == 5
    assert cov.covered_dimensions_count == 2
    assert cov.partially_covered_dimensions_count == 1
    assert cov.gap_dimensions_count == 2

    # Check status and levels
    assert cov.dimension_results["data_sharding"].status == "covered"
    assert cov.dimension_results["data_sharding"].coverage_level == "full"
    assert cov.dimension_results["data_sharding"].is_covered is True

    assert cov.dimension_results["consensus_protocols"].status == "partial"
    assert cov.dimension_results["consensus_protocols"].coverage_level == "partial"
    assert cov.dimension_results["consensus_protocols"].is_covered is False
    assert "consensus_protocols" in cov.partially_covered_dimensions

    assert cov.dimension_results["distributed_transactions"].status == "gap"
    assert cov.dimension_results["distributed_transactions"].coverage_level == "none"
    assert cov.dimension_results["distributed_transactions"].is_covered is False
    assert "distributed_transactions" in cov.gap_dimensions

    # Warnings should flag both partial coverage and gaps
    warnings_text = " ".join(cov.warnings)
    assert "Partial coverage" in warnings_text
    assert "Coverage gap" in warnings_text


def test_phase2b_missing_comparative_baseline_detection():
    """Verify that comparative research reports missing comparative baselines are flagged with warnings."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Comparative evaluation of retrieval-augmented generation (RAG) versus long-context LLMs"
    # Report only evaluates RAG in metrics table and body, completely omitting Long-Context baseline metrics
    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "RAG uses dense retrieval with chunking and vector index embeddings to augment generation.\n"
        "RAG retrieves top-k relevant passages from external corpora.\n\n"
        "## Evaluation & Metrics\n"
        "| Metric | Reported Value | Baseline Comparison | Evidence Depth |\n"
        "|---|---|---|---|\n"
        "| RAG Retrieval Accuracy | 89.4% | BM25 baseline 76.1% | Full Text |\n"
        "| RAG Faithfulness Score | 0.92 | Unaugmented LLM 0.65 | Full Text |\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)

    assert len(cov.missing_comparisons) > 0
    warnings_str = " ".join(cov.missing_comparisons)
    assert "Missing comparative baseline" in warnings_str
    assert "Long-Context LLMs" in warnings_str


def test_phase2b_fallback_when_dimensions_cannot_be_reliably_determined():
    """Verify that when topic is insufficient and no plan exists, the system reports that limitation rather than inventing dimensions."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    report = "A short note on neural network weights."
    # Vague 2-word topic with no research plan
    cov = audit_report_dimension_coverage(report, topic="machine learning")

    assert cov.is_topic_aware is False
    assert cov.total_dimensions == 0
    assert len(cov.warnings) > 0
    assert any("Unable to reliably derive topic-aware dimensions" in w for w in cov.warnings)


def test_phase2b_planned_queries_drive_dimension_coverage():
    """Verify that structured research plan queries from PlannerAgent dynamically establish dimension specifications."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    plan = [
        "vector database indexing latency in Milvus and Qdrant",
        "chunking size strategies for dense passage retrieval",
        "cross-encoder re-ranking precision on MS-MARCO",
    ]
    report = (
        "# Vector Search Evaluation\n\n"
        "Indexing latency in Milvus was evaluated across 10 million vectors.\n"
        "The indexing latency scaled logarithmically.\n"
        "Different chunking size strategies were benchmarked for dense retrieval.\n"
        "Chunking parameters showed optimal recall at 512 tokens.\n"
    )

    cov = audit_report_dimension_coverage(report, topic="Vector Search", planned_queries=plan)

    assert cov.is_topic_aware is True
    assert cov.total_dimensions == 3
    # First two dimensions covered, third (re-ranking) is a gap
    assert cov.covered_dimensions_count == 2
    assert cov.gap_dimensions_count == 1


# ===========================================================================
# Phase 2I Regression Tests: Prose Comparative Claims & Required Dimensions
# ===========================================================================

def test_phase2i_prose_audit_ignores_sources_and_references_section(tmp_path):
    """Task 1: Comparative phrases inside paper titles under ## Sources must NOT emit prose warnings."""
    from research_crew.evidence.validator import (
        audit_report_citations,
        check_report_prose_comparative_claims,
    )

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/html/2404.10719v1",
                "Full text of DPO and PPO study.",
                title="Is DPO Superior to PPO for LLM Alignment? A Comprehensive Study",
            )
        )
    )

    # Narrative prose contains no comparative claims; 'Superior to PPO' appears only in ## Sources
    report = (
        "# Research Summary: Alignment Algorithms\n\n"
        "## Core Foundations & Mechanism\n"
        "Direct Preference Optimization optimizes language models directly on preference pairs [1].\n"
        "Reinforcement Learning from Human Feedback aligns models using learned reward signals [1].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|---|---|---|---|\n"
        "| Win Rate | 58.2% | Baseline standard model | https://arxiv.org/html/2404.10719v1 |\n\n"
        "## Sources\n"
        "[1] Is DPO Superior to PPO for LLM Alignment? A Comprehensive Study (https://arxiv.org/html/2404.10719v1)\n"
    )

    audit = audit_report_citations(report, store)
    assert len(audit.prose_comparative_warnings) == 0
    assert not any("Superior to PPO" in w for w in audit.warnings)


def test_phase2i_prose_audit_detects_unsupported_body_claim(tmp_path):
    """Task 1: Unsupported comparative claims in body prose must continue to be detected."""
    from research_crew.evidence.validator import audit_report_citations

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/html/2406.09279v2",
                "PPO outperforms DPO by up to 2.5% in math.",
            )
        )
    )

    # Body contains unsupported claim: 'superior zero-shot speaker generalization'
    # Table has only math benchmark
    report = (
        "# Speech and Language Modeling\n\n"
        "## Core Foundations & Mechanism\n"
        "The model architecture demonstrates superior zero-shot speaker generalization across unseen speakers [1].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|---|---|---|---|\n"
        "| PPO performance in math | Up to 2.5% | PPO compared against DPO in math | https://arxiv.org/html/2406.09279v2 |\n\n"
        "## Sources\n"
        "[1] https://arxiv.org/html/2406.09279v2\n"
    )

    audit = audit_report_citations(report, store)
    assert len(audit.prose_comparative_warnings) >= 1
    assert any("superior zero-shot speaker generalization" in w for w in audit.prose_comparative_warnings)


def test_phase2i_prose_audit_handles_legitimate_dpo_ppo_comparison(tmp_path):
    """Task 1: Legitimate DPO/PPO comparisons backed by matching benchmark table rows must not emit false warnings."""
    from research_crew.evidence.validator import audit_report_citations

    store = EvidenceStore.create(output_dir=tmp_path)
    store.add(
        EvidenceRecord.from_tool_result(
            ToolFetchResult.success(
                "https://arxiv.org/html/2406.09279v2",
                "PPO outperforms DPO by up to 2.5% in math benchmarks.",
            )
        )
    )

    report = (
        "# Research Summary: DPO vs PPO\n\n"
        "## Core Foundations & Mechanism\n"
        "Across mathematical evaluation benchmarks, PPO demonstrates superior performance compared to DPO [1].\n\n"
        "## Benchmarks & Metrics\n"
        "| Metric / Measurement | Value | Context & Comparison Baseline | Source |\n"
        "|---|---|---|---|\n"
        "| PPO performance advantage in math | Up to 2.5% | PPO compared against DPO in math evaluation benchmarks | https://arxiv.org/html/2406.09279v2 |\n\n"
        "## Sources\n"
        "[1] Study of Preference Optimization (https://arxiv.org/html/2406.09279v2)\n"
    )

    audit = audit_report_citations(report, store)
    assert len(audit.prose_comparative_warnings) == 0


def test_phase2i_dimension_coverage_heading_only_not_substantive():
    """Task 2: Mentioning a dimension only in headings does not constitute substantive coverage."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Reinforcement Learning from Human Feedback (RLHF) vs Direct Preference Optimization (DPO): Compare alignment quality, training stability, computational cost, and evaluation benchmarks"
    # Heading mentions 'Computational Cost' but narrative body completely omits it
    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Reinforcement Learning from Human Feedback uses reward models and policy gradient updates.\n"
        "Direct Preference Optimization optimizes implicit rewards directly on paired data.\n"
        "Alignment evaluation benchmarks show win-rate gains on MT-Bench.\n"
        "Training stability is maintained with KL divergence regularisation.\n\n"
        "## Computational Cost & Efficiency\n\n"
        "## Sources\n"
        "[1] https://example.com/paper\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)
    assert cov.is_topic_aware is True
    # compute_efficiency should NOT be covered merely because of section heading
    assert "compute_efficiency" not in cov.covered_dimensions
    assert cov.dimension_results["compute_efficiency"].is_covered is False
    assert cov.dimension_results["compute_efficiency"].status == "gap"
    assert cov.gap_dimensions_count > 0


def test_phase2i_dimension_coverage_explicit_evidence_limited_statement():
    """Task 2: A required dimension explicitly documented as evidence-limited is covered without fabricating data."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Reinforcement Learning from Human Feedback (RLHF) vs Direct Preference Optimization (DPO): Compare alignment quality, training stability, computational cost, and evaluation benchmarks"
    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Reinforcement Learning from Human Feedback uses reward models and policy gradient updates.\n"
        "Direct Preference Optimization optimizes implicit rewards directly on paired data.\n"
        "Alignment evaluation benchmarks show win-rate gains on MT-Bench and AlpacaEval.\n"
        "Training stability is maintained with KL divergence regularization to avoid mode collapse.\n"
        "Evidence regarding computational cost and GPU hours is limited in the retrieved literature; empirical training compute was not reported in the evaluated sources.\n"
    )

    cov = audit_report_dimension_coverage(report, topic=topic)
    assert cov.is_topic_aware is True
    assert "compute_efficiency" in cov.covered_dimensions
    assert cov.dimension_results["compute_efficiency"].is_covered is True
    assert cov.dimension_results["compute_efficiency"].coverage_level == "evidence_limited"
    assert cov.gap_dimensions_count == 0


def test_phase2i_build_summary_task_passes_required_dimensions():
    """Task 2: build_summary_task passes required dimensions and computational cost guidelines into description."""
    from crewai import LLM, Task
    from research_crew.agents.synthesizer_agent import build_synthesizer_agent
    from research_crew.tasks.summary_task import build_summary_task

    dummy_llm = LLM(model="gpt-4o", api_key="fake")
    synthesizer_agent = build_synthesizer_agent(dummy_llm)
    dummy_prev_task = Task(description="Prev extraction", expected_output="[]", agent=synthesizer_agent)
    topic = "RLHF vs DPO comparison"
    req_dims = ["compute_efficiency", "training_stability"]

    task = build_summary_task(synthesizer_agent, dummy_prev_task, topic, required_dimensions=req_dims)

    assert "REQUIRED TOPIC DIMENSIONS" in task.description
    assert "Compute Efficiency" in task.description
    assert "Training Stability" in task.description
    assert "Computational Cost / Resource Overhead" in task.description
    assert "Explicit Limitation:" in task.description


def test_phase2k_required_dimensions_and_planned_queries_no_false_zero():
    """Phase 2K: Supplying both explicit required dimensions and planned queries does not produce 0/0."""
    from research_crew.evidence.validator import audit_report_dimension_coverage, derive_topic_dimensions

    topic = "Reinforcement Learning from Human Feedback (RLHF) vs Direct Preference Optimization (DPO): Compare alignment quality, training stability, computational cost, and evaluation benchmarks"
    specs, _, _ = derive_topic_dimensions(topic=topic)
    req_dims = [s[0] for s in specs]
    assert len(req_dims) > 0

    queries = [
        "Direct Preference Optimization vs RLHF alignment quality large language models training stability",
        "computational cost memory footprint DPO versus PPO reward model training LLM alignment",
        "open-source evaluation benchmarks for LLM alignment MT-Bench AlpacaEval win rate",
    ]

    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Reinforcement Learning from Human Feedback aligns language models using learned reward models.\n"
        "Direct Preference Optimization directly parameterizes implicit rewards from preference pairs.\n"
        "Evaluation benchmarks show win-rate improvements on MT-Bench and AlpacaEval.\n"
        "Training stability is governed by KL divergence constraints against policy drift.\n"
        "Detailed quantitative measurements of computational cost and GPU hours are limited in the retrieved literature.\n"
    )

    cov = audit_report_dimension_coverage(
        report,
        required_dimensions=req_dims,
        topic=topic,
        planned_queries=queries,
    )
    assert cov.total_dimensions == len(req_dims)
    assert cov.total_dimensions > 0
    assert cov.covered_dimensions_count > 0
    assert cov.gap_dimensions_count == 0
    assert f"{cov.covered_dimensions_count}/{cov.total_dimensions}" != "0/0"


def test_phase2k_covered_and_missing_dimensions_accurately_separated():
    """Phase 2K: Correctly covered dimensions are counted, while genuinely missing dimensions remain gaps."""
    from research_crew.evidence.validator import audit_report_dimension_coverage, derive_topic_dimensions

    topic = "Reinforcement Learning from Human Feedback (RLHF) vs Direct Preference Optimization (DPO): Compare alignment quality, training stability, computational cost, and evaluation benchmarks"
    specs, _, _ = derive_topic_dimensions(topic=topic)
    req_dims = [s[0] for s in specs] + ["unmentioned_distributed_consensus"]

    queries = ["query A", "query B"]

    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Reinforcement Learning from Human Feedback (RLHF) aligns language models using learned reward model optimization.\n"
        "Direct Preference Optimization (DPO) directly parameterizes implicit reward formulations from preference pairs.\n"
        "Evaluation benchmarks show win-rate improvements on MT-Bench and AlpacaEval.\n"
        "Training stability is governed by KL divergence constraints against policy drift.\n"
    )

    cov = audit_report_dimension_coverage(
        report,
        required_dimensions=req_dims,
        topic=topic,
        planned_queries=queries,
    )

    assert cov.total_dimensions == len(req_dims)
    assert "compute_efficiency" in cov.gap_dimensions
    assert "unmentioned_distributed_consensus" in cov.gap_dimensions
    assert cov.dimension_results["unmentioned_distributed_consensus"].is_covered is False
    assert cov.dimension_results["unmentioned_distributed_consensus"].status == "gap"
    assert cov.gap_dimensions_count >= 2
    assert "rlhf_architecture" in cov.covered_dimensions
    assert "dpo_architecture" in cov.covered_dimensions


def test_phase2k_callers_without_explicit_required_dimensions_retain_behavior():
    """Phase 2K: Callers without explicit required dimensions retain existing query- or topic-based derivation."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    report = (
        "# Research Summary: Custom Topic\n\n"
        "First query aspect is covered in detail here with many mentions.\n"
        "Second query aspect is also covered in detail here with multiple mentions.\n"
    )
    queries = [
        "first query aspect investigation",
        "second query aspect investigation",
        "third query aspect investigation",
    ]

    cov = audit_report_dimension_coverage(
        report,
        required_dimensions=None,
        topic="Custom Topic",
        planned_queries=queries,
    )
    assert cov.total_dimensions == 3
    dim_keys = list(cov.dimension_results.keys())
    assert dim_keys[0].startswith("dim_1_")
    assert dim_keys[1].startswith("dim_2_")
    assert dim_keys[2].startswith("dim_3_")


def test_phase2k_evidence_limited_dimensions_distinguished_from_full():
    """Phase 2K: Evidence-limited dimensions are marked with coverage_level='evidence_limited' rather than full."""
    from research_crew.evidence.validator import audit_report_dimension_coverage

    topic = "Reinforcement Learning from Human Feedback (RLHF) vs Direct Preference Optimization (DPO): Compare alignment quality, training stability, computational cost, and evaluation benchmarks"
    req_dims = ["training_stability_collapse", "compute_efficiency"]

    report = (
        f"# Research Summary: {topic}\n\n"
        "## Core Foundations & Mechanism\n"
        "Training stability is maintained with KL divergence regularization. High training stability prevents collapse.\n"
        "Evidence regarding computational cost and GPU hours is limited in the retrieved literature; empirical compute was not reported in the evaluated sources.\n"
    )

    cov = audit_report_dimension_coverage(
        report,
        required_dimensions=req_dims,
        topic=topic,
    )

    assert cov.total_dimensions == 2
    assert cov.dimension_results["training_stability_collapse"].is_covered is True
    assert cov.dimension_results["training_stability_collapse"].coverage_level == "full"

    assert cov.dimension_results["compute_efficiency"].is_covered is True
    assert cov.dimension_results["compute_efficiency"].coverage_level == "evidence_limited"


# ---------------------------------------------------------------------------
# Phase 2V.5: metric context consistency
# ---------------------------------------------------------------------------

def _phase2v5_metric_table(metric: str, value: str, baseline: str, source: str) -> str:
    return (
        "# Results\n\n"
        "| Metric | Value | Context & Comparison Baseline | Source |\n"
        "|---|---|---|---|\n"
        f"| {metric} | {value} | {baseline} | {source} |\n"
    )


def test_phase2v5_metric_wrong_dataset_size_flagged_as_mismatch(tmp_path):
    """A traceable value must still be flagged when its claimed dataset size conflicts with the source."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10001"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "On the 10M sample dataset, the model achieves 92% accuracy.")
    ))

    report = _phase2v5_metric_table("Accuracy", "92%", "100M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("conflicts with source context sizes" in w for w in row.warnings)


def test_phase2v5_metric_value_only_appears_for_different_metric_flagged(tmp_path):
    """A number traceable in source must not pass relevance when it appears only in a different metric context."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10002"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            url,
            "Model A achieves 94.2% accuracy on 10M samples. "
            "The system also reports an inference latency of 12 ms under load.",
        )
    ))

    report = _phase2v5_metric_table("Latency", "94.2 ms", "Model A", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("accuracy" in w.lower() for w in row.warnings)


def test_phase2v5_metric_correct_dataset_size_preserved(tmp_path):
    """A matching dataset size and metric should remain supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10003"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "On the 100M sample dataset, the model achieves 92% accuracy.")
    ))

    report = _phase2v5_metric_table("Accuracy", "92%", "100M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True


def test_phase2v5_metric_missing_dataset_size_reports_uncertainty(tmp_path):
    """When the source lacks dataset-size context, do not invent a contradiction."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10004"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The model achieves 92% accuracy.")
    ))

    report = _phase2v5_metric_table("Accuracy", "92%", "100M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert not any("dataset size" in w.lower() for w in row.warnings)


def test_phase2v5_metric_multiple_dataset_sizes_associates_correctly(tmp_path):
    """A source with multiple dataset sizes should allow the row's claimed size to match its local context."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10005"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            url,
            "On 10M samples, accuracy is 91%. On 100M samples, accuracy is 92%.",
        )
    ))

    report = _phase2v5_metric_table("Accuracy", "92%", "100M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 2: F1 fix — order-independent numeric context attribution
# ---------------------------------------------------------------------------

_PHASE2V6_CONTEXT_A = (
    "At 12 layers, the model achieves 85% accuracy. Inference latency is 12 ms."
)
_PHASE2V6_CONTEXT_B = (
    "Inference latency is 12 ms. At 12 layers, the model achieves 85% accuracy."
)


def test_phase2v6_number_in_foreign_and_matching_context_no_mismatch(tmp_path):
    """A value appearing in both a foreign and a matching metric context must not be flagged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10006"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, _PHASE2V6_CONTEXT_A)
    ))

    report = _phase2v5_metric_table("Latency", "12 ms", "Model A", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True
    assert not any("Number appears in source context" in w for w in row.warnings)


def test_phase2v6_reversed_sentence_order_same_verdict(tmp_path):
    """The verdict must be independent of sentence order for identical source content."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10007"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, _PHASE2V6_CONTEXT_B)
    ))

    report = _phase2v5_metric_table("Latency", "12 ms", "Model A", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True
    assert not any("Number appears in source context" in w for w in row.warnings)


def test_phase2v6_number_only_in_foreign_context_still_flagged(tmp_path):
    """A value whose only source context belongs to a different metric must still be flagged."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10008"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "At 12 layers, the model achieves 85% accuracy.")
    ))

    report = _phase2v5_metric_table("Latency", "12 ms", "Model A", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("Number appears in source context" in w for w in row.warnings)


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 3: F2 fix — shared trailing units in ranges/enumerations
# ---------------------------------------------------------------------------

def test_phase2v6_step3_range_shared_unit_associates_both_values():
    """'between 10M and 100M samples' must associate both magnitudes with the sample unit."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    contexts = _extract_dataset_size_contexts(
        "We evaluate on between 10M and 100M samples. Accuracy is 92%."
    )

    assert ("sample", 10_000_000.0) in contexts
    assert ("sample", 100_000_000.0) in contexts
    assert not any(unit.startswith("_") for unit, _ in contexts)


def test_phase2v6_step3_enumeration_shared_unit_associates_all_values():
    """'10M, 50M, and 100M vectors' must associate all three magnitudes with the vector unit."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    contexts = _extract_dataset_size_contexts(
        "Indexes were built over 10M, 50M, and 100M vectors."
    )

    assert ("vector", 10_000_000.0) in contexts
    assert ("vector", 50_000_000.0) in contexts
    assert ("vector", 100_000_000.0) in contexts
    assert not any(unit.startswith("_") for unit, _ in contexts)


def test_phase2v6_step3_word_form_range_shared_unit():
    """'10 million to 100 million samples' must associate both magnitudes with the sample unit."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    contexts = _extract_dataset_size_contexts(
        "Training used 10 million to 100 million samples."
    )

    assert ("sample", 10_000_000.0) in contexts
    assert ("sample", 100_000_000.0) in contexts
    assert not any(unit.startswith("_") for unit, _ in contexts)


def test_phase2v6_step3_no_unit_propagation_across_unrelated_contexts():
    """Units must not be inferred across sentence boundaries or unrelated intervening words."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    cross_sentence = _extract_dataset_size_contexts(
        "We used 10M for pretraining. The final model saw 100M samples."
    )
    assert ("_million", 10_000_000.0) in cross_sentence
    assert ("sample", 10_000_000.0) not in cross_sentence

    intervening_words = _extract_dataset_size_contexts("10M widgets and 100M samples")
    assert ("_million", 10_000_000.0) in intervening_words
    assert ("sample", 10_000_000.0) not in intervening_words

    standalone = _extract_dataset_size_contexts(
        "On the 10M sample dataset, the model achieves 92% accuracy."
    )
    assert standalone == [("sample", 10_000_000.0)]


def test_phase2v6_step3_range_context_claim_not_flagged_end_to_end(tmp_path):
    """A claimed size present in a source range must not be flagged as a dataset-size mismatch."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10009"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            url,
            "We evaluate on between 10M and 100M samples. Accuracy is 92%.",
        )
    ))

    report = _phase2v5_metric_table("Accuracy", "92%", "10M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert not any("conflicts with source context sizes" in w for w in row.warnings)


def test_phase2v6_step3_absent_size_in_range_context_still_flagged(tmp_path):
    """A claimed size absent from an unambiguous range context must still trigger the mismatch."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10010"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            url,
            "We evaluate on between 10M and 100M samples. Accuracy is 92%.",
        )
    ))

    report = _phase2v5_metric_table("Accuracy", "92%", "50M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("conflicts with source context sizes" in w for w in row.warnings)


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 5: F3 fix — dense-sentence metric attribution
# ---------------------------------------------------------------------------

def _phase2v6_step5_metric_row(metric: str, value: str, source_url: str) -> str:
    return _phase2v5_metric_table(metric, value, "Model A", source_url)


def test_phase2v6_step5_dense_sentence_wrong_metric_flagged(tmp_path):
    """A number tied to accuracy must not pass as latency merely because both names appear nearby."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10011"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "We report 94.2% accuracy and 12 ms latency.")
    ))

    report = _phase2v6_step5_metric_row("Latency", "94.2 ms", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("accuracy" in w.lower() for w in row.warnings)


def test_phase2v6_step5_dense_sentence_legit_latency_supported(tmp_path):
    """The actual latency value in the same dense sentence must remain supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10012"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "We report 94.2% accuracy and 12 ms latency.")
    ))

    report = _phase2v6_step5_metric_row("Latency", "12 ms", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True


def test_phase2v6_step5_dense_sentence_legit_accuracy_supported(tmp_path):
    """The actual accuracy value must remain supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10013"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "We report 94.2% accuracy and 12 ms latency.")
    ))

    report = _phase2v6_step5_metric_row("Accuracy", "94.2%", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True


def test_phase2v6_step5_reversed_dense_sentence_same_verdicts(tmp_path):
    """Metric attribution must be order-independent in dense sentences."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10014"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "We report 12 ms latency and 94.2% accuracy.")
    ))

    report = _phase2v6_step5_metric_row("Latency", "94.2 ms", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "possible_mismatch"
    assert any("accuracy" in w.lower() for w in audit.rows[0].warnings)

    report = _phase2v6_step5_metric_row("Latency", "12 ms", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "supported_by_heuristic"

    report = _phase2v6_step5_metric_row("Accuracy", "94.2%", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "supported_by_heuristic"


def test_phase2v6_step5_respectively_paired_metrics_supported(tmp_path):
    """Respectively-paired metrics must keep the correct value associations supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10015"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "Accuracy and latency are 94.2% and 12 ms, respectively.")
    ))

    report = _phase2v6_step5_metric_row("Accuracy", "94.2%", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "supported_by_heuristic"
    assert audit.rows[0].is_full_text_verified is True

    report = _phase2v6_step5_metric_row("Latency", "12 ms", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "supported_by_heuristic"
    assert audit.rows[0].is_full_text_verified is True


def test_phase2v6_step5_respectively_wrong_metric_still_flagged(tmp_path):
    """Respectively-paired metrics must still reject an explicitly crossed value association."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10016"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "Accuracy and latency are 94.2% and 12 ms, respectively.")
    ))

    report = _phase2v6_step5_metric_row("Latency", "94.2 ms", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "possible_mismatch"
    assert any("accuracy" in w.lower() for w in audit.rows[0].warnings)


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 7: F4 fix — unit-abbreviation normalization (hr/param)
# ---------------------------------------------------------------------------

def test_phase2v6_step7_canonical_unit_abbreviations_normalize():
    """Bare abbreviations must canonicalize like their plural/full forms."""
    from research_crew.evidence.validator import _canonical_dataset_size_unit

    assert _canonical_dataset_size_unit("hr", None) == "hour"
    assert _canonical_dataset_size_unit("hrs", None) == "hour"
    assert _canonical_dataset_size_unit("hour", None) == "hour"
    assert _canonical_dataset_size_unit("hours", None) == "hour"
    assert _canonical_dataset_size_unit("param", None) == "parameter"
    assert _canonical_dataset_size_unit("params", None) == "parameter"
    assert _canonical_dataset_size_unit("parameter", None) == "parameter"
    assert _canonical_dataset_size_unit("parameters", None) == "parameter"


def test_phase2v6_step7_abbreviated_units_extract_normalized():
    """Abbreviated dataset-size mentions must extract with the canonical full-form unit."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    assert _extract_dataset_size_contexts("The model trains for 24 hr.") == [("hour", 24.0)]
    assert _extract_dataset_size_contexts("The model trains for 24 hours.") == [("hour", 24.0)]
    assert _extract_dataset_size_contexts("The model has 1B param.") == [("parameter", 1000000000.0)]
    assert _extract_dataset_size_contexts("The model has 1B parameters.") == [("parameter", 1000000000.0)]


def test_phase2v6_step7_hr_mischaracterization_flagged(tmp_path):
    """An abbreviated dataset-scale value must be flagged like its full form."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10017"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The model trains for 24 hours. Accuracy is 92%.")
    ))

    report = _phase2v6_step5_metric_row("Accuracy", "24 hr", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("mischaracterized" in w for w in row.warnings)


def test_phase2v6_step7_param_mischaracterization_flagged(tmp_path):
    """An abbreviated parameter-count value must be flagged like its full form."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10018"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The model contains 1B parameters in its vocabulary. Accuracy is 92%.")
    ))

    report = _phase2v6_step5_metric_row("Accuracy", "1B param", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("mischaracterized" in w for w in row.warnings)


def test_phase2v6_step7_cross_unit_abbreviation_conflict_flagged(tmp_path):
    """A size conflicting with same-canonical-unit source values must be flagged via abbreviations too."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10019"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            url,
            "The model has 2B parameters and was trained for 24 hours of speech.",
        )
    ))

    report = _phase2v6_step5_metric_row("Model size", "24 param", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("conflicts with source context sizes" in w for w in row.warnings)


def test_phase2v6_step7_matching_abbreviated_units_still_supported(tmp_path):
    """Legitimate rows whose abbreviated units match the source must remain supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10020"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(
            url,
            "The model trains for 24 hours of speech data and contains 1B parameters in total.",
        )
    ))

    report = _phase2v6_step5_metric_row("Training hours", "24 hr", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "supported_by_heuristic"

    report = _phase2v6_step5_metric_row("Model size", "1B param", url)
    audit = audit_report_metrics(report, store)
    assert audit.rows[0].relevance_status == "supported_by_heuristic"


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 9: F5 fix — label-first dataset-size extraction
# ---------------------------------------------------------------------------

def test_phase2v6_step9_label_first_extraction_normalized():
    """Label-first dataset-size mentions must extract with the canonical unit."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    assert _extract_dataset_size_contexts("Sample count: 10M") == [("sample", 10_000_000.0)]
    assert _extract_dataset_size_contexts("Training hours: 24") == [("hour", 24.0)]
    assert _extract_dataset_size_contexts("Parameter count: 1B") == [("parameter", 1_000_000_000.0)]


def test_phase2v6_step9_number_first_and_non_size_controls_preserved():
    """Number-first forms, non-size labels, and unsafe label-first units keep prior behavior."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    assert _extract_dataset_size_contexts("The dataset contains 10M samples.") == [("sample", 10_000_000.0)]
    assert _extract_dataset_size_contexts("The model has 1B parameters.") == [("parameter", 1_000_000_000.0)]
    assert _extract_dataset_size_contexts("Memory: 64 GB") == []
    assert _extract_dataset_size_contexts("Example: 10M parameters were used.") == [("parameter", 10_000_000.0)]
    assert _extract_dataset_size_contexts("Sample count: 10M samples") == [("sample", 10_000_000.0)]


def test_phase2v6_step9_label_first_mismatch_flagged_end_to_end(tmp_path):
    """A claim conflicting with a label-first source count must be flagged, not supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10021"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "Sample count: 10M. The model uses 50 layers.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "50M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("conflicts with source context sizes" in w for w in row.warnings)


def test_phase2v6_step9_label_first_matching_claim_supported(tmp_path):
    """A claim matching the label-first source count must remain supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10022"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "Sample count: 10M. The model uses 50 layers.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "10M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert row.is_full_text_verified is True


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 11: F6 fix — human-readable dataset-size warning formatting
# ---------------------------------------------------------------------------

def test_phase2v6_step11_large_size_warning_comma_formatted(tmp_path):
    """Large conflicting sizes must render with separators, never scientific notation."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10031"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "Sample count: 10M. The model uses 50 layers.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "50M samples", url)
    audit = audit_report_metrics(report, store)

    assert audit.total_metrics_found == 1
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    warning = next(w for w in row.warnings if "conflicts with source context sizes" in w)
    assert "50,000,000 sample" in warning
    assert "10,000,000 sample" in warning
    assert "e+" not in warning
    assert "E+" not in warning


def test_phase2v6_step11_integer_and_decimal_warnings_keep_values(tmp_path):
    """Integer and decimal conflict values must stay exact and readable in warnings."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.10032"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "Training hours: 12. The model uses 24 layers.")
    ))

    report = _phase2v6_step5_metric_row("Training hours", "24 hr", url)
    audit = audit_report_metrics(report, store)
    row = audit.rows[0]
    assert row.relevance_status == "possible_mismatch"
    warning = next(w for w in row.warnings if "conflicts with source context sizes" in w)
    assert "24 hour" in warning
    assert "12 hour" in warning
    assert "e+" not in warning

    decimal_dir = tmp_path / "decimal"
    decimal_dir.mkdir()
    decimal_store = EvidenceStore.create(output_dir=decimal_dir)
    decimal_url = "https://arxiv.example/abs/2407.10033"
    decimal_store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(decimal_url, "Training hours: 12.5. The model uses 24.5 layers.")
    ))

    decimal_report = _phase2v6_step5_metric_row("Training hours", "24.5 hr", decimal_url)
    decimal_audit = audit_report_metrics(decimal_report, decimal_store)
    decimal_row = decimal_audit.rows[0]
    assert decimal_row.relevance_status == "possible_mismatch"
    decimal_warning = next(
        w for w in decimal_row.warnings if "conflicts with source context sizes" in w
    )
    assert "24.5 hour" in decimal_warning
    assert "12.5 hour" in decimal_warning
    assert "e+" not in decimal_warning


def test_phase2v6_step11_format_size_value_helper():
    """The formatter uses separators for large values and keeps decimals exact."""
    from research_crew.evidence.validator import _format_size_value

    assert _format_size_value(24.0) == "24"
    assert _format_size_value(24.5) == "24.5"
    assert _format_size_value(1.5) == "1.5"
    assert _format_size_value(0.0) == "0"
    assert _format_size_value(999_999.0) == "999,999"
    assert _format_size_value(10_000_000.0) == "10,000,000"
    assert _format_size_value(50_000_000.0) == "50,000,000"
    assert _format_size_value(1_000_000_000.0) == "1,000,000,000"
    assert _format_size_value(1_234_567.89) == "1,234,567.89"
    assert _format_size_value(1_234_567_890_123.456) == "1,234,567,890,123.456"
    assert _format_size_value(-5.0) == "-5"
    assert _format_size_value(-24.5) == "-24.5"
    for formatted in (_format_size_value(v) for v in (1e16, 1e21)):
        assert "e+" not in formatted
        assert "E+" not in formatted


def test_phase2v6_step11_extraction_and_verdicts_unaffected():
    """Display formatting must not alter extraction results or verdict inputs."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts

    assert _extract_dataset_size_contexts("Sample count: 10M") == [("sample", 10_000_000.0)]
    assert _extract_dataset_size_contexts("Training hours: 24") == [("hour", 24.0)]
    assert _extract_dataset_size_contexts("Parameter count: 1B") == [("parameter", 1_000_000_000.0)]
    assert _extract_dataset_size_contexts("between 10M and 100M samples") == [
        ("sample", 10_000_000.0),
        ("sample", 100_000_000.0),
    ]
    assert _extract_dataset_size_contexts("Memory: 64 GB") == []


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 13: Scientific-notation parsing fix
# ---------------------------------------------------------------------------

def test_phase2v6_step13_number_first_scientific_notation_extracts():
    """Number-first dataset-size mentions with scientific notation parse to full magnitudes."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts as ex

    assert ex("The dataset contains 1.5e+08 tokens.") == [("token", 150_000_000.0)]
    assert ex("The dataset contains 1e8 tokens.") == [("token", 100_000_000.0)]
    assert ex("The dataset contains 5e+07 samples.") == [("sample", 50_000_000.0)]
    assert ex("The dataset contains 1.5E+08 tokens.") == [("token", 150_000_000.0)]
    assert ex("The dataset contains 1.5e08 tokens.") == [("token", 150_000_000.0)]
    assert ex("The dataset contains 5e-07 tokens.") == [("token", 5e-7)]


def test_phase2v6_step13_label_first_scientific_notation_extracts():
    """Label-first forms with scientific notation parse to full magnitudes."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts as ex

    assert ex("Sample count: 1.5e+08") == [("sample", 150_000_000.0)]
    assert ex("Sample count: 1e8") == [("sample", 100_000_000.0)]
    assert ex("Sample count: 5e+07") == [("sample", 50_000_000.0)]
    assert ex("Sample count: 1.5e+08 samples") == [("sample", 150_000_000.0)]


def test_phase2v6_step13_malformed_scientific_notation_uncertain():
    """Malformed scientific notation must not be partially matched as a valid value."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts as ex

    assert ex("The dataset contains 1.5e+ tokens.") == []
    assert ex("The dataset contains 5e+ samples.") == []
    assert ex("Sample count: 1.5e+") == []
    assert ex("The dataset contains e+08 tokens.") == []


def test_phase2v6_step13_normalize_tokens_treats_sci_notation_as_one():
    """Scientific notation normalizes to the same plain decimal token as its equivalent integer."""
    from research_crew.evidence.validator import _normalize_numeric_tokens

    assert _normalize_numeric_tokens("1.5e+08 tokens") == ["150000000"]
    assert _normalize_numeric_tokens("1e8") == ["100000000"]
    assert _normalize_numeric_tokens("5e+07") == ["50000000"]
    assert _normalize_numeric_tokens("5e-07") == ["0.0000005"]
    assert _normalize_numeric_tokens("1,500,000 tokens") == ["1500000"]
    assert _normalize_numeric_tokens("1.5M tokens") == ["1.5"]
    assert _normalize_numeric_tokens("10M samples") == ["10"]


def test_phase2v6_step13_extract_all_number_spans_no_fragments():
    """Number spans must expose a scientific literal as one normalized value, not fragments."""
    from research_crew.evidence.validator import _extract_all_number_spans

    spans = _extract_all_number_spans("The dataset contains 1.5e+08 tokens.")
    norms = [norm for _, _, norm in spans]
    assert "150000000" in norms
    assert "1.5" not in norms
    assert "08" not in norms


def test_phase2v6_step13_end_to_end_plain_claim_vs_sci_source(tmp_path):
    """A correct plain-notation claim against a sci-notation source must be supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.30001"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 1.5e+08 tokens.")
    ))
    report = _phase2v6_step5_metric_row("Token count", "150000000 tokens", url)
    audit = audit_report_metrics(report, store)
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"


def test_phase2v6_step13_end_to_end_sci_claim_vs_plain_source(tmp_path):
    """A correct scientific-notation claim against a plain-integer source must be supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.30002"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 150000000 tokens.")
    ))
    report = _phase2v6_step5_metric_row("Token count", "1.5e+08 tokens", url)
    audit = audit_report_metrics(report, store)
    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"


def test_phase2v6_step13_end_to_end_exponent_digit_claim_not_supported(tmp_path):
    """Claiming only the exponent digits must not be supported against a sci-notation source."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.30003"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 1.5e+08 tokens.")
    ))
    report = _phase2v6_step5_metric_row("Token count", "08 tokens", url)
    audit = audit_report_metrics(report, store)
    row = audit.rows[0]
    assert row.relevance_status != "supported_by_heuristic"


def test_phase2v6_step13_end_to_end_mantissa_fragment_cross_metric_not_supported(tmp_path):
    """A mantissa fragment from sci notation must not make an unrelated metric traceable."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.30004"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 1.5e+08 tokens.")
    ))
    report = _phase2v6_step5_metric_row("Training hours", "1.5 hr", url)
    audit = audit_report_metrics(report, store)
    row = audit.rows[0]
    assert row.relevance_status != "supported_by_heuristic"


def test_phase2v6_step13_preserves_existing_parsing(tmp_path):
    """Existing number-first, label-first, suffix, decimal, and abbreviation parsing remain intact."""
    from research_crew.evidence.validator import _extract_dataset_size_contexts as ex

    assert ex("between 10M and 100M samples") == [
        ("sample", 10_000_000.0),
        ("sample", 100_000_000.0),
    ]
    assert ex("The model trains for 24 hr.") == [("hour", 24.0)]
    assert ex("The model has 1B param.") == [("parameter", 1_000_000_000.0)]
    assert ex("Sample count: 10M") == [("sample", 10_000_000.0)]
    assert ex("Memory: 64 GB") == []
    assert ex("The model trained for 24.5 hours.") == [("hour", 24.5)]
    assert ex("The dataset contains 1,500,000 tokens.") == [("token", 1_500_000.0)]
    assert ex("The dataset contains 1.5 million tokens.") == [("token", 1_500_000.0)]


# ---------------------------------------------------------------------------
# Phase 2V.6 Step 15: F7 fix — implied-unit binding for bare-magnitude claims
# ---------------------------------------------------------------------------

def test_phase2v6_step15_bare_sample_claim_vs_concrete_source_flagged(tmp_path):
    """A bare 100M sample claim must conflict with a source documenting 10M samples."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50011"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 10M samples and 100 layers.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any(
        "100,000,000" in w and "10,000,000" in w and "conflicts with source context sizes" in w
        for w in row.warnings
    )


def test_phase2v6_step15_bare_token_claim_vs_concrete_source_flagged(tmp_path):
    """A bare 100M token claim must conflict with a source documenting 10M tokens."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50012"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 10M tokens and 100 layers.")
    ))

    report = _phase2v6_step5_metric_row("Number of tokens", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any(
        "100,000,000" in w and "10,000,000" in w and "conflicts with source context sizes" in w
        for w in row.warnings
    )


def test_phase2v6_step15_explicit_unit_claim_flagged_unchanged(tmp_path):
    """Claims carrying an explicit unit word keep the existing conflict behavior."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50013"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 10M samples and 100 layers.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "100M samples", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any("conflicts with source context sizes" in w for w in row.warnings)


def test_phase2v6_step15_matching_bare_claim_supported(tmp_path):
    """A correct bare claim equal to the source sample size must remain supported."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50014"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 100M samples.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"


def test_phase2v6_step15_bare_versus_bare_conflict_unchanged(tmp_path):
    """Bare claim versus bare source values keeps the existing conflict detection."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50015"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 10M. The model uses 100 layers.")
    ))

    report = _phase2v6_step5_metric_row("Sample count", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any(
        "100,000,000" in w and "10,000,000" in w and "_million" in w
        and "conflicts with source context sizes" in w
        for w in row.warnings
    )


def test_phase2v6_step15_no_decoy_traceability_unchanged(tmp_path):
    """Without a decoy occurrence, the numeric traceability gate still flags the claim."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50016"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 10M samples.")
    ))

    report = _phase2v6_step5_metric_row("Number of samples", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is False
    assert row.relevance_status == "possible_mismatch"
    assert any("not found in captured text" in w for w in row.warnings)


def test_phase2v6_step15_implied_unit_helper_ambiguity():
    """The helper returns a unit only for a unique canonical candidate."""
    from research_crew.evidence.validator import _implied_dataset_unit

    assert _implied_dataset_unit("Sample count") == "sample"
    assert _implied_dataset_unit("Number of samples") == "sample"
    assert _implied_dataset_unit("Number of parameters") == "parameter"
    assert _implied_dataset_unit("Training hours") == "hour"
    assert _implied_dataset_unit("Training hrs") == "hour"
    assert _implied_dataset_unit("Parameter count (params)") == "parameter"
    assert _implied_dataset_unit("Vectors") == "vector"

    assert _implied_dataset_unit("Samples and tokens") is None
    assert _implied_dataset_unit("Dataset scale") is None
    assert _implied_dataset_unit("Model size") is None


def test_phase2v6_step15_ambiguous_metric_no_inference(tmp_path):
    """An ambiguous metric label must not trigger arbitrary unit inference."""
    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50017"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The dataset contains 10M samples and 100 layers.")
    ))

    report = _phase2v6_step5_metric_row("Samples and tokens", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "supported_by_heuristic"
    assert not any("conflicts with source context sizes" in w for w in row.warnings)


def test_phase2v6_step15_abbreviation_units_bind(tmp_path):
    """F4 abbreviation normalization feeds the implied-unit binding."""
    from research_crew.evidence.validator import _implied_dataset_unit

    assert _implied_dataset_unit("Number of params") == "parameter"

    store = EvidenceStore.create(output_dir=tmp_path)
    url = "https://arxiv.example/abs/2407.50018"
    store.add(EvidenceRecord.from_tool_result(
        ToolFetchResult.success(url, "The model trains for 10M hr and uses 100 layers.")
    ))

    report = _phase2v6_step5_metric_row("Training hrs", "100M", url)
    audit = audit_report_metrics(report, store)

    row = audit.rows[0]
    assert row.is_traceable is True
    assert row.relevance_status == "possible_mismatch"
    assert any(
        "100,000,000" in w and "10,000,000" in w and "conflicts with source context sizes" in w
        for w in row.warnings
    )
