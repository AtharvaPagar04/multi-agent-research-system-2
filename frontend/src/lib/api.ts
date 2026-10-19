/**
 * Typed client for the local research backend (backend/app.py).
 *
 * The base URL comes from NEXT_PUBLIC_API_BASE_URL (inlined by Next.js at
 * build time) and defaults to the local development backend at
 * http://127.0.0.1:8000. Every failure mode (network unreachable, timeout,
 * HTTP error, unreadable payload) is normalized into an ApiError so the UI
 * never crashes on a missing or misbehaving backend.
 *
 * This module is intentionally dependency-free and side-effect-free so it
 * can be unit-tested with `node --test` after a plain `tsc` build
 * (see tsconfig.test.json and tests/api.test.mjs).
 */

export const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
export const MAX_TOPIC_LENGTH = 500;
export const REQUEST_TIMEOUT_MS = 10_000;

export type ApiOrigin = "manager" | "artifact";

export interface ApiArtifactAvailability {
  report_available: boolean;
  evidence_available: boolean;
  claim_verifications_available: boolean;
  feedback_loop_available: boolean;
}

export interface ApiJobSnapshot {
  origin: ApiOrigin;
  job_id: string;
  topic: string | null;
  state: string | null;
  stage: string | null;
  created_at: number | null;
  started_at: number | null;
  finished_at: number | null;
  elapsed_seconds: number | null;
  exit_code: number | null;
  run_id: string | null;
  error: string | null;
  failure_kind: string | null;
  stdout: string | null;
  stderr: string | null;
  stdout_truncated: boolean | null;
  stderr_truncated: boolean | null;
  artifacts: ApiArtifactAvailability | null;
}

export interface ApiHistoricalRun {
  origin: "artifact";
  run_id: string;
  artifacts: ApiArtifactAvailability;
}

export interface ApiRunListResponse {
  jobs: ApiJobSnapshot[];
  historical_runs: ApiHistoricalRun[];
}

export interface ApiCancelResponse {
  accepted: boolean;
  job: ApiJobSnapshot;
}

export type ApiErrorKind = "network" | "timeout" | "http" | "invalid_response";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | null;
  readonly detail: string;

  constructor(kind: ApiErrorKind, status: number | null, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.kind = kind;
    this.status = status;
    this.detail = detail;
  }
}

const ACTIVE_STATES = new Set(["queued", "starting", "running"]);
const TERMINAL_STATES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);

export function isActiveState(state: string | null | undefined): boolean {
  return state != null && ACTIVE_STATES.has(state);
}

export function isTerminalState(state: string | null | undefined): boolean {
  return state != null && TERMINAL_STATES.has(state);
}

export function activeJobIds(jobs: ApiJobSnapshot[]): string[] {
  return jobs
    .filter((job) => job.origin === "manager" && isActiveState(job.state))
    .map((job) => job.job_id);
}

export function canCancel(job: ApiJobSnapshot): boolean {
  return job.origin === "manager" && isActiveState(job.state);
}

export function shouldFetchReport(job: ApiJobSnapshot): boolean {
  return (
    job.origin === "manager" &&
    job.state === "completed" &&
    job.artifacts?.report_available === true
  );
}

export function artifactLabels(
  artifacts: ApiArtifactAvailability | null,
): string[] {
  if (!artifacts) return [];
  const labels: string[] = [];
  if (artifacts.report_available) labels.push("report");
  if (artifacts.evidence_available) labels.push("evidence");
  if (artifacts.claim_verifications_available) labels.push("claims");
  if (artifacts.feedback_loop_available) labels.push("feedback");
  return labels;
}

export function artifactRunHref(job: ApiJobSnapshot): string | null {
  return job.origin === "manager" && job.run_id
    ? `/runs/${job.run_id}/report`
    : null;
}

export function cancelOutcomeMessage(
  accepted: boolean,
  job: ApiJobSnapshot,
): string {
  const shortId = job.job_id.slice(0, 8);
  if (!accepted) {
    return `The backend did not accept cancellation — the job is ${
      job.state ?? "no longer active"
    }.`;
  }
  if (isTerminalState(job.state)) {
    return `Job ${shortId}… is now ${job.state}.`;
  }
  return `Cancellation requested for ${shortId}…. Running jobs stop on a best-effort basis; polling will confirm the final state.`;
}

export type TopicValidation =
  | { ok: true; topic: string }
  | { ok: false; message: string };

export function validateTopic(raw: string): TopicValidation {
  const topic = raw.trim();
  if (!topic) {
    return { ok: false, message: "Enter a research topic first." };
  }
  if (topic.length > MAX_TOPIC_LENGTH) {
    return {
      ok: false,
      message: `Topic must be at most ${MAX_TOPIC_LENGTH} characters (currently ${topic.length}).`,
    };
  }
  return { ok: true, topic };
}

export function apiBaseUrl(): string {
  const raw = process.env.NEXT_PUBLIC_API_BASE_URL;
  const trimmed = typeof raw === "string" ? raw.trim() : "";
  if (!trimmed) return DEFAULT_API_BASE_URL;
  return trimmed.replace(/\/+$/, "");
}

export function describeApiError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.kind === "timeout") {
      return "The backend did not respond in time. Check that it is running and try again.";
    }
    if (error.kind === "network") {
      return `Could not reach the research backend at ${apiBaseUrl()}. Start it with: .venv313/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000`;
    }
    if (error.status === 429) {
      return `Backend queue is full: ${error.detail}`;
    }
    if (error.status === 503) {
      return "The backend job manager is not accepting requests right now.";
    }
    return error.detail;
  }
  return "Unexpected error while talking to the research backend.";
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)
    : {};
}

function asStringOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function asNumberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function normalizeAvailability(raw: unknown): ApiArtifactAvailability {
  const record = asRecord(raw);
  return {
    report_available: record.report_available === true,
    evidence_available: record.evidence_available === true,
    claim_verifications_available:
      record.claim_verifications_available === true,
    feedback_loop_available: record.feedback_loop_available === true,
  };
}

function normalizeJob(raw: unknown): ApiJobSnapshot {
  const record = asRecord(raw);
  const origin: ApiOrigin = record.origin === "artifact" ? "artifact" : "manager";
  const artifactsRaw = asRecord(record.artifacts);
  const hasArtifacts = Object.keys(artifactsRaw).length > 0;
  return {
    origin,
    job_id: asStringOrNull(record.job_id) ?? "",
    topic: asStringOrNull(record.topic),
    state: asStringOrNull(record.state),
    stage: asStringOrNull(record.stage),
    created_at: asNumberOrNull(record.created_at),
    started_at: asNumberOrNull(record.started_at),
    finished_at: asNumberOrNull(record.finished_at),
    elapsed_seconds: asNumberOrNull(record.elapsed_seconds),
    exit_code: asNumberOrNull(record.exit_code),
    run_id: asStringOrNull(record.run_id),
    error: asStringOrNull(record.error),
    failure_kind: asStringOrNull(record.failure_kind),
    stdout: asStringOrNull(record.stdout),
    stderr: asStringOrNull(record.stderr),
    stdout_truncated:
      typeof record.stdout_truncated === "boolean"
        ? record.stdout_truncated
        : null,
    stderr_truncated:
      typeof record.stderr_truncated === "boolean"
        ? record.stderr_truncated
        : null,
    artifacts: hasArtifacts ? normalizeAvailability(artifactsRaw) : null,
  };
}

function parseDetailFromBody(text: string, fallback: string): string {
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    if (typeof parsed.detail === "string" && parsed.detail.trim()) {
      return parsed.detail;
    }
  } catch {
    // not JSON — keep the fallback
  }
  return fallback;
}

interface RequestSpec {
  method: "GET" | "POST";
  path: string;
  body?: string;
  accept: string;
}

async function request(spec: RequestSpec, read: "json" | "text"): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl()}${spec.path}`, {
      method: spec.method,
      body: spec.body,
      signal: controller.signal,
      cache: "no-store",
      headers: spec.body
        ? { Accept: spec.accept, "Content-Type": "application/json" }
        : { Accept: spec.accept },
    });
  } catch (error) {
    if (
      controller.signal.aborted ||
      (error instanceof DOMException && error.name === "AbortError")
    ) {
      throw new ApiError("timeout", null, "The backend request timed out.");
    }
    throw new ApiError("network", null, "Could not reach the research backend.");
  } finally {
    clearTimeout(timer);
  }

  if (!response.ok) {
    const text = await response.text().catch(() => "");
    const detail = parseDetailFromBody(
      text,
      `Request failed with status ${response.status}.`,
    );
    throw new ApiError("http", response.status, detail);
  }

  if (read === "text") {
    return await response.text();
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new ApiError(
      "invalid_response",
      response.status,
      "The backend returned an unreadable response.",
    );
  }
  return payload;
}

export async function createRun(topic: string): Promise<ApiJobSnapshot> {
  const payload = await request(
    {
      method: "POST",
      path: "/api/runs",
      body: JSON.stringify({ topic }),
      accept: "application/json",
    },
    "json",
  );
  const job = normalizeJob(payload);
  if (!job.job_id) {
    throw new ApiError(
      "invalid_response",
      202,
      "The backend accepted the submission but returned no job identifier.",
    );
  }
  return job;
}

export async function listRuns(): Promise<ApiRunListResponse> {
  const payload = await request(
    { method: "GET", path: "/api/runs", accept: "application/json" },
    "json",
  );
  const record = asRecord(payload);
  if (!Array.isArray(record.jobs) || !Array.isArray(record.historical_runs)) {
    throw new ApiError(
      "invalid_response",
      200,
      "The backend returned an unexpected run list payload.",
    );
  }
  const historicalRuns: ApiHistoricalRun[] = record.historical_runs
    .map((entry) => {
      const raw = asRecord(entry);
      const runId = asStringOrNull(raw.run_id) ?? "";
      return {
        origin: "artifact" as const,
        run_id: runId,
        artifacts: normalizeAvailability(raw.artifacts),
      };
    })
    .filter((entry) => entry.run_id.length > 0);
  return {
    jobs: record.jobs.map(normalizeJob).filter((job) => job.job_id.length > 0),
    historical_runs: historicalRuns,
  };
}

export async function getRun(jobId: string): Promise<ApiJobSnapshot> {
  const payload = await request(
    {
      method: "GET",
      path: `/api/runs/${encodeURIComponent(jobId)}`,
      accept: "application/json",
    },
    "json",
  );
  return normalizeJob(payload);
}

export async function cancelRun(jobId: string): Promise<ApiCancelResponse> {
  const payload = await request(
    {
      method: "POST",
      path: `/api/runs/${encodeURIComponent(jobId)}/cancel`,
      accept: "application/json",
    },
    "json",
  );
  const record = asRecord(payload);
  return {
    accepted: record.accepted === true,
    job: normalizeJob(record.job),
  };
}

export async function fetchRunReport(jobId: string): Promise<string> {
  const payload = await request(
    {
      method: "GET",
      path: `/api/runs/${encodeURIComponent(jobId)}/report`,
      accept: "text/markdown, text/plain",
    },
    "text",
  );
  return typeof payload === "string" ? payload : "";
}
