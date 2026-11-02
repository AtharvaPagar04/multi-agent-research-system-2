import { cache } from "react";

import {
  ApiError,
  fetchRunAudit,
  fetchRunClaims,
  fetchRunEvidence,
  fetchRunFeedback,
  fetchRunReport,
  listRuns,
  type ApiRunListResponse,
} from "./api";
import { shortRunId } from "./fs-paths";
import { normalizeClaim, parseReportMarkdown } from "./report-parser";
import type {
  EvidenceRecord,
  FeedbackArtifact,
  FeedbackIteration,
  ResearchRunDetail,
  RunSummary,
} from "./types";

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toCount(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function toTrimmedString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function normalizeEvidence(
  raw: Record<string, unknown>,
  index: number,
): EvidenceRecord {
  return {
    evidence_id:
      typeof raw.evidence_id === "string" && raw.evidence_id
        ? raw.evidence_id
        : `evidence-${index}`,
    source_url: typeof raw.source_url === "string" ? raw.source_url : "",
    retrieval_status:
      typeof raw.retrieval_status === "string" ? raw.retrieval_status : "unknown",
    captured_text: typeof raw.captured_text === "string" ? raw.captured_text : null,
    content_hash: typeof raw.content_hash === "string" ? raw.content_hash : null,
    final_url: typeof raw.final_url === "string" ? raw.final_url : null,
    title: typeof raw.title === "string" ? raw.title : null,
    snippet_text: typeof raw.snippet_text === "string" ? raw.snippet_text : null,
    provider: typeof raw.provider === "string" ? raw.provider : null,
  };
}

function normalizeIteration(
  raw: Record<string, unknown>,
  index: number,
): FeedbackIteration {
  const breakdown: Record<string, number> = {};
  if (isPlainObject(raw.verdict_breakdown)) {
    for (const [key, value] of Object.entries(raw.verdict_breakdown)) {
      if (typeof value === "number" && Number.isFinite(value)) {
        breakdown[key] = value;
      }
    }
  }

  return {
    iteration_index: toCount(raw.iteration_index) ?? index,
    queries_issued: Array.isArray(raw.queries_issued)
      ? raw.queries_issued.filter((query): query is string => typeof query === "string")
      : [],
    new_evidence_ids: Array.isArray(raw.new_evidence_ids)
      ? raw.new_evidence_ids.filter(
          (evidenceId): evidenceId is string => typeof evidenceId === "string",
        )
      : [],
    claims_checked_count: toCount(raw.claims_checked_count),
    verdict_breakdown: breakdown,
    action_taken: toTrimmedString(raw.action_taken),
    targeted_claim_resolutions: raw.targeted_claim_resolutions,
  };
}

function resolveTopic(
  reportTitle: string | null,
  claims: { claim_text: string }[],
): string | null {
  if (reportTitle) return reportTitle;
  // No report on backend: expose a compact claim-context label for navigation only.
  const first = claims.find((claim) => claim.claim_text?.trim());
  if (!first) return null;
  const text = first.claim_text.trim().replace(/\s+/g, " ");
  return text.length > 96 ? `${text.slice(0, 93)}…` : text;
}

async function loadRunInternal(
  runId: string,
): Promise<ResearchRunDetail | null> {
  const cleanRunId = runId.trim().toLowerCase();
  if (!cleanRunId) return null;

  // Request all artifacts concurrently via the backend API
  const [claimsResult, evidenceResult, feedbackResult, reportResult, auditResult] =
    await Promise.allSettled([
      fetchRunClaims(cleanRunId),
      fetchRunEvidence(cleanRunId),
      fetchRunFeedback(cleanRunId),
      fetchRunReport(cleanRunId),
      fetchRunAudit(cleanRunId),
    ]);

  const claimsRaw =
    claimsResult.status === "fulfilled" ? claimsResult.value : null;
  const evidenceRaw =
    evidenceResult.status === "fulfilled" ? evidenceResult.value : null;
  const feedbackRaw =
    feedbackResult.status === "fulfilled" ? feedbackResult.value : null;
  const reportMarkdown =
    reportResult.status === "fulfilled" ? reportResult.value : null;
  const auditPayload =
    auditResult.status === "fulfilled" ? auditResult.value : null;

  const hasClaims =
    isPlainObject(claimsRaw) && Array.isArray(claimsRaw.records);
  const hasEvidence =
    isPlainObject(evidenceRaw) && Array.isArray(evidenceRaw.records);
  const hasFeedback =
    isPlainObject(feedbackRaw) && Array.isArray(feedbackRaw.iterations);
  const hasReport =
    typeof reportMarkdown === "string" && reportMarkdown.trim().length > 0;
  const hasAudit = auditPayload !== null;

  // If no artifacts exist at all, check if backend failed with network/503
  if (!hasClaims && !hasEvidence && !hasFeedback && !hasReport && !hasAudit) {
    if (
      claimsResult.status === "rejected" &&
      claimsResult.reason instanceof ApiError &&
      (claimsResult.reason.kind === "network" || claimsResult.reason.status === 503)
    ) {
      throw claimsResult.reason;
    }
    return null;
  }

  const evidenceRecords: EvidenceRecord[] = hasEvidence
    ? (evidenceRaw.records as unknown[])
        .filter(isPlainObject)
        .map(normalizeEvidence)
    : [];

  const claims = hasClaims
    ? (claimsRaw.records as unknown[])
        .filter(isPlainObject)
        .map(normalizeClaim)
    : [];

  const claimWarnings: string[] = [];
  if (hasClaims && Array.isArray(claimsRaw.warnings)) {
    for (const w of claimsRaw.warnings) {
      if (typeof w === "string" && !claimWarnings.includes(w)) {
        claimWarnings.push(w);
      }
    }
  }
  if (auditPayload && Array.isArray(auditPayload.warnings)) {
    for (const w of auditPayload.warnings) {
      if (typeof w === "string" && !claimWarnings.includes(w)) {
        claimWarnings.push(w);
      }
    }
  }

  let report = null;
  if (hasReport) {
    report = parseReportMarkdown(
      reportMarkdown,
      `research_report_${cleanRunId}.md`,
    );
  }

  // Fallback to audit lines from backend audit endpoint if report didn't have them
  if (
    report &&
    report.auditLines.length === 0 &&
    auditPayload &&
    auditPayload.audit_lines.length > 0
  ) {
    report.auditLines = auditPayload.audit_lines;
  }

  const fullTextCount = evidenceRecords.filter(
    (record) => record.retrieval_status === "full_text",
  ).length;
  const snippetOnlyCount = evidenceRecords.filter(
    (record) => record.retrieval_status === "snippet_only",
  ).length;
  const failedCount = evidenceRecords.filter(
    (record) => record.retrieval_status === "failed",
  ).length;

  const computedSupported = claims.filter(
    (claim) => claim.verdict === "supported",
  ).length;
  const computedContradicted = claims.filter(
    (claim) => claim.verdict === "contradicted",
  ).length;
  const computedInsufficient = claims.filter(
    (claim) => claim.verdict === "insufficient_evidence",
  ).length;

  const totalClaims = hasClaims
    ? claims.length
    : (auditPayload?.claim_audit?.total ?? null);
  const supportedClaims = hasClaims
    ? computedSupported
    : (auditPayload?.claim_audit?.supported ?? null);
  const contradictedClaims = hasClaims ? computedContradicted : null;
  const insufficientClaims = hasClaims ? computedInsufficient : null;

  const supportRate =
    totalClaims != null && supportedClaims != null && totalClaims > 0
      ? supportedClaims / totalClaims
      : null;

  const createdAt = hasEvidence
    ? toTrimmedString(evidenceRaw.created_at)
    : null;

  const topic = resolveTopic(report?.title ?? null, claims);

  const feedback: FeedbackArtifact | null = hasFeedback
    ? {
        run_id: cleanRunId,
        max_rounds: toCount(feedbackRaw.max_rounds),
        total_rounds_executed: toCount(feedbackRaw.total_rounds_executed),
        terminated_reason: toTrimmedString(feedbackRaw.terminated_reason),
        final_claims_supported_count: toCount(
          feedbackRaw.final_claims_supported_count,
        ),
        final_unresolved_claims_count: toCount(
          feedbackRaw.final_unresolved_claims_count,
        ),
        final_action_policy: toTrimmedString(feedbackRaw.final_action_policy),
        iterations: (feedbackRaw.iterations as unknown[])
          .filter(isPlainObject)
          .map(normalizeIteration),
      }
    : null;

  return {
    runId: cleanRunId,
    shortId: shortRunId(cleanRunId),
    topic,
    createdAt,
    hasEvidence,
    hasClaims,
    hasFeedback,
    hasReport: Boolean(report),
    evidenceCount: evidenceRecords.length,
    fullTextCount,
    snippetOnlyCount,
    failedCount,
    totalClaims,
    supportedClaims,
    contradictedClaims,
    insufficientClaims,
    supportRate,
    feedbackRounds: feedback ? feedback.total_rounds_executed : null,
    terminatedReason: feedback ? feedback.terminated_reason : null,
    claims,
    claimWarnings,
    evidence: evidenceRecords,
    feedback,
    report,
    audit: auditPayload,
  };
}

const detailCache = new Map<
  string,
  { data: ResearchRunDetail | null; expiry: number }
>();
let listCache: { data: RunSummary[]; expiry: number } | null = null;
const CACHE_TTL_MS = 30_000;

export const getResearchRun = cache(
  async (runId: string): Promise<ResearchRunDetail | null> => {
    const normalized = runId.trim().toLowerCase();
    if (!normalized) return null;
    const now = Date.now();
    const cached = detailCache.get(normalized);
    if (cached && cached.expiry > now) {
      return cached.data;
    }
    const detail = await loadRunInternal(normalized);
    detailCache.set(normalized, { data: detail, expiry: now + CACHE_TTL_MS });
    return detail;
  },
);

export const listResearchRuns = cache(async (): Promise<RunSummary[]> => {
  const now = Date.now();
  if (listCache && listCache.expiry > now) {
    return listCache.data;
  }

  let runList: ApiRunListResponse;
  try {
    runList = await listRuns();
  } catch (error) {
    if (
      error instanceof ApiError &&
      (error.kind === "network" || error.status === 503)
    ) {
      return [];
    }
    throw error;
  }

  const runIds = new Set<string>();
  for (const item of runList.historical_runs) {
    if (item.run_id) runIds.add(item.run_id.toLowerCase());
  }
  for (const job of runList.jobs) {
    if (job.run_id) runIds.add(job.run_id.toLowerCase());
  }

  const detailPromises = Array.from(runIds).map(async (runId) => {
    try {
      return await getResearchRun(runId);
    } catch {
      return null;
    }
  });

  const details = await Promise.all(detailPromises);
  const summaries: RunSummary[] = [];

  for (const detail of details) {
    if (!detail) continue;
    summaries.push({
      runId: detail.runId,
      shortId: detail.shortId,
      topic: detail.topic,
      createdAt: detail.createdAt,
      hasEvidence: detail.hasEvidence,
      hasClaims: detail.hasClaims,
      hasFeedback: detail.hasFeedback,
      hasReport: detail.hasReport,
      evidenceCount: detail.evidenceCount,
      fullTextCount: detail.fullTextCount,
      snippetOnlyCount: detail.snippetOnlyCount,
      failedCount: detail.failedCount,
      totalClaims: detail.totalClaims,
      supportedClaims: detail.supportedClaims,
      contradictedClaims: detail.contradictedClaims,
      insufficientClaims: detail.insufficientClaims,
      supportRate: detail.supportRate,
      feedbackRounds: detail.feedbackRounds,
      terminatedReason: detail.terminatedReason,
    });
  }

  summaries.sort((a, b) => {
    const aTime = a.createdAt
      ? Date.parse(a.createdAt)
      : Number.NEGATIVE_INFINITY;
    const bTime = b.createdAt
      ? Date.parse(b.createdAt)
      : Number.NEGATIVE_INFINITY;
    if (aTime !== bTime) return bTime - aTime;
    return a.runId.localeCompare(b.runId);
  });

  listCache = { data: summaries, expiry: now + CACHE_TTL_MS };
  return summaries;
});

export const getLatestRunId = cache(async (): Promise<string | null> => {
  const runs = await listResearchRuns();
  return runs[0]?.runId ?? null;
});
