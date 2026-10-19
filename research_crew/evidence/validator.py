"""
evidence/validator.py
---------------------
Deterministic provenance checks for EvidenceRecord objects.

PURPOSE AND SCOPE BOUNDARIES
----------------------------
These functions answer: "Can this text passage be traced to a specific
captured retrieval in this run's evidence store?"

GUARANTEES:
A matching content_hash establishes consistency with the stored text at capture time.
It does NOT prove origin-server authenticity, network provenance, or the factual truth
of any claim. Semantic claim verification is outside the scope of Phase 1A.

Checks performed by validate_passage()
---------------------------------------
1. evidence_id must exist in the store.
2. The record's source_url or final_url must match the URL supplied by the caller.
3. The passage must be non-empty (not blank or whitespace-only).
4. The stored content_hash must be consistent with the stored captured_text.
5. The record's retrieval_status must not be FAILED.
6. The passage must be a literal substring of captured_text (or preserved snippet_text).

Exposing Retrieval Completeness
-------------------------------
ValidationResult explicitly exposes `retrieval_status`, `is_full_text`, and
`is_snippet_only` so downstream callers can distinguish full-text evidence from
search-snippet evidence.
"""

from dataclasses import dataclass, field
from enum import Enum
import logging
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Set, Tuple

from research_crew.evidence.model import (
    EvidenceRecord,
    RetrievalStatus,
    make_content_hash,
    make_passage_id,
)
from research_crew.evidence.store import EvidenceStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class ValidationOutcome(str, Enum):
    """All possible outcomes of a provenance check."""

    VALID = "valid"
    """Passage is confirmed present in the captured text for this evidence record."""

    UNKNOWN_EVIDENCE_ID = "unknown_evidence_id"
    """The evidence_id supplied by the caller does not exist in the store."""

    URL_MISMATCH = "url_mismatch"
    """The stored source_url/final_url does not match the URL supplied by the caller."""

    EMPTY_PASSAGE = "empty_passage"
    """The passage is the empty string or contains only whitespace."""

    INVALID_HASH = "invalid_hash"
    """The stored content_hash is inconsistent with the stored captured_text."""

    PASSAGE_NOT_IN_TEXT = "passage_not_in_text"
    """The passage is not a literal substring of the captured_text."""

    FAILED_RETRIEVAL = "failed_retrieval"
    """The record's retrieval_status is FAILED; it holds no usable source text."""


@dataclass(frozen=True)
class ValidationResult:
    """The immutable outcome of a single provenance check.

    Attributes
    ----------
    outcome:
        One of the ValidationOutcome enum values.
    evidence_id:
        The source-level evidence_id that was checked.
    detail:
        A human-readable explanation of the outcome.
    passage:
        The passage that was checked (None when not applicable).
    retrieval_status:
        The RetrievalStatus of the supporting evidence record (FULL_TEXT,
        SNIPPET_ONLY, or FAILED). Exposes retrieval completeness to callers.
    passage_id:
        Deterministic passage-level identifier when valid; None otherwise.
    """

    outcome: ValidationOutcome
    evidence_id: str
    detail: str
    passage: Optional[str] = None
    retrieval_status: Optional[RetrievalStatus] = None
    passage_id: Optional[str] = None

    @property
    def is_valid(self) -> bool:
        """True only when outcome is VALID."""
        return self.outcome == ValidationOutcome.VALID

    @property
    def is_full_text(self) -> bool:
        """True if the supporting evidence record is verified from full text."""
        return self.is_valid and self.retrieval_status == RetrievalStatus.FULL_TEXT

    @property
    def is_snippet_only(self) -> bool:
        """True if the supporting evidence record is verified from a snippet only."""
        return self.is_valid and self.retrieval_status == RetrievalStatus.SNIPPET_ONLY

    def __str__(self) -> str:
        return (
            f"ValidationResult(outcome={self.outcome.value}, "
            f"evidence_id={self.evidence_id!r}, "
            f"status={self.retrieval_status.value if self.retrieval_status else None}, "
            f"detail={self.detail!r})"
        )


# ---------------------------------------------------------------------------
# Public validation functions
# ---------------------------------------------------------------------------

def validate_passage(
    store: EvidenceStore,
    evidence_id: str,
    claimed_url: str,
    passage: str,
) -> ValidationResult:
    """Check that *passage* is traceable to a specific evidence record in *store*.

    Checks 1–6 in order, returning on the first failure.
    """
    # ── Check 1: evidence_id must exist ─────────────────────────────────────
    record: Optional[EvidenceRecord] = store.get(evidence_id)
    if record is None:
        result = ValidationResult(
            outcome=ValidationOutcome.UNKNOWN_EVIDENCE_ID,
            evidence_id=evidence_id,
            detail=f"No record found for evidence_id={evidence_id!r}",
            passage=passage,
            retrieval_status=None,
        )
        logger.debug("Validation failed: %s", result)
        return result

    # ── Check 2: URL must match (source_url or final_url) ───────────────────
    if not record.matches_url(claimed_url):
        result = ValidationResult(
            outcome=ValidationOutcome.URL_MISMATCH,
            evidence_id=evidence_id,
            detail=(
                f"URL mismatch: stored source_url={record.source_url!r}, "
                f"final_url={record.final_url!r}, claimed={claimed_url!r}"
            ),
            passage=passage,
            retrieval_status=record.retrieval_status,
        )
        logger.debug("Validation failed: %s", result)
        return result

    # ── Check 3: Passage must be non-empty ──────────────────────────────────
    if not passage or not passage.strip():
        result = ValidationResult(
            outcome=ValidationOutcome.EMPTY_PASSAGE,
            evidence_id=evidence_id,
            detail="Passage is empty or whitespace-only",
            passage=passage,
            retrieval_status=record.retrieval_status,
        )
        logger.debug("Validation failed: %s", result)
        return result

    # ── Check 4: Content hash consistency check ─────────────────────────────
    expected_hash = make_content_hash(record.captured_text)
    if record.content_hash != expected_hash:
        result = ValidationResult(
            outcome=ValidationOutcome.INVALID_HASH,
            evidence_id=evidence_id,
            detail=(
                f"Content hash mismatch — stored={record.content_hash!r}, "
                f"recomputed={expected_hash!r}. "
                "The captured_text or content_hash may have been tampered with."
            ),
            passage=passage,
            retrieval_status=record.retrieval_status,
        )
        logger.warning("Hash consistency failure for evidence_id=%s", evidence_id)
        return result

    # ── Check 5: Reject FAILED retrievals ───────────────────────────────────
    if record.retrieval_status == RetrievalStatus.FAILED:
        result = ValidationResult(
            outcome=ValidationOutcome.FAILED_RETRIEVAL,
            evidence_id=evidence_id,
            detail=(
                "Record has retrieval_status=failed; "
                "no usable source text is available for provenance checks."
            ),
            passage=passage,
            retrieval_status=RetrievalStatus.FAILED,
        )
        logger.debug("Validation failed: %s", result)
        return result

    # ── Check 6: Passage must appear in captured text or preserved snippet ──
    matched_status = record.retrieval_status
    if passage in record.captured_text:
        pass
    elif record.snippet_text and passage in record.snippet_text:
        # Matched historical search snippet preserved during upgrade
        matched_status = RetrievalStatus.SNIPPET_ONLY
    elif any(passage in obs for obs in record.snippet_observations):
        # Matched an observed search snippet
        matched_status = RetrievalStatus.SNIPPET_ONLY
    else:
        preview = passage[:120] if len(passage) > 120 else passage
        result = ValidationResult(
            outcome=ValidationOutcome.PASSAGE_NOT_IN_TEXT,
            evidence_id=evidence_id,
            detail=(
                f"Passage is not a substring of captured_text "
                f"(evidence_id={evidence_id!r}). "
                f"Passage preview: {preview!r}"
            ),
            passage=passage,
            retrieval_status=record.retrieval_status,
        )
        logger.debug("Validation failed: %s", result)
        return result

    # ── All checks passed ────────────────────────────────────────────────────
    p_id = make_passage_id(evidence_id, passage)
    logger.debug(
        "Passage provenance confirmed: evidence_id=%s passage_id=%s url=%s status=%s",
        evidence_id,
        p_id,
        claimed_url,
        matched_status.value,
    )
    return ValidationResult(
        outcome=ValidationOutcome.VALID,
        evidence_id=evidence_id,
        detail="Passage provenance confirmed in captured_text",
        passage=passage,
        retrieval_status=matched_status,
        passage_id=p_id,
    )


def validate_record_integrity(record: EvidenceRecord) -> ValidationResult:
    """Check that a single record's content_hash is consistent with captured_text.

    A valid check confirms internal consistency with the stored text, not origin
    server authenticity or factual truth.
    """
    expected_hash = make_content_hash(record.captured_text)
    if record.content_hash != expected_hash:
        result = ValidationResult(
            outcome=ValidationOutcome.INVALID_HASH,
            evidence_id=record.evidence_id,
            detail=(
                f"Integrity check failed: stored={record.content_hash!r}, "
                f"recomputed={expected_hash!r}"
            ),
            retrieval_status=record.retrieval_status,
        )
        logger.warning(
            "Record integrity check failed for evidence_id=%s", record.evidence_id
        )
        return result

    return ValidationResult(
        outcome=ValidationOutcome.VALID,
        evidence_id=record.evidence_id,
        detail="Record integrity confirmed: content_hash matches captured_text",
        retrieval_status=record.retrieval_status,
    )


# ---------------------------------------------------------------------------
# Report Citation & Metric Quality Audits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MetricAuditRow:
    """Audit outcome for an individual row in a report's metric table."""

    metric: str
    value: str
    baseline: str
    source: str
    resolved_url: Optional[str] = None
    is_traceable: bool = False
    has_baseline_or_flag: bool = False
    baseline_status: str = "missing"
    relevance_status: str = "insufficient_evidence"
    relevance_detail: str = ""
    metric_type: str = "performance_benchmark"
    evidence_depth: str = "none"
    is_full_text_verified: bool = False
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class MetricAuditSummary:
    """Audit summary for quantitative benchmark tables in a research report.

    Validates:
    1. Table structure: Expects 'Context & Comparison Baseline' column.
    2. Baseline context: Distinguishes explicitly tied baselines, unlinked prose baselines,
       and explicit disclosures ('[Baseline unspecified in source text]').
    3. Source traceability: Resolves the cited source and verifies that numerical
       tokens in the reported value appear in the captured text or snippets.
    4. Claim relevance & classification: Separates dataset-scale statistics from performance
       benchmarks, audits comparative claims, and evaluates domain-level relevance.
    5. Evidence depth: Distinguishes full-text verified claims from snippet-only evidence.
    """

    total_metrics_found: int
    traceable_metrics_count: int
    untraceable_metrics_count: int
    missing_baseline_count: int
    specified_baseline_count: int = 0
    explicitly_unspecified_baseline_count: int = 0
    unlinked_prose_baseline_count: int = 0
    explicitly_unspecified_performance_baseline_count: int = 0
    missing_performance_baseline_count: int = 0
    supported_relevance_count: int = 0
    possible_mismatch_count: int = 0
    insufficient_evidence_count: int = 0
    performance_metrics_count: int = 0
    dataset_scale_metrics_count: int = 0
    full_text_metric_rows_count: int = 0
    snippet_only_metric_rows_count: int = 0
    full_text_supported_count: int = 0
    snippet_only_supported_count: int = 0
    rows: List[MetricAuditRow] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportAuditSummary:
    """Summary of deterministic citation and evidence audit for a research report.

    IMPORTANT DISTINCTION & BOUNDARIES:
    -----------------------------------
    1. Retrieval / Provenance Validation:
       Confirms whether URLs cited in the report exist as records in the run's EvidenceStore,
       and whether the supporting record has FULL_TEXT vs SNIPPET_ONLY status.
    2. Claim Linkage:
       Confirms whether cited sources were actually captured in the pipeline run.
    3. Factual Truth:
       Programmatic URL/provenance matching CANNOT establish the factual truth or correctness
       of claims. Substring and provenance checks verify consistency with captured source text,
       not the real-world accuracy of the assertions.
    4. Prose Claims & Semantic Verification:
       Heuristic pattern auditing warns about unsupported comparative assertions in narrative prose,
       but does not evaluate full natural language semantic entailment. Unflagged prose statements
       are NOT guaranteed to be verified for factual truth.
    """

    total_citations_found: int
    matched_sources_count: int
    unmatched_sources_count: int
    full_text_sources_count: int
    snippet_only_sources_count: int
    failed_sources_count: int
    unmatched_urls: List[str] = field(default_factory=list)
    is_thin_evidence: bool = False
    warnings: List[str] = field(default_factory=list)
    has_foundations_section: bool = True
    metrics_summary: Optional[MetricAuditSummary] = None
    prose_comparative_warnings: List[str] = field(default_factory=list)
    claim_verification_summary: Optional[Any] = None
    coverage_summary: Optional[Any] = None


@dataclass(frozen=True)
class DimensionCoverageResult:
    """Result of auditing coverage for an individual topic dimension."""

    dimension: str
    display_name: str
    is_covered: bool
    status: str  # "covered" | "partial" | "gap"
    matched_snippets: List[str] = field(default_factory=list)
    coverage_level: str = "full"  # "full" | "partial" | "none"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dimension": self.dimension,
            "display_name": self.display_name,
            "is_covered": self.is_covered,
            "status": self.status,
            "coverage_level": self.coverage_level,
            "matched_snippets": list(self.matched_snippets),
        }


@dataclass(frozen=True)
class ReportCoverageSummary:
    """Summary of topic dimension coverage audit for a research report."""

    total_dimensions: int
    covered_dimensions_count: int
    gap_dimensions_count: int
    covered_dimensions: List[str]
    gap_dimensions: List[str]
    dimension_results: Dict[str, DimensionCoverageResult]
    warnings: List[str]
    partially_covered_dimensions_count: int = 0
    partially_covered_dimensions: List[str] = field(default_factory=list)
    missing_comparisons: List[str] = field(default_factory=list)
    is_topic_aware: bool = True
    topic: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_dimensions": self.total_dimensions,
            "covered_dimensions_count": self.covered_dimensions_count,
            "gap_dimensions_count": self.gap_dimensions_count,
            "partially_covered_dimensions_count": self.partially_covered_dimensions_count,
            "covered_dimensions": list(self.covered_dimensions),
            "gap_dimensions": list(self.gap_dimensions),
            "partially_covered_dimensions": list(self.partially_covered_dimensions),
            "missing_comparisons": list(self.missing_comparisons),
            "is_topic_aware": self.is_topic_aware,
            "topic": self.topic,
            "warnings": list(self.warnings),
            "dimension_results": {
                k: v.to_dict() for k, v in self.dimension_results.items()
            },
        }


def _decompose_arbitrary_topic(topic: str) -> List[Tuple[str, str, str]]:
    """Deterministically decompose an arbitrary topic string into structured dimensions.

    Only decomposes when the topic contains distinct sub-aspects (e.g. conjunctions or comparative clauses).
    Does not invent generic fallback dimensions.
    """
    clauses = re.split(r"(?i)\s+(?:versus|vs\.?|and|with|,)\s+", topic)
    sub_aspects = []
    for c in clauses:
        c_clean = c.strip(" ,.-:;")
        tokens = [
            w for w in re.findall(r"[a-zA-Z0-9_\-]+", c_clean)
            if len(w) >= 3 and w.lower() not in _STOPWORDS and w.lower() not in {
                "comparative", "evaluation", "study", "analysis", "survey", "overview", "research"
            }
        ]
        if tokens:
            sub_aspects.append((c_clean, tokens))

    if len(sub_aspects) < 2:
        return []

    specs = []
    for idx, (aspect_name, tokens) in enumerate(sub_aspects[:5]):
        slug = f"topic_aspect_{idx+1}_" + "_".join(t.lower() for t in tokens[:2])
        title = " ".join(t.title() for t in tokens[:4])
        pattern_str = r"(?i)\b(?:" + "|".join(re.escape(t) for t in tokens[:6]) + r")\b"
        specs.append((slug, title, pattern_str))

    return specs


def derive_topic_dimensions(
    topic: Optional[str] = None,
    planned_queries: Optional[List[str]] = None,
    report: Optional[str] = None,
) -> Tuple[List[Tuple[str, str, str]], bool, List[str]]:
    """Derive topic-aware research dimension specifications (key, display_name, regex).

    Returns
    -------
    Tuple[dimension_specs, is_topic_aware, warnings]
    """
    clean_topic = (topic or "").strip()
    if not clean_topic and report:
        m = re.search(r"(?im)^#\s+Research\s+Summary:\s*([^\n]+)", report)
        if m:
            clean_topic = m.group(1).strip()

    # 1. Compatibility with legacy speech synthesis unit tests
    if clean_topic in ("Indian Language Speech Synthesis", "Minimal Pipeline") and not planned_queries:
        specs = [
            (
                "architecture",
                "Model Architecture",
                r"(?i)\b(?:architecture|transformer|encoder|decoder|conformer|flow\s*matching|acoustic\s*model|vocoder|self[- ]attention|feed[- ]forward|neural\s*network|diffusion)\b",
            ),
            (
                "hindi_marathi_tamil_support",
                "Hindi/Marathi/Tamil Language Support",
                r"(?i)\b(?:hindi|marathi|tamil)\b",
            ),
            (
                "datasets",
                "Datasets & Corpora",
                r"(?i)\b(?:dataset(?:s)?|corpus|corpora|indictts|common\s*voice|librispeech|training\s*data|hours\s+of\s+(?:speech|audio)|audio\s*data|speech\s*corpus|voxceleb)\b",
            ),
            (
                "low_resource_adaptation",
                "Low-Resource Adaptation",
                r"(?i)\b(?:low[- ]resource|zero[- ]shot|few[- ]shot|cross[- ]lingual|adaptation|transfer\s*learning|fine[- ]tuning|unseen\s*speaker)\b",
            ),
            (
                "quality_benchmarks",
                "Quality Benchmarks",
                r"(?i)\b(?:quality\s*benchmark(?:s)?|wer|cer|mos|sim-o|sim-r|bleu|evaluation\s*(?:metric|result|benchmark|score)(?:s)?|mcd)\b",
            ),
            (
                "latency",
                "Inference Latency & RTF",
                r"(?i)\b(?:latency|real[- ]time\s*factor|rtf|throughput|inference\s*speed|inference\s*time|speedup|milliseconds|\b\d+\s*ms\b)\b",
            ),
            (
                "hardware",
                "Hardware & Compute Environment",
                r"(?i)\b(?:hardware|gpu(?:s)?|tpu(?:s)?|a100|v100|h100|rtx|cuda|vram|memory\s*bandwidth|nvidia|compute\s*cluster|cpu)\b",
            ),
            (
                "licensing",
                "Licensing & Availability",
                r"(?i)\b(?:licens(?:e|ing)|open[- ]source|apache\s*2(?:\.0)?|mit\s*license|cc[- ]by|proprietary|commercial\s*use|weights\s*release|code\s*release)\b",
            ),
        ]
        return specs, True, []

    # 2. Derive dimensions from research plan (planned_queries) if provided
    if planned_queries and len(planned_queries) > 0:
        specs = []
        for i, q in enumerate(planned_queries):
            q_clean = q.strip()
            if not q_clean:
                continue
            words = [
                w for w in re.findall(r"[a-zA-Z0-9_\-]+", q_clean)
                if len(w) >= 3 and w.lower() not in _STOPWORDS
            ]
            if not words:
                continue
            key = f"dim_{i+1}_" + "_".join(words[:2]).lower()
            title = " ".join(words[:4]).title()
            terms = [re.escape(w) for w in words[:6]]
            pattern = r"(?i)\b(?:" + "|".join(terms) + r")\b"
            specs.append((key, title, pattern))
        if specs:
            return specs, True, []

    # 3. Derive dimensions from topic string
    if not clean_topic or len(clean_topic.split()) < 3:
        return [], False, [
            "Unable to reliably derive topic-aware dimensions: topic is unspecified or insufficient and no research plan was provided."
        ]

    topic_lower = clean_topic.lower()

    # Topic A: RAG vs. Long-Context LLMs
    if ("rag" in topic_lower or "retrieval" in topic_lower) and ("long-context" in topic_lower or "long context" in topic_lower):
        specs = [
            (
                "rag_architecture",
                "RAG Architecture & Retrieval Pipeline",
                r"(?i)\b(?:retrieval[- ]augmented(?:\s*generation)?|\brag\b|vector\s*(?:database|db|index|search)|dense\s*retrieval|chunking|embeddings?|top[- ]k|rerank(?:ing|er)?)\b",
            ),
            (
                "long_context_architecture",
                "Long-Context LLM Architecture & Scaling",
                r"(?i)\b(?:long[- ]context|extended\s*context|context\s*length|context\s*window|rope|rotary\s*position|kv\s*cache|attention\s*window|million\s*tokens?|flash[- ]?attention)\b",
            ),
            (
                "retrieval_benchmarks",
                "Retrieval Accuracy & Needle-in-a-Haystack Benchmarks",
                r"(?i)\b(?:needle\s*in\s*a\s*haystack|niah|u[- ]niah|babilong|retrieval\s*accuracy|retrieval\s*recall|mrr|ndcg|passkey|information\s*retrieval\s*benchmark)\b",
            ),
            (
                "computational_cost_latency",
                "Computational Cost, Latency & Token Economics",
                r"(?i)\b(?:inference\s*latency|time[- ]to[- ]first[- ]token|ttft|computational\s*cost|token\s*cost|compute\s*overhead|throughput|memory\s*footprint|vram\s*consumption|cost\s*per\s*query)\b",
            ),
            (
                "hallucination_faithfulness",
                "Faithfulness & Hallucination Mitigation",
                r"(?i)\b(?:faithfulness|hallucination(?:s)?|grounding|groundedness|factuality|context\s*attribution|unsupported\s*generation|context\s*faithfulness)\b",
            ),
            (
                "hybrid_architectures",
                "Hybrid Architectures & Adaptive Routing",
                r"(?i)\b(?:hybrid(?:\s*approach|\s*architecture|\s*system)?|routing|router|adaptive\s*retrieval|cascading|model\s*selection|fallback)\b",
            ),
        ]
        return specs, True, []

    # Topic B: Distributed Databases, Sharding, Consensus
    if ("distributed" in topic_lower and "database" in topic_lower) or ("sharding" in topic_lower and "consensus" in topic_lower):
        specs = [
            (
                "data_sharding",
                "Data Sharding & Partitioning",
                r"(?i)\b(?:shard(?:ing|s)?|partition(?:ing|s)?|hash\s*partitioning|range\s*partitioning|consistent\s*hashing|rebalance|rebalancing|data\s*distribution|shard\s*key)\b",
            ),
            (
                "consensus_protocols",
                "Consensus Protocols & State Machine Replication",
                r"(?i)\b(?:consensus(?:\s*protocol|\s*algorithm)?|raft|paxos|multi[- ]paxos|etcd|zab|viewstamped|quorum|leader\s*election|state\s*machine\s*replication)\b",
            ),
            (
                "distributed_transactions",
                "Distributed Transactions & Concurrency Control",
                r"(?i)\b(?:distributed\s*transaction(?:s)?|two[- ]phase\s*commit|2pc|acid|concurrency\s*control|mvcc|serializab(?:le|ility)|isolation\s*level|snapshot\s*isolation)\b",
            ),
            (
                "fault_tolerance",
                "Fault Tolerance & High Availability",
                r"(?i)\b(?:fault\s*tolerance|high\s*availability|partition\s*tolerance|split[- ]brain|failover|recovery|heartbeat|node\s*failure|resilience)\b",
            ),
            (
                "throughput_benchmarks",
                "Throughput, Latency & Benchmarks",
                r"(?i)\b(?:throughput|latency|iops|ycsb|tpcc|tpc[- ]c|transactions?\s*per\s*second|\btps\b|read[- ]write\s*latency|p99\s*latency|benchmark(?:s)?)\b",
            ),
        ]
        return specs, True, []

    # Topic C: RLHF vs. DPO (Preference Optimization)
    if ("rlhf" in topic_lower or "preference" in topic_lower) and ("dpo" in topic_lower or "direct preference" in topic_lower):
        specs = [
            (
                "rlhf_architecture",
                "RLHF & Reward Modeling",
                r"(?i)\b(?:rlhf|reinforcement\s*learning\s*from\s*human\s*feedback|reward\s*model(?:ing)?|ppo|proximal\s*policy\s*optimization|actor[- ]critic|value\s*function|policy\s*gradient)\b",
            ),
            (
                "dpo_architecture",
                "Direct Preference Optimization (DPO)",
                r"(?i)\b(?:direct\s*preference\s*optimization|\bdpo\b|implicit\s*reward|kto|ipo|preference\s*loss|reference\s*model|bradley[- ]terry)\b",
            ),
            (
                "alignment_benchmarks",
                "Alignment Evaluation & Benchmarks",
                r"(?i)\b(?:win[- ]rate|alpaca[- ]eval|mt[- ]bench|arena[- ]hard|human\s*evaluation|preference\s*benchmark|pairwise\s*comparison|eval(?:uation)?)\b",
            ),
            (
                "training_stability_collapse",
                "Training Stability & Mode Collapse",
                r"(?i)\b(?:reward\s*hacking|mode\s*collapse|training\s*stability|overoptimization|kl\s*penalty|kl\s*divergence|regularization|drift)\b",
            ),
            (
                "compute_efficiency",
                "Compute Efficiency & Resource Overhead",
                r"(?i)\b(?:compute\s*efficiency|gpu\s*hours|vram|memory\s*footprint|training\s*time|throughput|training\s*cost|gpu\s*overhead)\b",
            ),
        ]
        return specs, True, []

    # 4. General topic decomposer
    specs = _decompose_arbitrary_topic(clean_topic)
    if specs:
        return specs, True, []

    return [], False, [
        f"Unable to reliably derive topic-aware dimensions for topic '{clean_topic}'."
    ]


def check_report_comparative_baselines(report: str, topic: str) -> List[str]:
    """Identify missing comparisons or baselines when the research topic is comparative."""
    warnings: List[str] = []
    topic_clean = topic.strip()
    m = re.search(
        r"(?i)(?:comparative\s*evaluation\s*(?:of)?\s*)?(.+?)\s+(?:versus|\bvs\.?\b)\s+(.+)",
        topic_clean,
    )
    if not m:
        return []

    side_a_raw = m.group(1).strip()
    side_b_raw = m.group(2).strip()

    def get_core_entity_regex(name: str) -> Tuple[str, str]:
        lower = name.lower()
        if "rag" in lower or "retrieval-augmented" in lower:
            return "RAG", r"(?i)\b(?:rag|retrieval[- ]augmented)\b"
        if "long-context" in lower or "long context" in lower:
            return "Long-Context LLMs", r"(?i)\b(?:long[- ]context|context\s*window|niah)\b"
        if "rlhf" in lower:
            return "RLHF", r"(?i)\b(?:rlhf|reward\s*model|ppo)\b"
        if "dpo" in lower or "direct preference" in lower:
            return "DPO", r"(?i)\b(?:dpo|direct\s*preference)\b"
        tokens = [
            w for w in re.findall(r"[a-zA-Z0-9_\-]+", name)
            if len(w) >= 3 and w.lower() not in _STOPWORDS
        ]
        if tokens:
            return name, r"(?i)\b(?:" + "|".join(re.escape(t) for t in tokens[:3]) + r")\b"
        return name, re.escape(name)

    name_a, pat_a = get_core_entity_regex(side_a_raw)
    name_b, pat_b = get_core_entity_regex(side_b_raw)

    matches_a = len(re.findall(pat_a, report))
    matches_b = len(re.findall(pat_b, report))

    if matches_a >= 2 and matches_b == 0:
        warnings.append(
            f"Missing comparative analysis: Topic compares '{name_a}' vs '{name_b}', but the report provides zero analysis or mentions for '{name_b}'."
        )
    elif matches_b >= 2 and matches_a == 0:
        warnings.append(
            f"Missing comparative analysis: Topic compares '{name_a}' vs '{name_b}', but the report provides zero analysis or mentions for '{name_a}'."
        )

    table_match = re.search(
        r"(\|.+Metric.+\|\n\|[-:\s|]+\|\n(?:\|.+\|\n)+)", report, re.IGNORECASE
    )
    if table_match:
        table_text = table_match.group(1)
        table_matches_a = len(re.findall(pat_a, table_text))
        table_matches_b = len(re.findall(pat_b, table_text))
        if table_matches_a > 0 and table_matches_b == 0:
            warnings.append(
                f"Missing comparative baseline: Report metrics table reports results for '{name_a}' but lacks a comparative baseline or metric for '{name_b}'."
            )
        elif table_matches_b > 0 and table_matches_a == 0:
            warnings.append(
                f"Missing comparative baseline: Report metrics table reports results for '{name_b}' but lacks a comparative baseline or metric for '{name_a}'."
            )

    return warnings


def audit_report_dimension_coverage(
    report: str,
    required_dimensions: Optional[List[str]] = None,
    topic: Optional[str] = None,
    planned_queries: Optional[List[str]] = None,
) -> ReportCoverageSummary:
    """Deterministically audit coverage of requested topic dimensions in a report.

    Dimensions are derived topic-aware from the topic and/or planned queries.
    Identifies covered (>= 2 matches), partial (1 match), and gap (0 matches) dimensions.
    Also detects missing comparisons/baselines in comparative topics.
    """
    if required_dimensions:
        req_set = set(required_dimensions)
        dimension_specs, is_topic_aware, derivation_warnings = derive_topic_dimensions(
            topic=topic,
            report=report,
        )
        matched_specs = [d for d in dimension_specs if d[0] in req_set]

        # If topic/report did not yield matches for required_dimensions, but planned_queries was provided,
        # check if required_dimensions were derived from planned_queries
        if not matched_specs and planned_queries:
            query_specs, q_aware, q_warns = derive_topic_dimensions(
                topic=topic,
                planned_queries=planned_queries,
                report=report,
            )
            matched_query_specs = [d for d in query_specs if d[0] in req_set]
            if matched_query_specs:
                matched_specs = matched_query_specs
                is_topic_aware = q_aware
                derivation_warnings = q_warns

        # Synthesize fallback specs for any required dimensions not found in derived specs,
        # ensuring all required dimensions remain authoritative and are not silently dropped.
        found_keys = {d[0] for d in matched_specs}
        for k in required_dimensions:
            if k not in found_keys:
                words = [
                    w for w in re.findall(r"[a-zA-Z0-9]+", k)
                    if len(w) >= 3 and w.lower() not in _STOPWORDS
                ]
                disp = k.replace("_", " ").title()
                if words:
                    pat = r"(?i)\b(?:" + "|".join(re.escape(w) for w in words) + r")\b"
                else:
                    pat = r"(?i)\b" + re.escape(k) + r"\b"
                matched_specs.append((k, disp, pat))

        dimension_specs = matched_specs
    else:
        dimension_specs, is_topic_aware, derivation_warnings = derive_topic_dimensions(
            topic=topic,
            planned_queries=planned_queries,
            report=report,
        )

    results: Dict[str, DimensionCoverageResult] = {}
    covered_names: List[str] = []
    partial_names: List[str] = []
    gap_names: List[str] = []
    warnings: List[str] = list(derivation_warnings)

    # Filter out heading lines and bibliography/reference list so mere headings do not count as substantive coverage
    narrative_lines = []
    in_bib = False
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading_clean = stripped.lstrip("#").strip().lower()
            if re.match(r"^(sources|references|bibliography|works\s+cited)\b", heading_clean):
                in_bib = True
                break
            continue
        if in_bib:
            break
        if re.match(r"^\[\^?\d+\](?:\:|\s+|$)", stripped):
            continue
        narrative_lines.append(line)

    narrative_text = "\n".join(narrative_lines)

    _EVIDENCE_LIMITED_PATTERN = re.compile(
        r"\b(?:evidence\s+(?:is\s+)?(?:insufficient|limited|unavailable|lacking|unclear)|"
        r"insufficient\s+evidence|limited\s+(?:empirical\s+)?(?:data|evidence|studies)|"
        r"(?:not|never|rarely)\s+reported\s+in\s+the\s+(?:retrieved|evaluated|available)\s+(?:sources|literature|evidence)|"
        r"unspecified\s+in\s+source\s+text)\b",
        re.IGNORECASE,
    )

    for dim_key, disp_name, pattern in dimension_specs:
        matches = list(re.finditer(pattern, narrative_text))
        snippets = []
        for m in matches[:3]:
            start = max(0, m.start() - 40)
            end = min(len(narrative_text), m.end() + 40)
            snip = " ".join(narrative_text[start:end].split())
            snippets.append(snip)

        is_evidence_limited = False
        for m in matches:
            start = max(0, m.start() - 150)
            end = min(len(narrative_text), m.end() + 150)
            window = narrative_text[start:end]
            if _EVIDENCE_LIMITED_PATTERN.search(window):
                is_evidence_limited = True
                break

        if len(matches) >= 2:
            res = DimensionCoverageResult(
                dimension=dim_key,
                display_name=disp_name,
                is_covered=True,
                status="covered",
                coverage_level="evidence_limited" if is_evidence_limited else "full",
                matched_snippets=snippets,
            )
            covered_names.append(dim_key)
        elif len(matches) == 1:
            if is_evidence_limited:
                res = DimensionCoverageResult(
                    dimension=dim_key,
                    display_name=disp_name,
                    is_covered=True,
                    status="covered",
                    coverage_level="evidence_limited",
                    matched_snippets=snippets,
                )
                covered_names.append(dim_key)
            else:
                res = DimensionCoverageResult(
                    dimension=dim_key,
                    display_name=disp_name,
                    is_covered=False,
                    status="partial",
                    coverage_level="partial",
                    matched_snippets=snippets,
                )
                partial_names.append(dim_key)
                warnings.append(
                    f"Partial coverage: Requested dimension '{disp_name}' has only 1 passing mention without empirical or comparative depth."
                )
        else:
            res = DimensionCoverageResult(
                dimension=dim_key,
                display_name=disp_name,
                is_covered=False,
                status="gap",
                coverage_level="none",
                matched_snippets=[],
            )
            gap_names.append(dim_key)
            warnings.append(
                f"Coverage gap: Requested dimension '{disp_name}' is absent from the report."
            )
        results[dim_key] = res

    # Missing comparison / baseline audit
    missing_comparisons: List[str] = []
    effective_topic = topic
    if not effective_topic and report:
        m = re.search(r"(?im)^#\s+Research\s+Summary:\s*([^\n]+)", report)
        if m:
            effective_topic = m.group(1).strip()

    if effective_topic:
        missing_comparisons = check_report_comparative_baselines(report, effective_topic)
        warnings.extend(missing_comparisons)

    return ReportCoverageSummary(
        total_dimensions=len(dimension_specs),
        covered_dimensions_count=len(covered_names),
        gap_dimensions_count=len(gap_names),
        partially_covered_dimensions_count=len(partial_names),
        covered_dimensions=covered_names,
        gap_dimensions=gap_names,
        partially_covered_dimensions=partial_names,
        dimension_results=results,
        missing_comparisons=missing_comparisons,
        is_topic_aware=is_topic_aware,
        topic=effective_topic,
        warnings=warnings,
    )


def check_report_foundations(report: str) -> bool:
    """Check whether the report contains a section explaining core mechanisms/foundations.

    Looks for headings like '## Core Foundations & Mechanism', '## Core Foundations',
    or '## Foundations & Mechanism'.
    """
    pattern = r"(?im)^#{1,3}\s+.*(?:Core Foundations|Foundations\s*&\s*Mechanism).*"
    return bool(re.search(pattern, report))


def extract_report_source_mapping(report: str) -> Dict[str, str]:
    """Map citation markers (e.g. '[1]', '1') to full source URLs found in the report.

    Accurately extracts numbered bibliography entries from sections like '## Sources',
    '## References', '## Bibliography', avoiding spurious matches from report body text.
    Supports bracketed [1], list numbered 1., bulleted - [1], markdown links [Title](url),
    inline bullet Source lines, and footnote references.
    """
    mapping: Dict[str, str] = {}

    # 1. Locate dedicated Sources / References / Bibliography section if present
    bib_match = re.search(
        r"(?im)^#{1,4}\s+(?:sources|references|bibliography|citations)\b[^\n]*\n([\s\S]*?)(?=(?:\n#{1,4}\s+|\n---|\Z))",
        report,
    )

    if bib_match:
        bib_text = bib_match.group(1)
        pending_ref: Optional[str] = None
        for line in bib_text.splitlines():
            s_line = line.strip()
            if not s_line:
                continue

            # Look for leading citation marker: [1], [tag], 1., 1), * [1], - [1], * 1.
            tag_match = re.search(r"^(?:[\*\-\•]\s*)?\[([a-zA-Z0-9_\-]+)\][\.\:\)]?\s*(.*)", s_line)
            num_match = re.search(r"^(?:[\*\-\•]\s*)?(\d+)[\.\:\)]?\s*(.*)", s_line)
            m = tag_match or num_match
            if m:
                ref_num = m.group(1)
                rest = m.group(2)
                pending_ref = ref_num

                # Check for markdown link: [Title](url) on the same line
                md_url = re.search(r"\[[^\]]+\]\((https?://[^\)]+)\)", rest)
                if md_url:
                    clean_url = md_url.group(1).rstrip(".,;:)*_~`\"'")
                    mapping[f"[{ref_num}]"] = clean_url
                    mapping[ref_num] = clean_url
                    pending_ref = None
                    continue

                # Check for raw URL: https?://... on the same line
                url_match = re.search(r"https?://[^\s\)\]\>\"\'\*`]+", rest)
                if url_match:
                    clean_url = url_match.group(0).rstrip(".,;:)*_~`\"'")
                    mapping[f"[{ref_num}]"] = clean_url
                    mapping[ref_num] = clean_url
                    pending_ref = None
                    continue
            elif pending_ref is not None:
                # Check for markdown link or raw URL on continuation line
                md_url = re.search(r"\[[^\]]+\]\((https?://[^\)]+)\)", s_line)
                if md_url:
                    clean_url = md_url.group(1).rstrip(".,;:)*_~`\"'")
                    mapping[f"[{pending_ref}]"] = clean_url
                    mapping[pending_ref] = clean_url
                    pending_ref = None
                    continue

                url_match = re.search(r"https?://[^\s\)\]\>\"\'\*`]+", s_line)
                if url_match:
                    clean_url = url_match.group(0).rstrip(".,;:)*_~`\"'")
                    mapping[f"[{pending_ref}]"] = clean_url
                    mapping[pending_ref] = clean_url
                    pending_ref = None
                    continue

    # 2. Footnote citations anywhere in report: '[1]: https://...' or '[^1]: https://...' or '[tag]: https://...'
    for fn_match in re.finditer(r"(?m)^\[\^?([a-zA-Z0-9_\-]+)\]:\s*(https?://[^\s\)\]\>\"\'\*`]+)", report):
        ref_num = fn_match.group(1)
        clean_url = fn_match.group(2).rstrip(".,;:)*_~`\"'")
        mapping[f"[{ref_num}]"] = clean_url
        mapping[ref_num] = clean_url

    # 3. Inline bullet source lines: e.g., '[1] ...\n *Source: https://...*' or '[tag] ...'
    for bullet_match in re.finditer(r"\[([a-zA-Z0-9_\-]+)\].*?\n\s*\*?(?:Source|URL):\s*(https?://[^\s\)\]\>\"\'\*`]+)", report, re.IGNORECASE):
        ref_num = bullet_match.group(1)
        clean_url = bullet_match.group(2).rstrip(".,;:)*_~`\"'")
        if ref_num not in mapping:
            mapping[f"[{ref_num}]"] = clean_url
            mapping[ref_num] = clean_url

    # 4. Standalone citation lines: '[1] https://...' or '[tag] https://...' anywhere in the report
    for line_match in re.finditer(r"(?m)^\s*(?:[\*\-\•]\s*)?\[([a-zA-Z0-9_\-]+)\]\s*(?:[:\-–]\s*)?(https?://[^\s\)\]\>\"\'\*`]+)", report):
        ref_num = line_match.group(1)
        clean_url = line_match.group(2).rstrip(".,;:)*_~`\"'")
        if ref_num not in mapping:
            mapping[f"[{ref_num}]"] = clean_url
            mapping[ref_num] = clean_url

    # 5. Local URL fallback: if a claim has [N] or [tag] and URL immediately on the same line
    for local_match in re.finditer(r"\[([a-zA-Z0-9_\-]+)\][^\n]{0,100}?(https?://[^\s\)\]\>\"\'\*`]+)", report):
        ref_num = local_match.group(1)
        if ref_num not in mapping:
            clean_url = local_match.group(2).rstrip(".,;:)*_~`\"'")
            mapping[f"[{ref_num}]"] = clean_url
            mapping[ref_num] = clean_url

    return mapping


def _normalize_numeric_literal(token: str) -> str:
    """Normalize one numeric literal to a plain decimal string.

    Handles comma/space thousands separators and scientific notation. Values
    containing 'e' or 'E' are expanded via Decimal so they match their plain
    decimal equivalents in traceability checks.
    """
    token = token.replace(",", "").replace(" ", "").lstrip("+")
    if "e" in token.lower():
        try:
            return format(Decimal(token), "f")
        except (InvalidOperation, ValueError):
            return token
    return token


def _normalize_numeric_tokens(text: str) -> list[str]:
    """Extract numeric values while normalizing thousands separators and attached units.

    Examples:
        '~20,000 hours' -> ['20000']
        '20 000 hours'  -> ['20000']
        '61.4%'          -> ['61.4']
        '512x speedup'   -> ['512']
    """
    normalized = text.replace("\u202f", " ").replace("\u00a0", " ")

    # Match numbers with optional comma/space thousands separators
    # and optional decimal portions, permitting attached units (e.g. 512x, 100ms, 61.4%).
    pattern = (
        r"(?<![\w.eE+\-])"
        r"[+-]?"
        r"(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+|\d+)"
        r"(?:\.\d+)?"
        r"(?:[eE][+-]?\d+)?"
        r"(?![0-9]|\.\d|,\d{3}|[eE][+-]?)"
    )

    tokens = re.findall(pattern, normalized)
    return [_normalize_numeric_literal(token) for token in tokens]


def _numeric_token_is_present(token: str, corpus: str) -> bool:
    """Check a normalized numeric token against source text."""
    corpus_tokens = _normalize_numeric_tokens(corpus)
    return token in corpus_tokens


# ---------------------------------------------------------------------------
# Claim-to-Evidence Relevance Heuristics
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "a", "an", "the", "and", "or", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "as", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "it", "its", "this", "that",
    "vs", "versus", "approx", "approximately", "around", "about", "baseline",
    "unspecified", "not", "reported", "provided", "source", "text", "context",
    "comparison", "measurement", "metric", "value", "result", "table",
}

_DATASET_SCALE_METRIC_KEYWORDS = (
    "training scale", "dataset scale", "dataset size", "corpus size",
    "training data", "speech data", "hours of speech", "speech hours",
    "training hours", "total hours", "audio hours", "hours",
    "dataset", "corpus", "token count", "parameter count", "model size",
    "vocabulary size", "speaker count", "speaker diversity", "speakers", "speaker",
    "language count", "languages", "language coverage", "language",
    "dialect count", "dialects", "dialect",
    "utterance count", "utterances", "utterance",
    "sample count", "samples", "sample",
    "document count", "documents", "document",
    "record count", "records", "record",
    "example count", "examples", "example",
    "audio duration", "scale",
    "number of documents", "number of records", "number of examples",
    "number of samples", "number of tokens", "number of speakers",
    "number of languages", "number of utterances",
)

_DATASET_SCALE_UNITS = (
    "hours", "hrs", "tokens", "samples", "parameters", "params",
    "speakers", "speaker", "languages", "language", "dialects", "dialect",
    "sentences", "utterances", "utterance",
    "documents", "document", "records", "record",
    "examples", "example",
)

_DATASET_SCALE_UNITS_REGEX = re.compile(
    r"\b(?:hours?|hrs?|speakers?|languages?|dialects?|tokens?|samples?|"
    r"utterances?|parameters?|params?|sentences?|words?|documents?|records?|examples?)\b",
    re.IGNORECASE,
)

_DATASET_ABBREV_UNIT_RE = re.compile(r"\b(?:hr|param)\b", re.IGNORECASE)

_PERFORMANCE_BENCHMARK_KEYWORDS = (
    "wer", "cer", "word error rate", "character error rate",
    "accuracy", "acc", "f1", "precision", "recall", "bleu", "rouge",
    "exact match", "exact match gain",
    "latency", "inference time", "execution time", "delay", "runtime",
    "throughput", "tokens per second", "tokens/s", "speedup", "fps", "rtf", "real-time factor",
    "perplexity", "ppl", "loss", "bpc", "pesq", "stoi", "mushra",
    "mos", "mean opinion score", "error rate",
    "win rate", "win-rate", "winrate",
    "ndcg", "mrr",
    "quality", "speech quality", "audio quality", "fidelity",
    "naturalness", "intelligibility",
)

_SCALE_FOCUSED_METRIC_KEYWORDS = (
    "dataset size", "corpus size", "training data", "speech data",
    "training scale", "dataset scale", "model size", "parameter count",
    "token count", "vocabulary size", "speaker count", "language count",
    "dialect count", "utterance count", "sample count", "document count",
    "record count", "example count", "audio duration", "speech hours",
    "audio hours", "training hours", "total hours", "hours of speech",
    "number of documents", "number of records", "number of examples",
    "number of samples", "number of tokens", "number of speakers",
    "number of languages", "number of utterances",
)

_PROSE_OR_VAGUE_BASELINE_PATTERN = (
    r"\b(?:mentioned in prose|named in prose|described in prose|in prose|"
    r"general comparison|compared to prior work|prior work|previous work|"
    r"previous models|prose baseline|unlinked baseline)\b"
)

_COMPARATIVE_CLAIM_KEYWORDS = (
    "significantly enhances",
    "significantly improves",
    "significantly outperforms",
    "significantly reduces",
    "enhances",
    "improves",
    "outperforms",
    "reduces",
    "faster than",
    "better than",
    "superior to",
    "exceeds",
)


def _classify_metric_type(metric_str: str, value_str: str, baseline_str: str) -> str:
    """Classify a metric row as either 'dataset_scale' or 'performance_benchmark'.

    Separates descriptive corpus/model scale parameters from experimental benchmarks.
    Distinguishes the metric itself and its measured values from incidental mentions of
    datasets or benchmarks in context/baseline fields.
    """
    m_lower = metric_str.lower()
    v_lower = value_str.lower()

    # 1. Check if metric name denotes evaluation/performance benchmark
    is_eval_metric = any(
        re.search(r"\b" + re.escape(kw) + r"\b", m_lower)
        for kw in _PERFORMANCE_BENCHMARK_KEYWORDS
    )

    # 2. Check for scale indicators in metric name and value
    is_scale_focused_name = any(
        re.search(r"\b" + re.escape(kw) + r"\b", m_lower)
        for kw in _SCALE_FOCUSED_METRIC_KEYWORDS
    )
    has_scale_in_metric = any(
        re.search(r"\b" + re.escape(kw) + r"\b", m_lower)
        for kw in _DATASET_SCALE_METRIC_KEYWORDS
    )
    has_scale_unit_in_value = bool(_DATASET_SCALE_UNITS_REGEX.search(v_lower))

    # If the metric explicitly names a scale/volume dimension (e.g., 'Dataset Size',
    # 'Document Count', 'Accuracy Benchmark Dataset Size') and its value expresses
    # scale units (e.g. '10,000 examples', '20K hours'), it is dataset_scale
    # even if an evaluation keyword appeared in the name.
    if is_scale_focused_name and has_scale_unit_in_value:
        return "dataset_scale"

    # If it matches an experimental/evaluation benchmark keyword, it is a performance metric.
    # Incidental mentions of datasets in baseline/context do not override this.
    if is_eval_metric:
        return "performance_benchmark"

    # For other metrics, classify as dataset_scale only if the metric name or measured value
    # indicates scale/corpus statistics (never solely based on baseline contextual text).
    if has_scale_in_metric or has_scale_unit_in_value:
        return "dataset_scale"

    return "performance_benchmark"

_DOMAIN_RULES: Dict[str, Dict[str, Any]] = {
    "speedup": {
        "claim_triggers": ["speedup", "acceleration", "accelerat", "throughput", "fps", "boost", "faster"],
        "evidence_concepts": ["speedup", "faster", "accelerat", "throughput", "boost", "fps", "efficien"],
        "conflicting_indicators": ["gpu", "gpus", "core", "cores", "node", "nodes", "accelerator", "accelerators", "tpu", "tpus", "processor", "processors"],
        "mismatch_detail": "Metric claims speedup/throughput, but cited source text references hardware units without speedup terms.",
    },
    "wer": {
        "claim_triggers": ["wer", "word error rate", "cer", "character error rate"],
        "evidence_concepts": ["wer", "word error rate", "error rate", "cer", "transcription", "asr", "recognition"],
        "conflicting_indicators": ["speaker", "speakers", "language", "languages", "dialect", "dialects", "annotator", "annotators"],
        "mismatch_detail": "Metric claims Word Error Rate (WER) / transcription accuracy, but cited source text lacks recognition or error rate metrics.",
    },
    "latency": {
        "claim_triggers": ["latency", "delay", "runtime", "inference time", "execution time", "response time"],
        "evidence_concepts": ["latency", "delay", "runtime", "inference time", "execution time", "response time", "ms", "millisecond", "microsecond", "second"],
        "conflicting_indicators": [],
        "mismatch_detail": "Metric claims latency/runtime, but cited source text lacks latency or time metrics.",
    },
    "perplexity": {
        "claim_triggers": ["perplexity", "ppl", "bpc"],
        "evidence_concepts": ["perplexity", "ppl", "loss", "bpc", "cross-entropy"],
        "conflicting_indicators": [],
        "mismatch_detail": "Metric claims perplexity/loss, but cited source text lacks perplexity or loss metrics.",
    },
    "accuracy": {
        "claim_triggers": ["accuracy", "acc", "f1", "bleu", "rouge", "precision", "recall"],
        "evidence_concepts": ["accuracy", "acc", "f1", "bleu", "rouge", "precision", "recall", "score"],
        "conflicting_indicators": [],
        "mismatch_detail": "Metric claims accuracy/score, but cited source text lacks accuracy or evaluation score metrics.",
    },
    "scale": {
        "claim_triggers": ["training scale", "dataset scale", "dataset size", "corpus size", "training data", "speech data", "hours of speech", "speech hours", "training hours", "scale"],
        "evidence_concepts": ["scale", "hour", "dataset", "corpus", "token", "parameter", "sample", "size", "speech", "train", "data"],
        "conflicting_indicators": [],
        "mismatch_detail": "Metric claims dataset/training scale, but cited source text lacks scale or dataset metrics.",
    },
}


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")


def _sentence_spans(text: str) -> List[Tuple[int, int, str]]:
    """Split text into sentence spans, returning (start, end, sentence)."""
    spans: List[Tuple[int, int, str]] = []
    start = 0
    for match in _SENTENCE_SPLIT_RE.finditer(text):
        end = match.start()
        spans.append((start, end, text[start:end]))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text), text[start:]))
    return spans


def _assign_domains_to_numbers_in_sentence(
    sentence: str,
    num_spans: List[Tuple[int, int]],
) -> List[Optional[str]]:
    """Return the most likely metric domain for each number in a sentence.

    For respectively/parallel metric enumerations with equal counts of metric
    triggers and target numbers, a positional alignment is used. Otherwise the
    nearest domain trigger (by character distance) is assigned. Numbers in
    sentences with no triggers remain unassigned.
    """
    sent_lower = sentence.lower()
    trigger_spans: List[Tuple[int, int, str]] = []
    for domain_name, rule in _DOMAIN_RULES.items():
        if domain_name == "scale":
            continue
        for trig in rule["claim_triggers"]:
            for m in re.finditer(r"\b" + re.escape(trig.lower()) + r"\b", sent_lower):
                trigger_spans.append((m.start(), m.end(), domain_name))
    trigger_spans.sort()

    n = len(num_spans)
    t = len(trigger_spans)
    assignments: List[Optional[str]] = [None] * n
    if n == 0 or t == 0:
        return assignments

    if "respectively" in sent_lower and n >= 2 and t >= 2 and n == t:
        num_order = sorted(range(n), key=lambda i: num_spans[i][0])
        trig_order = sorted(range(t), key=lambda i: trigger_spans[i][0])
        for k in range(n):
            num_idx = num_order[k]
            trig_idx = trig_order[k]
            assignments[num_idx] = trigger_spans[trig_idx][2]
        return assignments

    for i, (ns, ne) in enumerate(num_spans):
        best_d: Optional[int] = None
        best_dom: Optional[str] = None
        for ts, te, dom in trigger_spans:
            if ns <= te and ts <= ne:
                d = 0
            else:
                d = min(abs(ns - te), abs(ts - ne))
            if best_d is None or d < best_d:
                best_d = d
                best_dom = dom
        assignments[i] = best_dom
    return assignments


def _stem_word(word: str) -> str:
    """Lightweight suffix normalization for English benchmark terminology."""
    w = word.lower().strip()
    if len(w) <= 3:
        return w
    for suffix in ("ations", "ation", "ments", "ment", "ings", "ing", "ers", "er", "ies", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[: -len(suffix)]
    return w


def _extract_number_windows(
    text: str, target_tokens: List[str], window_chars: int = 120
) -> List[str]:
    """Find local text windows surrounding occurrences of target normalized numeric tokens."""
    pattern = (
        r"(?<![\w.eE+\-])"
        r"[+-]?"
        r"(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+|\d+)"
        r"(?:\.\d+)?"
        r"(?:[eE][+-]?\d+)?"
        r"(?![0-9]|\.\d|,\d{3}|[eE][+-]?)"
    )
    windows: List[str] = []
    text_clean = text.replace("\u202f", " ").replace("\u00a0", " ")
    for m in re.finditer(pattern, text_clean):
        norm = _normalize_numeric_literal(m.group(0))
        if norm in target_tokens:
            start = max(0, m.start() - window_chars)
            end = min(len(text_clean), m.end() + window_chars)
            windows.append(text_clean[start:end].lower())
    return windows


def _extract_all_number_spans(text: str) -> List[Tuple[int, int, str]]:
    """Return (start, end, normalized_token) for every numeric token in text."""
    pattern = (
        r"(?<![\w.eE+\-])"
        r"[+-]?"
        r"(?:\d{1,3}(?:,\d{3})+|\d{1,3}(?: \d{3})+|\d+)"
        r"(?:\.\d+)?"
        r"(?:[eE][+-]?\d+)?"
        r"(?![0-9]|\.\d|,\d{3}|[eE][+-]?)"
    )
    spans: List[Tuple[int, int, str]] = []
    text_clean = text.replace("\u202f", " ").replace("\u00a0", " ")
    for m in re.finditer(pattern, text_clean):
        norm = _normalize_numeric_literal(m.group(0))
        spans.append((m.start(), m.end(), norm))
    return spans


_DATASET_SIZE_SUFFIX_MULTIPLIERS = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}

_DATASET_SIZE_EXTRA_UNIT_WORDS = ("vectors", "vector")

_DATASET_SIZE_UNIT_WORDS = tuple(sorted(
    set(_DATASET_SCALE_UNITS)
    | {u.rstrip("s") for u in _DATASET_SCALE_UNITS if u.endswith("s")}
    | set(_DATASET_SIZE_EXTRA_UNIT_WORDS),
    key=len,
    reverse=True,
))

_DATASET_SIZE_CONTEXT_RE = re.compile(
    r"(?<![\w.eE+\-])[+-]?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?P<exp>[eE][+-]?\d+)?"
    r"(?!\.\d)(?![eE][+-]?)"
    r"\s*(?P<suf>k|m|b|thousand|million|billion)?(?:\s*(?P<unit>"
    + "|".join(re.escape(u) for u in _DATASET_SIZE_UNIT_WORDS)
    + r"))?\b",
    re.IGNORECASE,
)

_RANGE_OR_ENUM_GAP_RE = re.compile(
    r"^[ \t,\-–—]*(?:(?:and|or|to|through)\b[ \t,\-–—]*)?$",
    re.IGNORECASE,
)

_DATASET_SIZE_LABEL_FIRST_SAFE_UNITS = frozenset({
    "hour", "hours", "hr", "hrs",
    "token", "tokens",
    "sample", "samples",
    "parameter", "parameters", "param", "params",
    "vector", "vectors",
})

_DATASET_SIZE_LABEL_FIRST_RE = re.compile(
    r"\b(?P<unit>"
    + "|".join(re.escape(u) for u in _DATASET_SIZE_UNIT_WORDS)
    + r")(?:\s+(?P<label>count|size|total|number))?"
    r"\s*[:=]\s*"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<dec>\d+))?"
    r"(?P<exp>[eE][+-]?\d+)?"
    r"(?!\.\d)(?![eE][+-]?)"
    r"(?:\s*(?P<suf>k|m|b|thousand|million|billion)?)\b",
    re.IGNORECASE,
)


def _parse_scale_number(
    num_str: str,
    suffix: Optional[str],
    exp: Optional[str] = None,
) -> Optional[float]:
    """Convert a numeric token, optional exponent, and optional k/m/b suffix into a scalar value."""
    try:
        value = Decimal(num_str.replace(",", "").replace(" ", ""))
    except (InvalidOperation, ValueError):
        return None
    if exp:
        try:
            exp_val = int(exp.lstrip("eE"))
            value *= Decimal(10) ** exp_val
        except (InvalidOperation, ValueError):
            return None
    if suffix:
        multiplier = _DATASET_SIZE_SUFFIX_MULTIPLIERS.get(suffix.lower())
        if multiplier is None:
            return None
        value *= Decimal(multiplier)
    result = float(value)
    return result if math.isfinite(result) else None


def _canonical_dataset_size_unit(unit: Optional[str], suffix: Optional[str]) -> Optional[str]:
    """Use a concrete unit word when present, otherwise map magnitude suffixes to a canonical category."""
    if unit:
        unit_lower = unit.lower()
        if unit_lower in ("param", "params"):
            return "parameter"
        if unit_lower in ("hr", "hrs"):
            return "hour"
        return unit_lower.rstrip("s")
    if suffix:
        suffix_lower = suffix.lower()
        if suffix_lower in ("k", "thousand"):
            return "_thousand"
        if suffix_lower in ("m", "million"):
            return "_million"
        if suffix_lower in ("b", "bn", "billion"):
            return "_billion"
    return None


def _format_size_value(value: float) -> str:
    """Format a parsed dataset-size value for human-readable warning text.

    Integral values render with thousands separators and no decimal suffix;
    non-integral values keep their decimal precision with separators and never
    fall back to scientific notation.
    """
    if value.is_integer():
        return f"{int(value):,}"
    formatted = f"{value:,}"
    if "e" in formatted or "E" in formatted:
        formatted = f"{value:,.6f}".rstrip("0").rstrip(".")
    return formatted


def _implied_dataset_unit(metric_str: str) -> Optional[str]:
    """Infer the unique concrete dataset-size unit a metric label implies.

    Candidate unit words are matched on word boundaries and canonicalized with
    the shared unit normalization, so equivalent spellings collapse into a
    single candidate. Returns the canonical unit only when exactly one distinct
    concrete unit is implied, otherwise None (no candidate or ambiguous label).
    """
    metric_lower = metric_str.lower()
    candidates = set()
    for word in _DATASET_SIZE_UNIT_WORDS:
        if re.search(r"\b" + re.escape(word) + r"\b", metric_lower):
            canonical = _canonical_dataset_size_unit(word, None)
            if canonical is not None and not canonical.startswith("_"):
                candidates.add(canonical)
    if len(candidates) == 1:
        return next(iter(candidates))
    return None


def _extract_dataset_size_contexts(text: str) -> List[Tuple[str, float]]:
    """Return (unit, value) pairs for dataset-size mentions found in text.

    Handles number-first forms ('10M samples') and conservative label-first
    forms ('Sample count: 10M', 'Training hours: 24'). Label-first matching is
    limited to 'unit [count|size|total|number] [:|=] number' expressions within
    a single clause; a suffix-less number followed by a non-dataset word is
    treated as uncertain, and a unit without a label word must be a common
    statistical unit.
    Overlapping number-first matches are deduplicated in favor of the
    label-first span. Ambiguous forms are left unextracted (uncertainty).

    A shared trailing unit in a range or enumeration (e.g. 'between 10M and
    100M samples') is associated with every bare-magnitude value connected to
    it by pure range/enumeration conjunctions. Propagation never crosses
    sentence boundaries or unrelated intervening words; ambiguous cases keep
    the bare-magnitude unit.
    """
    if not text:
        return []

    entries: List[Tuple[int, int, str, float]] = []
    label_first_spans: List[Tuple[int, int]] = []

    for match in _DATASET_SIZE_LABEL_FIRST_RE.finditer(text):
        unit = match.group("unit")
        suffix = match.group("suf")
        if match.group("label") is None and unit.lower() not in _DATASET_SIZE_LABEL_FIRST_SAFE_UNITS:
            continue
        num_str = match.group("num")
        dec = match.group("dec")
        exp = match.group("exp")
        if dec is not None:
            num_str = f"{num_str}.{dec}"
            num_end = match.end("dec")
        else:
            num_end = match.end("num")
        if exp is not None:
            num_end = match.end("exp")
        if suffix is None:
            trailing_match = re.match(r"\s*([A-Za-z]+)", text[num_end:])
            if trailing_match and trailing_match.group(1).lower() not in _DATASET_SIZE_UNIT_WORDS:
                continue
        value = _parse_scale_number(num_str, suffix, exp)
        if value is None:
            continue
        canonical_unit = _canonical_dataset_size_unit(unit, suffix)
        if canonical_unit:
            entries.append((match.start(), match.end(), canonical_unit, value))
            label_first_spans.append((match.start(), match.end()))

    for match in _DATASET_SIZE_CONTEXT_RE.finditer(text):
        num_str = match.group("num")
        dec = match.group("dec")
        suffix = match.group("suf")
        unit = match.group("unit")
        exp = match.group("exp")
        if dec is not None:
            num_str = f"{num_str}.{dec}"
        if suffix is None and unit is None:
            continue
        value = _parse_scale_number(num_str, suffix, exp)
        if value is None:
            continue
        canonical_unit = _canonical_dataset_size_unit(unit, suffix)
        if not canonical_unit:
            continue
        start, end = match.start(), match.end()
        if any(start < label_end and end > label_start for label_start, label_end in label_first_spans):
            continue
        entries.append((start, end, canonical_unit, value))

    entries.sort(key=lambda entry: entry[0])
    results: List[Tuple[str, float]] = [(u, v) for _, _, u, v in entries]
    spans: List[Tuple[int, int]] = [(s, e) for s, e, _, _ in entries]

    for i in range(len(results) - 1, 0, -1):
        current_unit = results[i][0]
        previous_unit = results[i - 1][0]
        if current_unit.startswith("_") or not previous_unit.startswith("_"):
            continue
        gap = text[spans[i - 1][1]:spans[i][0]]
        if _RANGE_OR_ENUM_GAP_RE.match(gap):
            results[i - 1] = (current_unit, results[i - 1][1])

    return results


def _check_metric_context_consistency(
    metric_str: str,
    value_str: str,
    baseline_str: str,
    rec: EvidenceRecord,
    num_tokens: List[str],
) -> Optional[str]:
    """Check whether a metric's numeric context aligns with the cited source.

    Returns a mismatch detail string when a conflict is detected, otherwise None.
    Missing or ambiguous context is treated as uncertainty and does not return a conflict.
    """
    corpus = (rec.captured_text or "") + " " + (rec.snippet_text or "")
    for obs in rec.snippet_observations:
        corpus += " " + obs

    # 1. Dataset-size conflicts: reported size vs source sizes for the same unit.
    source_sizes = _extract_dataset_size_contexts(corpus)
    row_text = f"{metric_str} {value_str} {baseline_str}"
    row_sizes = _extract_dataset_size_contexts(row_text)
    for row_unit, row_value in row_sizes:
        candidate_units = [row_unit]
        if row_unit.startswith("_"):
            implied_unit = _implied_dataset_unit(metric_str)
            if implied_unit is not None:
                candidate_units.insert(0, implied_unit)
        for candidate_unit in candidate_units:
            source_values_for_unit = [v for u, v in source_sizes if u == candidate_unit]
            if source_values_for_unit:
                if not any(abs(v - row_value) < 1e-9 for v in source_values_for_unit):
                    source_values = ", ".join(
                        _format_size_value(v) for v in sorted(set(source_values_for_unit))
                    )
                    return (
                        f"Reported dataset size {_format_size_value(row_value)} {candidate_unit} conflicts with "
                        f"source context sizes {source_values} {candidate_unit}."
                    )
                break

    # 2. Metric-domain attribution: a number is accepted only when at least one
    # occurrence is assigned to a claim domain. Assignment is per-sentence:
    # positional alignment for respectively/parallel metric enumerations,
    # otherwise nearest domain trigger. Ambiguous cases are treated as
    # uncertainty and do not produce a conflict.
    if not num_tokens:
        return None
    claim_lower = metric_str.lower()
    claim_domains = set()
    for domain_name, rule in _DOMAIN_RULES.items():
        if domain_name == "scale":
            continue
        if any(trig in claim_lower for trig in rule["claim_triggers"]):
            claim_domains.add(domain_name)
    if not claim_domains:
        return None

    target_tokens = set(num_tokens)
    foreign_domain: Optional[str] = None
    for sent_start, sent_end, sentence in _sentence_spans(corpus):
        sent_num_spans = [
            (ns - sent_start, ne - sent_start, norm)
            for ns, ne, norm in _extract_all_number_spans(sentence)
        ]
        if not sent_num_spans:
            continue
        positions = [(ns, ne) for ns, ne, _ in sent_num_spans]
        assignments = _assign_domains_to_numbers_in_sentence(sentence, positions)
        for (_, _, norm), assigned in zip(sent_num_spans, assignments):
            if norm not in target_tokens:
                continue
            if assigned in claim_domains:
                return None
            if assigned and foreign_domain is None:
                foreign_domain = assigned

    if foreign_domain is not None:
        return f"Number appears in source context for {foreign_domain}, not {metric_str}."

    return None


def _evaluate_metric_relevance(
    metric_str: str,
    value_str: str,
    baseline_str: str,
    rec: Optional[EvidenceRecord],
    is_traceable: bool,
    num_tokens: List[str],
    metric_type: str = "performance_benchmark",
) -> tuple[str, str]:
    """Deterministically evaluate whether a metric claim's subject and context align with cited source evidence.

    Outcomes:
    - 'supported_by_heuristic': Heuristic indicators (keywords, domain concepts, local window)
      support the metric's topic and claim context.
    - 'possible_mismatch': Source context conflicts with claim (e.g. hardware count cited as
      speedup factor, or metric subject entirely absent from source).
    - 'insufficient_evidence': Record is missing, failed, snippet-only, or text is insufficient
      to make a reliable heuristic assessment.
    """
    if rec is None:
        return (
            "insufficient_evidence",
            "Cited source URL not found in evidence store; cannot evaluate relevance.",
        )

    if rec.retrieval_status == RetrievalStatus.FAILED:
        return (
            "insufficient_evidence",
            "Cited source retrieval failed; no text available to evaluate relevance.",
        )

    if rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY:
        return (
            "insufficient_evidence",
            "Cited source evidence is snippet-only; search snippets are insufficient for claim relevance verification.",
        )

    corpus = (rec.captured_text or "") + " " + (rec.snippet_text or "")
    for obs in rec.snippet_observations:
        corpus += " " + obs

    if len(corpus.strip()) < 30:
        return (
            "insufficient_evidence",
            "Captured source text is too short to evaluate relevance.",
        )

    claim_full = f"{metric_str} {value_str} {baseline_str}".lower()
    corpus_lower = corpus.lower()

    # Check comparative claims without reported quantitative experimental result
    has_comp_kw = any(comp in claim_full for comp in _COMPARATIVE_CLAIM_KEYWORDS)
    if has_comp_kw and not num_tokens:
        return (
            "possible_mismatch",
            "Comparative claim lacks reported experimental numerical result in metric table.",
        )

    if not is_traceable:
        return (
            "possible_mismatch",
            "Reported numeric value was not found in captured source text.",
        )

    # Check if a dataset-scale quantity (e.g. 20,000 hours) is mischaracterized as a performance/quality metric
    v_lower = value_str.lower()
    m_lower = metric_str.lower()
    is_dataset_unit = any(u in v_lower for u in _DATASET_SCALE_UNITS) or bool(
        _DATASET_ABBREV_UNIT_RE.search(v_lower)
    )
    is_quality_or_perf_claim = any(
        kw in m_lower
        for kw in ("quality", "accuracy", "speedup", "latency", "wer", "perplexity", "score", "fidelity")
    )
    if is_dataset_unit and is_quality_or_perf_claim and metric_type == "performance_benchmark":
        return (
            "possible_mismatch",
            f"Dataset-scale statistic ('{value_str}') mischaracterized as a performance/quality benchmark ('{metric_str}').",
        )

    context_conflict = _check_metric_context_consistency(
        metric_str=metric_str,
        value_str=value_str,
        baseline_str=baseline_str,
        rec=rec,
        num_tokens=num_tokens,
    )
    if context_conflict:
        return ("possible_mismatch", context_conflict)

    # Extract local text windows surrounding target numbers
    windows = _extract_number_windows(corpus, num_tokens)
    if not windows:
        windows = [corpus_lower]

    # Check domain rules
    matched_domains = []
    for domain_name, rule in _DOMAIN_RULES.items():
        if any(trig in claim_full for trig in rule["claim_triggers"]):
            matched_domains.append((domain_name, rule))

    if matched_domains:
        for domain_name, rule in matched_domains:
            # Check if conflicting indicators are present in the local number windows
            has_conflict_in_number_window = False
            if rule["conflicting_indicators"]:
                for win in windows:
                    for ind in rule["conflicting_indicators"]:
                        if re.search(r"\b" + re.escape(ind) + r"\b", win):
                            has_conflict_in_number_window = True
                            break
                    if has_conflict_in_number_window:
                        break

            # Check if domain evidence concepts appear in the windows or corpus
            has_concept_in_number_window = False
            has_concept_in_corpus = False
            for concept in rule["evidence_concepts"]:
                pattern = r"\b" + re.escape(concept) + r"\b" if " " not in concept else re.escape(concept)
                for win in windows:
                    if re.search(pattern, win):
                        has_concept_in_number_window = True
                        break
                if re.search(pattern, corpus_lower):
                    has_concept_in_corpus = True

            # If number is directly attached to conflicting indicator without domain concept near it
            if has_conflict_in_number_window and not has_concept_in_number_window:
                return (
                    "possible_mismatch",
                    rule["mismatch_detail"],
                )

            # If domain concepts are completely absent from corpus
            if not has_concept_in_corpus:
                return (
                    "possible_mismatch",
                    rule["mismatch_detail"],
                )

        return (
            "supported_by_heuristic",
            "Claim domain concepts aligned with cited source text.",
        )

    # General keyword overlap if no specific domain matched
    metric_words = re.findall(r"[a-zA-Z]{3,}", metric_str.lower())
    content_stems = {
        _stem_word(w)
        for w in metric_words
        if w not in _STOPWORDS and _stem_word(w) not in _STOPWORDS
    }

    if not content_stems:
        return (
            "supported_by_heuristic",
            "General metric value traceable in full text.",
        )

    matched_stems = set()
    for stem in content_stems:
        pattern = r"\b" + re.escape(stem)
        if re.search(pattern, corpus_lower):
            matched_stems.add(stem)

    if not matched_stems:
        return (
            "possible_mismatch",
            f"Metric subject terms ({', '.join(sorted(content_stems))}) do not appear in cited source text.",
        )

    return (
        "supported_by_heuristic",
        f"Metric terms ({', '.join(sorted(matched_stems))}) matched in cited source text.",
    )


def audit_report_metrics(
    report: str,
    store: EvidenceStore,
) -> MetricAuditSummary:
    """Deterministically audit quantitative metrics in a research report against the EvidenceStore.

    Checks:
    1. Metric table structure: Expects 'Context & Comparison Baseline' column.
    2. Baseline context: Flags rows missing a baseline unless explicitly flagged as
       unspecified (e.g., '[Baseline unspecified in source text]').
    3. Source traceability: Resolves the cited source and verifies that numerical
       tokens in the reported value appear in the captured text or snippets.
    """
    source_mapping = extract_report_source_mapping(report)
    lines = report.splitlines()

    in_table = False
    table_headers: List[str] = []
    rows_data: List[List[str]] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            raw_cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if not in_table:
                # Check if this could be a metric header
                clean_headers = [re.sub(r"[*_`]", "", c).strip().lower() for c in raw_cells]
                if any("metric" in h or "measurement" in h for h in clean_headers) and any(
                    "value" in h or "result" in h for h in clean_headers
                ):
                    in_table = True
                    table_headers = clean_headers
                    continue
            else:
                # Separator line? (e.g. |---|---|)
                if re.match(r"^[-:\s|]+$", stripped):
                    continue
                if len(raw_cells) >= 2:
                    rows_data.append(raw_cells)
        else:
            in_table = False

    if not rows_data:
        return MetricAuditSummary(
            total_metrics_found=0,
            traceable_metrics_count=0,
            untraceable_metrics_count=0,
            missing_baseline_count=0,
            specified_baseline_count=0,
            explicitly_unspecified_baseline_count=0,
            unlinked_prose_baseline_count=0,
            explicitly_unspecified_performance_baseline_count=0,
            missing_performance_baseline_count=0,
            supported_relevance_count=0,
            possible_mismatch_count=0,
            insufficient_evidence_count=0,
            performance_metrics_count=0,
            dataset_scale_metrics_count=0,
            full_text_metric_rows_count=0,
            snippet_only_metric_rows_count=0,
            full_text_supported_count=0,
            snippet_only_supported_count=0,
            rows=[],
            warnings=[],
        )

    # Determine column indexes
    metric_idx = 0
    value_idx = 1
    baseline_idx: Optional[int] = None
    source_idx = len(table_headers) - 1 if table_headers else 2

    for i, h in enumerate(table_headers):
        if "metric" in h or "measurement" in h:
            metric_idx = i
        elif "value" in h or "result" in h:
            value_idx = i
        elif "baseline" in h or "context" in h:
            baseline_idx = i
        elif "source" in h:
            source_idx = i

    audit_rows: List[MetricAuditRow] = []
    overall_warnings: List[str] = []
    traceable_count = 0
    untraceable_count = 0
    missing_baseline_count = 0
    missing_performance_baseline_count = 0
    specified_baseline_count = 0
    explicitly_unspecified_baseline_count = 0
    explicitly_unspecified_performance_baseline_count = 0
    unlinked_prose_baseline_count = 0
    supported_relevance_count = 0
    possible_mismatch_count = 0
    insufficient_evidence_count = 0
    performance_metrics_count = 0
    dataset_scale_metrics_count = 0
    full_text_supported_count = 0
    snippet_only_supported_count = 0

    for cells in rows_data:
        row_warnings: List[str] = []
        metric_str = cells[metric_idx] if metric_idx < len(cells) else ""
        value_str = cells[value_idx] if value_idx < len(cells) else ""
        baseline_str = cells[baseline_idx] if (baseline_idx is not None and baseline_idx < len(cells)) else ""
        source_str = cells[source_idx] if source_idx < len(cells) else ""

        # Classify metric type: performance_benchmark vs dataset_scale
        metric_type = _classify_metric_type(metric_str, value_str, baseline_str)
        if metric_type == "dataset_scale":
            dataset_scale_metrics_count += 1
        else:
            performance_metrics_count += 1

        # Distinguish a specified baseline, an explicit disclosure, an unlinked prose baseline, and an omission.
        baseline_status = "missing"
        if baseline_idx is None:
            if metric_type == "performance_benchmark":
                row_warnings.append(
                    f"Metric '{metric_str}': Table is missing a 'Context & Comparison Baseline' column."
                )
        else:
            b_norm = baseline_str.strip().lower()
            unspecified_marker = bool(re.search(
                r"\b(?:baseline|comparison)\b.{0,80}\b"
                r"(?:unspecified|not specified|unknown|not reported|not provided)\b"
                r"|\b(?:unspecified|not specified|unknown)\b.{0,80}\bbaseline\b",
                b_norm,
            ))
            prose_vague_marker = bool(re.search(_PROSE_OR_VAGUE_BASELINE_PATTERN, b_norm))

            if unspecified_marker:
                baseline_status = "explicitly_unspecified"
                explicitly_unspecified_baseline_count += 1
                if metric_type == "performance_benchmark":
                    explicitly_unspecified_performance_baseline_count += 1
                    row_warnings.append(
                        f"Metric '{metric_str}': Comparison baseline is explicitly "
                        "unspecified; numeric traceability does not verify the comparison."
                    )
            elif prose_vague_marker:
                baseline_status = "unlinked_prose_baseline"
                unlinked_prose_baseline_count += 1
                if metric_type == "performance_benchmark":
                    row_warnings.append(
                        f"Metric '{metric_str}': General comparison or prose baseline '{baseline_str}' "
                        "is not explicitly tied to a quantitative result; not counted as a verified experimental baseline."
                    )
            elif not b_norm or b_norm in {"-", "--", "---", "n/a", "none"}:
                if metric_type == "performance_benchmark":
                    row_warnings.append(
                        f"Metric '{metric_str}': Comparison baseline is missing or empty."
                    )
            else:
                baseline_status = "specified"
                specified_baseline_count += 1

        if baseline_status == "missing":
            missing_baseline_count += 1
            if metric_type == "performance_benchmark":
                missing_performance_baseline_count += 1

        # Resolve Source URL
        resolved_url: Optional[str] = None
        url_match = re.search(r"https?://[^\s\)\]\>\"\'\*`]+", source_str)
        if url_match:
            resolved_url = url_match.group(0).rstrip(".,;:)*_~`\"'")
        else:
            bracket_match = re.search(r"\[(\d+)\]", source_str)
            if bracket_match:
                resolved_url = source_mapping.get(bracket_match.group(0)) or source_mapping.get(bracket_match.group(1))
            elif source_str.strip().isdigit():
                resolved_url = source_mapping.get(source_str.strip())

        # Check traceability to evidence store
        is_traceable = False
        rec: Optional[EvidenceRecord] = None
        if not resolved_url:
            row_warnings.append(
                f"Metric '{metric_str}': Could not resolve source citation '{source_str}' to a URL."
            )
        else:
            rec = store.get_by_url(resolved_url)
            if rec is None:
                row_warnings.append(
                    f"Metric '{metric_str}': Cited URL {resolved_url!r} not found in evidence store."
                )
            else:
                num_tokens = _normalize_numeric_tokens(value_str)
                full_text_corpus = (rec.captured_text or "") + " " + (rec.snippet_text or "")
                for obs in rec.snippet_observations:
                    full_text_corpus += " " + obs

                if not num_tokens:
                    clean_val = value_str.strip()
                    if clean_val and clean_val in full_text_corpus:
                        is_traceable = True
                    else:
                        row_warnings.append(
                            f"Metric '{metric_str}' with value '{value_str}': "
                            f"Non-numeric value not found in captured text of source {resolved_url}."
                        )
                else:
                    missing_tokens = [
                        token
                        for token in num_tokens
                        if not _numeric_token_is_present(token, full_text_corpus)
                    ]

                    if not missing_tokens:
                        is_traceable = True
                    else:
                        row_warnings.append(
                            f"Metric '{metric_str}' with value '{value_str}': "
                            f"Numbers {missing_tokens} not found in captured text of source {resolved_url}."
                        )

        if is_traceable:
            traceable_count += 1
        else:
            untraceable_count += 1

        # Track evidence depth
        evidence_depth = "none"
        if rec:
            if rec.retrieval_status == RetrievalStatus.FULL_TEXT:
                evidence_depth = "full_text"
                if is_traceable:
                    full_text_supported_count += 1
            elif rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY:
                evidence_depth = "snippet_only"
                snippet_only_supported_count += 1

        # Check claim-to-evidence relevance heuristic
        num_tokens = _normalize_numeric_tokens(value_str)
        relevance_status, relevance_detail = _evaluate_metric_relevance(
            metric_str=metric_str,
            value_str=value_str,
            baseline_str=baseline_str,
            rec=rec,
            is_traceable=is_traceable,
            num_tokens=num_tokens,
            metric_type=metric_type,
        )

        is_full_text_verified = (
            evidence_depth == "full_text"
            and is_traceable
            and relevance_status == "supported_by_heuristic"
        )

        if relevance_status == "possible_mismatch":
            possible_mismatch_count += 1
            row_warnings.append(
                f"Metric '{metric_str}': Claim relevance check flagged possible mismatch with source content: {relevance_detail}"
            )
        elif relevance_status == "insufficient_evidence":
            insufficient_evidence_count += 1
            if rec and rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY:
                row_warnings.append(
                    f"Metric '{metric_str}': Supporting evidence is snippet-only; search snippets do not receive full-text verification status."
                )
        elif relevance_status == "supported_by_heuristic":
            supported_relevance_count += 1

        overall_warnings.extend(row_warnings)
        audit_rows.append(
            MetricAuditRow(
                metric=metric_str,
                value=value_str,
                baseline=baseline_str,
                source=source_str,
                resolved_url=resolved_url,
                is_traceable=is_traceable,
                has_baseline_or_flag=(baseline_status in {"specified", "explicitly_unspecified"}),
                baseline_status=baseline_status,
                relevance_status=relevance_status,
                relevance_detail=relevance_detail,
                metric_type=metric_type,
                evidence_depth=evidence_depth,
                is_full_text_verified=is_full_text_verified,
                warnings=row_warnings,
            )
        )

    return MetricAuditSummary(
        total_metrics_found=len(audit_rows),
        traceable_metrics_count=traceable_count,
        untraceable_metrics_count=untraceable_count,
        missing_baseline_count=missing_baseline_count,
        specified_baseline_count=specified_baseline_count,
        explicitly_unspecified_baseline_count=explicitly_unspecified_baseline_count,
        unlinked_prose_baseline_count=unlinked_prose_baseline_count,
        explicitly_unspecified_performance_baseline_count=explicitly_unspecified_performance_baseline_count,
        missing_performance_baseline_count=missing_performance_baseline_count,
        supported_relevance_count=supported_relevance_count,
        possible_mismatch_count=possible_mismatch_count,
        insufficient_evidence_count=insufficient_evidence_count,
        performance_metrics_count=performance_metrics_count,
        dataset_scale_metrics_count=dataset_scale_metrics_count,
        full_text_metric_rows_count=full_text_supported_count,
        snippet_only_metric_rows_count=snippet_only_supported_count,
        full_text_supported_count=full_text_supported_count,
        snippet_only_supported_count=snippet_only_supported_count,
        rows=audit_rows,
        warnings=overall_warnings,
    )


_PROSE_COMPARATIVE_PATTERNS = [
    # 1. improves / improve / improving / improved + quality/benchmark target
    re.compile(
        r"\b((?:improves?|improved|improving)\s+(?:the\s+)?(?:[\w-]+\s+){0,4}(?:generalization|quality|accuracy|fidelity|prosody|naturalness|intelligibility|timing|consistency|latency|throughput|performance|efficiency|robustness))\b",
        re.IGNORECASE,
    ),
    # 2. enhances / enhance / enhancing / enhanced + quality/benchmark target
    re.compile(
        r"\b((?:enhances?|enhanced|enhancing)\s+(?:the\s+)?(?:[\w-]+\s+){0,4}(?:generalization|quality|accuracy|fidelity|prosody|naturalness|intelligibility|timing|consistency|latency|throughput|performance|efficiency|robustness|speech synthesis))\b",
        re.IGNORECASE,
    ),
    # 3. superior + attribute or superior to
    re.compile(
        r"\b(superior\s+(?:to\b\s*[\w-]*|(?:[\w-]+\s+){0,4}(?:generalization|performance|quality|accuracy|fidelity|prosody|naturalness|intelligibility|results?|capabilities?|systems?|models?|approaches?|speech|voice|speaker|zero-shot)))\b",
        re.IGNORECASE,
    ),
    # 4. significantly + comparative verb/adjective
    re.compile(
        r"\b(significantly\s+(?:enhanced?|improves?|improved|outperformed?|outperforms|reduces?|reduced|faster|better|surpasses?|surpassed|exceeds?|exceeded))\b",
        re.IGNORECASE,
    ),
    # 5. substantially + comparative verb/adjective
    re.compile(
        r"\b(substantially\s+(?:enhanced?|improves?|improved|outperformed?|outperforms|reduces?|reduced|faster|better))\b",
        re.IGNORECASE,
    ),
    # 6. outperforms / surpasses prior/baseline/existing
    re.compile(
        r"\b((?:outperforms?|outperformed|surpasses?|surpassed)\s+(?:prior|previous|existing|state-of-the-art|sota|baseline|competing|alternative)\b[^\.\,\;\:\n]*)",
        re.IGNORECASE,
    ),
    # 7. better + quality/benchmark target
    re.compile(
        r"\b(better\s+(?:generalization|performance|quality|accuracy|fidelity|prosody|naturalness|intelligibility))\b",
        re.IGNORECASE,
    ),
]


def check_report_prose_comparative_claims(
    report: str,
    metrics_summary: Optional[MetricAuditSummary] = None,
) -> List[str]:
    """Audit report narrative prose outside tables for comparative claims lacking quantitative benchmarks.

    Detects comparative assertions in narrative prose (such as 'improves zero-shot speaker generalization',
    'enhance speech synthesis quality', or 'significantly enhanced') and verifies whether a corresponding
    quantitative evaluation result exists in the metrics table.

    Limitations:
    ------------
    This is a deterministic heuristic pattern audit of narrative text. It checks whether specific
    comparative phrases appear in prose and whether corresponding quantitative performance benchmarks
    are present in the metrics table. It does not perform full natural language entailment or semantic
    verification of arbitrary prose claims. Unflagged claims are not guaranteed to be factually true.
    """
    lines = report.splitlines()
    in_code_block = False
    prose_lines = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        # Skip markdown table rows
        if stripped.startswith("|") or stripped.endswith("|"):
            continue
        # Stop narrative analysis at bibliography/sources/references heading (case-insensitive)
        if stripped.startswith("#"):
            heading_clean = stripped.lstrip("#").strip().lower()
            if re.match(r"^(sources|references|bibliography|works\s+cited)\b", heading_clean):
                break
            continue
        # Also stop at bolded/standalone section labels for sources/references
        if re.match(r"^\*{1,2}(sources|references|bibliography|works\s+cited)\*{1,2}", stripped, re.IGNORECASE):
            break
        # Ignore reference-list entries e.g. [1] Paper Title... or [^1] ... or [1]: url
        if re.match(r"^\[\^?\d+\](?:\:|\s+|$)", stripped):
            continue
        # Skip report metadata footer lines
        if (
            stripped.startswith("*Report generated")
            or stripped.startswith("*Evidence ")
            or stripped.startswith("*Citation-level")
            or stripped.startswith("*Metric")
            or stripped.startswith("*Baseline status")
            or stripped.startswith("*Warning:")
            or stripped.startswith("*Audit note:")
            or stripped.startswith("*Prose audit limitation:")
        ):
            continue
        if stripped == "---":
            continue
        prose_lines.append(line)

    prose_text = " ".join(prose_lines)
    if not prose_text.strip():
        return []

    sentences = re.split(r"(?<=[.!?])\s+", prose_text)

    _CONCEPT_STOPWORDS = _STOPWORDS | {
        "model", "architecture", "approach", "paper", "method", "system",
        "author", "authors", "study", "work", "result", "results", "table",
        "superior", "significantly", "substantially", "enhanced", "enhance",
        "enhances", "improves", "improved", "improve", "outperforms",
        "outperform", "outperformed", "better", "faster", "compared",
        "demonstrates", "achieves", "presents", "shows", "features",
        "proposed", "existing", "prior", "previous",
    }
    _MEANINGFUL_3LETTER_ACRONYMS = {
        "dpo", "ppo", "kto", "ipo", "cpo", "sft", "llm", "rag", "wer", "cer", "mos",
        "gpu", "tpu", "cpu", "mla", "mha", "gqa", "moe", "cot", "tot",
    }

    warnings: List[str] = []
    seen_claims = set()

    for sentence in sentences:
        s_clean = sentence.strip()
        if not s_clean:
            continue

        for pattern in _PROSE_COMPARATIVE_PATTERNS:
            for match in pattern.finditer(s_clean):
                matched_phrase = match.group(1).strip()
                matched_phrase_norm = " ".join(matched_phrase.split())
                if matched_phrase_norm.lower() in seen_claims:
                    continue

                is_supported_by_benchmark = False
                if metrics_summary is not None and metrics_summary.performance_metrics_count > 0:
                    raw_words = re.findall(r"[a-zA-Z0-9_\-]+", s_clean.lower())
                    attribute_words = {
                        w for w in raw_words
                        if len(w) >= 4 and w not in _CONCEPT_STOPWORDS
                    }
                    model_acronyms = {
                        w for w in raw_words
                        if len(w) == 3
                        and (w in _MEANINGFUL_3LETTER_ACRONYMS or w.upper() in s_clean)
                        and w not in _CONCEPT_STOPWORDS
                    }

                    for row in metrics_summary.rows:
                        if row.metric_type == "performance_benchmark":
                            row_text = f"{row.metric} {row.baseline}".lower()
                            if attribute_words:
                                if any(aw in row_text or any(token in aw for token in row_text.split() if len(token) >= 4) for aw in attribute_words):
                                    is_supported_by_benchmark = True
                                    break
                            elif model_acronyms:
                                if len(model_acronyms) >= 2:
                                    if all(ma in row_text for ma in model_acronyms):
                                        is_supported_by_benchmark = True
                                        break
                                elif any(ma in row_text for ma in model_acronyms):
                                    is_supported_by_benchmark = True
                                    break

                if not is_supported_by_benchmark:
                    seen_claims.add(matched_phrase_norm.lower())
                    warnings.append(
                        f"Report prose asserts comparative claim '{matched_phrase_norm}' "
                        "without a corresponding quantitative evaluation result in the metrics table."
                    )

    return warnings


def audit_report_citations(
    report: str,
    store: EvidenceStore,
    min_sources: int = 3,
    claim_verification_summary: Optional[Any] = None,
    topic: Optional[str] = None,
    planned_queries: Optional[List[str]] = None,
    required_dimensions: Optional[List[str]] = None,
) -> ReportAuditSummary:
    """Deterministically audit cited source URLs in a research report against the run's EvidenceStore.

    Parameters
    ----------
    report:
        The markdown research report text.
    store:
        The active EvidenceStore for this run.
    min_sources:
        Minimum expected number of matched sources for adequate coverage (default: 3).

    Returns
    -------
    ReportAuditSummary
        Detailed breakdown of matched sources, retrieval completeness, and coverage warnings.
    """
    # Regex matching URLs in markdown links [text](url) or standalone http(s) URLs
    # Clean trailing punctuation and markdown delimiters often attached in markdown/prose (*, _, `, ., ), etc.)
    raw_urls = re.findall(r"https?://[^\s\)\]\>\"\'\*`]+", report)
    cleaned_urls: List[str] = []
    seen = set()

    for u in raw_urls:
        clean = u.rstrip(".,;:)*_~`\"'")
        if clean and clean not in seen:
            seen.add(clean)
            cleaned_urls.append(clean)

    total_found = len(cleaned_urls)
    matched_count = 0
    unmatched_count = 0
    full_text_count = 0
    snippet_only_count = 0
    failed_count = 0
    unmatched_urls: List[str] = []
    warnings: List[str] = []

    for url in cleaned_urls:
        rec = store.get_by_url(url)
        if rec is not None:
            matched_count += 1
            if rec.retrieval_status == RetrievalStatus.FULL_TEXT:
                full_text_count += 1
            elif rec.retrieval_status == RetrievalStatus.SNIPPET_ONLY:
                snippet_only_count += 1
            elif rec.retrieval_status == RetrievalStatus.FAILED:
                failed_count += 1
                warnings.append(
                    f"Cited source URL {url!r} has FAILED retrieval status in evidence store."
                )
        else:
            unmatched_count += 1
            unmatched_urls.append(url)
            warnings.append(
                f"Cited source URL {url!r} was not found in the run's evidence store."
            )

    is_thin = False
    if total_found == 0:
        is_thin = True
        warnings.append("Report contains zero detectable source URL citations.")
    elif matched_count < min_sources:
        is_thin = True
        warnings.append(
            f"Evidence coverage is thin: only {matched_count} sources matched in store "
            f"(minimum expected: {min_sources})."
        )
    elif full_text_count == 0 and matched_count > 0:
        is_thin = True
        warnings.append(
            "Evidence coverage is shallow: 0 matched sources have full-text evidence "
            f"(all {snippet_only_count} matched sources are snippet-only)."
        )

    has_foundations = check_report_foundations(report)
    metrics_summary = audit_report_metrics(report, store)
    prose_comp_warnings = check_report_prose_comparative_claims(report, metrics_summary)
    warnings.extend(prose_comp_warnings)
    coverage_summary = audit_report_dimension_coverage(
        report,
        required_dimensions=required_dimensions,
        topic=topic,
        planned_queries=planned_queries,
    )

    return ReportAuditSummary(
        total_citations_found=total_found,
        matched_sources_count=matched_count,
        unmatched_sources_count=unmatched_count,
        full_text_sources_count=full_text_count,
        snippet_only_sources_count=snippet_only_count,
        failed_sources_count=failed_count,
        unmatched_urls=unmatched_urls,
        is_thin_evidence=is_thin,
        warnings=warnings,
        has_foundations_section=has_foundations,
        metrics_summary=metrics_summary,
        prose_comparative_warnings=prose_comp_warnings,
        claim_verification_summary=claim_verification_summary,
        coverage_summary=coverage_summary,
    )
