export const UNAVAILABLE = "Unavailable";

export function formatNumber(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE;
  return new Intl.NumberFormat("en-US").format(value);
}

export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE;
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return UNAVAILABLE;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Intl.DateTimeFormat("en-US", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "UTC",
  }).format(parsed) + " UTC";
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return UNAVAILABLE;
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) return value;
  return new Intl.DateTimeFormat("en-US", {
    year: "numeric",
    month: "short",
    day: "2-digit",
    timeZone: "UTC",
  }).format(parsed);
}

export function truncate(value: string | null | undefined, max = 180): string {
  if (!value) return "";
  const clean = value.replace(/\s+/g, " ").trim();
  if (clean.length <= max) return clean;
  return `${clean.slice(0, max - 1)}…`;
}

export function verdictLabel(
  verdict: string | null | undefined,
): "Supported" | "Contradicted" | "Insufficient" | "Unknown" {
  if (verdict === "supported") return "Supported";
  if (verdict === "contradicted") return "Contradicted";
  if (verdict === "insufficient_evidence") return "Insufficient";
  return "Unknown";
}

export function retrievalLabel(
  status: string | null | undefined,
): "Full text" | "Snippet" | "Failed" | "Unknown" {
  if (status === "full_text") return "Full text";
  if (status === "snippet_only") return "Snippet";
  if (status === "failed") return "Failed";
  return "Unknown";
}
