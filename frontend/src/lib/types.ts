export type ClaimVerdict = "supported" | "contradicted" | "insufficient_evidence";

export type RetrievalStatus = "full_text" | "snippet_only" | "failed" | null;

export interface ClaimRecord {
  claim_id: string;
  claim_text: string;
  source_url: string | null;
  passage_id: string | null;
  passage_text: string | null;
  verdict: ClaimVerdict | null;
  rationale: string | null;
  action_taken: string | null;
  retrieval_status: RetrievalStatus;
  supporting_urls?: string[];
}

export interface ClaimVerificationSummary {
  total_claims: number;
  supported_claims_count: number;
  contradicted_claims_count: number;
  insufficient_evidence_claims_count: number;
}

export interface ClaimVerificationArtifact extends ClaimVerificationSummary {
  records: ClaimRecord[];
  warnings: string[];
}

export interface EvidenceRecord {
  evidence_id: string;
  source_url: string;
  retrieval_status: "full_text" | "snippet_only" | "failed" | string;
  captured_text: string | null;
  content_hash: string | null;
  final_url: string | null;
  title: string | null;
  snippet_text: string | null;
  provider: string | null;
  snippet_observations?: unknown;
}

export interface EvidenceArtifact {
  version: number;
  run_id: string;
  created_at: string;
  record_count: number;
  records: EvidenceRecord[];
}

export interface FeedbackIteration {
  iteration_index: number;
  queries_issued: string[];
  new_evidence_ids: string[];
  claims_checked_count: number | null;
  verdict_breakdown: Record<string, number>;
  action_taken?: string | null;
  targeted_claim_resolutions?: unknown;
}

export interface FeedbackArtifact {
  run_id: string;
  max_rounds: number | null;
  total_rounds_executed: number | null;
  terminated_reason: string | null;
  final_claims_supported_count: number | null;
  final_unresolved_claims_count: number | null;
  final_action_policy: string | null;
  iterations: FeedbackIteration[];
}

export interface ReportSource {
  marker: string;
  title: string | null;
  url: string | null;
}

export interface ReportAuditLine {
  label: string;
  detail: string;
  raw: string;
}

export interface ResearchReportArtifact {
  path: string;
  title: string | null;
  markdown: string;
  sources: ReportSource[];
  auditLines: ReportAuditLine[];
  metricAudit: {
    traceable: number | null;
    total: number | null;
    detail: string | null;
  };
  claimAudit: {
    supported: number | null;
    total: number | null;
    detail: string | null;
  };
}

export interface RunSummary {
  runId: string;
  shortId: string;
  topic: string | null;
  createdAt: string | null;
  hasEvidence: boolean;
  hasClaims: boolean;
  hasFeedback: boolean;
  hasReport: boolean;
  evidenceCount: number;
  fullTextCount: number;
  snippetOnlyCount: number;
  failedCount: number;
  totalClaims: number | null;
  supportedClaims: number | null;
  contradictedClaims: number | null;
  insufficientClaims: number | null;
  supportRate: number | null;
  feedbackRounds: number | null;
  terminatedReason: string | null;
}

export interface ResearchRunDetail extends RunSummary {
  claims: ClaimRecord[];
  claimWarnings: string[];
  evidence: EvidenceRecord[];
  feedback: FeedbackArtifact | null;
  report: ResearchReportArtifact | null;
}
