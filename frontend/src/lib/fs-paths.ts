/**
 * Identifier utilities for research runs.
 *
 * Direct filesystem reading of research artifacts has been removed in favor
 * of typed calls to the FastAPI backend (/api/runs endpoints).
 */

const RUN_ID_RE = /([0-9a-f]{32})/i;

export function extractRunId(value: string): string | null {
  const match = value.match(RUN_ID_RE);
  return match ? match[1].toLowerCase() : null;
}

export function shortRunId(runId: string): string {
  return runId.slice(0, 8);
}
