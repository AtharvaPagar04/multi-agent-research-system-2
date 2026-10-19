"""
research_crew.evidence
----------------------
Phase 1A: Evidence storage and deterministic integrity foundation.

Public API
----------
    EvidenceRecord    – immutable record of a single retrieved source
    PassageRecord     – immutable record of a specific passage within a source
    RetrievalStatus   – FULL_TEXT | SNIPPET_ONLY | FAILED
    make_evidence_id  – deterministic source entity ID generator
    make_passage_id   – deterministic passage entity ID generator
    make_content_hash – SHA-256 hash helper
    EvidenceStore     – append-only, atomic file-backed record store
    generate_run_id   – UUID4 run identifier generator
    validate_passage  – deterministic provenance check for a text passage
    validate_record_integrity – standalone hash consistency check
    ValidationResult  – result object (is_valid, is_full_text, is_snippet_only, outcome, detail)
    ValidationOutcome – enumeration of possible outcomes
"""

from research_crew.evidence.model import (
    EvidenceRecord,
    PassageRecord,
    RetrievalStatus,
    make_content_hash,
    make_evidence_id,
    make_passage_id,
)
from research_crew.evidence.store import EvidenceStore, generate_run_id
from research_crew.evidence.validator import (
    DimensionCoverageResult,
    MetricAuditRow,
    MetricAuditSummary,
    ReportAuditSummary,
    ReportCoverageSummary,
    ValidationOutcome,
    ValidationResult,
    audit_report_citations,
    audit_report_dimension_coverage,
    audit_report_metrics,
    check_report_foundations,
    validate_passage,
    validate_record_integrity,
)

from research_crew.evidence.verifier import (
    AtomicClaim,
    ClaimAction,
    ClaimVerdict,
    ClaimVerificationRecord,
    ClaimVerificationSummary,
    ClaimVerifier,
    extract_atomic_claims,
    find_passage_for_url_and_query,
    find_supporting_passage,
    repair_json_escapes,
    save_verification_records,
    verify_report_claims,
)

__all__ = [
    "EvidenceRecord",
    "PassageRecord",
    "RetrievalStatus",
    "make_content_hash",
    "make_evidence_id",
    "make_passage_id",
    "EvidenceStore",
    "generate_run_id",
    "ValidationOutcome",
    "ValidationResult",
    "validate_passage",
    "validate_record_integrity",
    "MetricAuditRow",
    "MetricAuditSummary",
    "ReportAuditSummary",
    "ReportCoverageSummary",
    "DimensionCoverageResult",
    "audit_report_citations",
    "audit_report_dimension_coverage",
    "audit_report_metrics",
    "check_report_foundations",
    "ClaimVerdict",
    "ClaimAction",
    "AtomicClaim",
    "ClaimVerificationRecord",
    "ClaimVerificationSummary",
    "ClaimVerifier",
    "extract_atomic_claims",
    "find_passage_for_url_and_query",
    "find_supporting_passage",
    "repair_json_escapes",
    "save_verification_records",
    "verify_report_claims",
]
