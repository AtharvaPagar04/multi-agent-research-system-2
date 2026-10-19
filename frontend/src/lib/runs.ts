import fs from "node:fs";
import path from "node:path";
import { cache } from "react";

import {
  artifactsDir,
  findReportFilesForRun,
  listRunIdsFromArtifacts,
  safeReadJson,
  shortRunId,
} from "./fs-paths";
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

function runIdMatches(raw: Record<string, unknown>, runId: string): boolean {
  const internal = raw.run_id;
  if (typeof internal !== "string" || !internal.trim()) return true;
  return internal.trim().toLowerCase() === runId.toLowerCase();
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
  // No report on disk: expose a compact claim-context label for navigation only.
  const first = claims.find((claim) => claim.claim_text?.trim());
  if (!first) return null;
  const text = first.claim_text.trim().replace(/\s+/g, " ");
  return text.length > 96 ? `${text.slice(0, 93)}…` : text;
}

function loadRunInternal(runId: string): ResearchRunDetail | null {
  const dir = artifactsDir();
  const evidencePath = path.join(dir, `evidence_${runId}.json`);
  const claimsPath = path.join(dir, `claim_verifications_${runId}.json`);
  const feedbackPath = path.join(dir, `feedback_loop_${runId}.json`);

  // Validate artifact shape before use; malformed or mismatched files are
  // treated as unavailable rather than partially trusted.
  const evidenceRaw = safeReadJson<unknown>(evidencePath);
  const evidenceArtifact =
    isPlainObject(evidenceRaw) &&
    Array.isArray(evidenceRaw.records) &&
    runIdMatches(evidenceRaw, runId)
      ? evidenceRaw
      : null;

  const claimsRaw = safeReadJson<unknown>(claimsPath);
  const claimsArtifact =
    isPlainObject(claimsRaw) && Array.isArray(claimsRaw.records)
      ? claimsRaw
      : null;

  const feedbackRaw = safeReadJson<unknown>(feedbackPath);
  const feedbackArtifact =
    isPlainObject(feedbackRaw) &&
    Array.isArray(feedbackRaw.iterations) &&
    runIdMatches(feedbackRaw, runId)
      ? feedbackRaw
      : null;

  const hasEvidence = evidenceArtifact !== null;
  const hasClaims = claimsArtifact !== null;
  const hasFeedback = feedbackArtifact !== null;

  if (!hasEvidence && !hasClaims && !hasFeedback) return null;

  const evidenceRecords: EvidenceRecord[] = hasEvidence
    ? (evidenceArtifact.records as unknown[])
        .filter(isPlainObject)
        .map(normalizeEvidence)
    : [];

  const claims = hasClaims
    ? (claimsArtifact.records as unknown[])
        .filter(isPlainObject)
        .map(normalizeClaim)
    : [];

  const reportPaths = findReportFilesForRun(runId);
  let report = null;
  for (const reportPath of reportPaths) {
    try {
      const markdown = fs.readFileSync(reportPath, "utf8");
      const parsed = parseReportMarkdown(markdown, path.relative(path.dirname(dir), reportPath));
      // Prefer title-bearing research reports over short audit notes.
      if (!report || (parsed.title && !report.title)) {
        report = parsed;
      }
    } catch {
      // skip unreadable report files
    }
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

  // Trust artifact counts only when they are numeric and agree with the
  // sanitized records actually rendered; otherwise compute from records.
  const computedSupported = claims.filter(
    (claim) => claim.verdict === "supported",
  ).length;
  const computedContradicted = claims.filter(
    (claim) => claim.verdict === "contradicted",
  ).length;
  const computedInsufficient = claims.filter(
    (claim) => claim.verdict === "insufficient_evidence",
  ).length;

  const totalClaims = hasClaims ? claims.length : null;
  const supportedClaims = hasClaims ? computedSupported : null;
  const contradictedClaims = hasClaims ? computedContradicted : null;
  const insufficientClaims = hasClaims ? computedInsufficient : null;

  const supportRate =
    totalClaims != null && supportedClaims != null && totalClaims > 0
      ? supportedClaims / totalClaims
      : null;

  const createdAt = hasEvidence
    ? toTrimmedString(evidenceArtifact.created_at)
    : null;

  const topic = resolveTopic(report?.title ?? null, claims);

  const feedback: FeedbackArtifact | null = hasFeedback
    ? {
        run_id: runId,
        max_rounds: toCount(feedbackArtifact.max_rounds),
        total_rounds_executed: toCount(feedbackArtifact.total_rounds_executed),
        terminated_reason: toTrimmedString(feedbackArtifact.terminated_reason),
        final_claims_supported_count: toCount(
          feedbackArtifact.final_claims_supported_count,
        ),
        final_unresolved_claims_count: toCount(
          feedbackArtifact.final_unresolved_claims_count,
        ),
        final_action_policy: toTrimmedString(feedbackArtifact.final_action_policy),
        iterations: (feedbackArtifact.iterations as unknown[]).filter(
          isPlainObject,
        ).map(normalizeIteration),
      }
    : null;

  return {
    runId,
    shortId: shortRunId(runId),
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
    claimWarnings:
      hasClaims && Array.isArray(claimsArtifact.warnings)
        ? claimsArtifact.warnings.filter(
            (warning): warning is string => typeof warning === "string",
          )
        : [],
    evidence: evidenceRecords,
    feedback,
    report,
  };
}

export const listResearchRuns = cache((): RunSummary[] => {
  const runIds = listRunIdsFromArtifacts();
  const summaries: RunSummary[] = [];

  for (const runId of runIds) {
    const detail = loadRunInternal(runId);
    if (!detail) continue;
    const summary: RunSummary = {
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
    };
    summaries.push(summary);
  }

  summaries.sort((a, b) => {
    const aTime = a.createdAt ? Date.parse(a.createdAt) : Number.NEGATIVE_INFINITY;
    const bTime = b.createdAt ? Date.parse(b.createdAt) : Number.NEGATIVE_INFINITY;
    if (aTime !== bTime) return bTime - aTime;
    return a.runId.localeCompare(b.runId);
  });

  return summaries;
});

export const getResearchRun = cache(
  (runId: string): ResearchRunDetail | null => loadRunInternal(runId),
);

export const getLatestRunId = cache((): string | null => {
  const runs = listResearchRuns();
  return runs[0]?.runId ?? null;
});
