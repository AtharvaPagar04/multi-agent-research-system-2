import type {
  ClaimRecord,
  ClaimVerdict,
  ReportAuditLine,
  ReportSource,
  ResearchReportArtifact,
  RetrievalStatus,
} from "./types";

const SOURCE_BLOCK_RE =
  /^\[(\d+)\]\s*(.+?)\n\s*(https?:\/\/\S+|\[No URL[^\]]*\]|None)\s*$/gm;

const AUDIT_LINE_RE = /^\*([^*].*?)\*\s*$/;

function normalizeVerdict(value: unknown): ClaimVerdict | null {
  if (value === "supported" || value === "contradicted" || value === "insufficient_evidence") {
    return value;
  }
  return null;
}

function normalizeRetrieval(value: unknown): RetrievalStatus {
  if (value === "full_text" || value === "snippet_only" || value === "failed") {
    return value;
  }
  return null;
}

export function normalizeClaim(raw: Record<string, unknown>): ClaimRecord {
  const supporting = raw.supporting_urls;
  return {
    claim_id: String(raw.claim_id ?? ""),
    claim_text: String(raw.claim_text ?? ""),
    source_url: raw.source_url == null ? null : String(raw.source_url),
    passage_id: raw.passage_id == null ? null : String(raw.passage_id),
    passage_text: raw.passage_text == null ? null : String(raw.passage_text),
    verdict: normalizeVerdict(raw.verdict),
    rationale: raw.rationale == null ? null : String(raw.rationale),
    action_taken: raw.action_taken == null ? null : String(raw.action_taken),
    retrieval_status: normalizeRetrieval(raw.retrieval_status),
    supporting_urls: Array.isArray(supporting)
      ? supporting.map((url) => String(url))
      : undefined,
  };
}

export function parseReportMarkdown(
  markdown: string,
  filePath: string,
): ResearchReportArtifact {
  const lines = markdown.split(/\r?\n/);
  let title: string | null = null;

  for (const line of lines) {
    const heading = /^#\s+(.+)$/.exec(line.trim());
    if (heading) {
      title = heading[1].replace(/^Research Summary:\s*/i, "").trim() || heading[1].trim();
      break;
    }
  }

  const sources: ReportSource[] = [];
  for (const match of markdown.matchAll(SOURCE_BLOCK_RE)) {
    const url = match[3];
    const isMissing =
      !url ||
      url.startsWith("[") ||
      url.toLowerCase() === "none" ||
      url.toLowerCase().startsWith("none");
    sources.push({
      marker: match[1],
      title: match[2].trim(),
      url: isMissing ? null : url.trim(),
    });
  }

  const auditLines: ReportAuditLine[] = [];
  for (const line of lines) {
    const trimmed = line.trim();
    const match = AUDIT_LINE_RE.exec(trimmed);
    if (!match) continue;
    const body = match[1].trim();
    const sep = body.indexOf(":");
    if (sep === -1) continue;
    const label = body.slice(0, sep).trim();
    const detail = body.slice(sep + 1).trim();
    if (!label || !detail) continue;
    auditLines.push({ label, detail, raw: body });
  }

  const claimAuditLine = auditLines.find((line) =>
    /^claim verification$/i.test(line.label),
  );
  let claimSupported: number | null = null;
  let claimTotal: number | null = null;
  if (claimAuditLine) {
    const m = /(\d+)\s*\/\s*(\d+)/.exec(claimAuditLine.detail);
    if (m) {
      claimSupported = Number(m[1]);
      claimTotal = Number(m[2]);
    }
  }

  const metricAuditLine = auditLines.find((line) =>
    /^metrics audit$/i.test(line.label),
  );
  let metricTraceable: number | null = null;
  let metricTotal: number | null = null;
  if (metricAuditLine) {
    const m = /(\d+)\s*\/\s*(\d+)/.exec(metricAuditLine.detail);
    if (m) {
      metricTraceable = Number(m[1]);
      metricTotal = Number(m[2]);
    }
  }

  return {
    path: filePath,
    title,
    markdown,
    sources,
    auditLines,
    metricAudit: {
      traceable: metricTraceable,
      total: metricTotal,
      detail: metricAuditLine?.detail ?? null,
    },
    claimAudit: {
      supported: claimSupported,
      total: claimTotal,
      detail: claimAuditLine?.detail ?? null,
    },
  };
}
