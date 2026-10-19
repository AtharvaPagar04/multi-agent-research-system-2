import fs from "node:fs";
import path from "node:path";

const RUN_ID_RE = /([0-9a-f]{32})/i;
const EVIDENCE_RUN_RE = /^evidence_([0-9a-f]{32})\.json$/i;
const CLAIMS_RUN_RE = /^claim_verifications_([0-9a-f]{32})\.json$/i;
const FEEDBACK_RUN_RE = /^feedback_loop_([0-9a-f]{32})\.json$/i;
const RUN_ID_INLINE_RE = /Evidence store run ID:\s*`([0-9a-f]{32})`/i;

export function repoRoot(): string {
  const override = process.env.RESEARCH_REPO_ROOT;
  if (override && override.trim()) return path.resolve(override);
  return path.resolve(process.cwd(), "..");
}

export function artifactsDir(): string {
  const override = process.env.RESEARCH_ARTIFACTS_DIR;
  if (override && override.trim()) return path.resolve(override);
  return path.join(repoRoot(), "evidence_runs");
}

export function extractRunId(value: string): string | null {
  const match = value.match(RUN_ID_RE);
  return match ? match[1].toLowerCase() : null;
}

export function shortRunId(runId: string): string {
  return runId.slice(0, 8);
}

export function safeReadJson<T>(filePath: string): T | null {
  try {
    if (!fs.existsSync(filePath)) return null;
    const raw = fs.readFileSync(filePath, "utf8");
    if (!raw.trim()) return null;
    return JSON.parse(raw) as T;
  } catch {
    return null;
  }
}

export function listRunIdsFromArtifacts(): string[] {
  const dir = artifactsDir();
  let entries: string[];
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return [];
  }

  const runIds = new Set<string>();
  for (const entry of entries) {
    const lower = entry.toLowerCase();
    const match =
      EVIDENCE_RUN_RE.exec(entry) ??
      CLAIMS_RUN_RE.exec(entry) ??
      FEEDBACK_RUN_RE.exec(entry);
    if (match) {
      runIds.add(match[1].toLowerCase());
      continue;
    }
    const generic = RUN_ID_RE.exec(lower);
    if (generic && (lower.endsWith(".json") || lower.endsWith(".md"))) {
      runIds.add(generic[1]);
    }
  }
  return [...runIds];
}

export function findReportFilesForRun(runId: string): string[] {
  const roots = [repoRoot(), artifactsDir()];
  const hits: string[] = [];
  const needle = runId.toLowerCase();

  for (const root of roots) {
    let entries: string[];
    try {
      entries = fs.readdirSync(root);
    } catch {
      continue;
    }

    for (const entry of entries) {
      if (!entry.toLowerCase().endsWith(".md")) continue;
      const full = path.join(root, entry);
      try {
        const stat = fs.statSync(full);
        if (!stat.isFile()) continue;
      } catch {
        continue;
      }

      const nameHit = entry.toLowerCase().includes(needle);
      if (nameHit) {
        hits.push(full);
        continue;
      }

      try {
        const head = fs.readFileSync(full, "utf8").slice(0, 12000);
        const inline = RUN_ID_INLINE_RE.exec(head);
        if (inline && inline[1].toLowerCase() === needle) {
          hits.push(full);
        }
      } catch {
        // ignore unreadable files
      }
    }
  }

  return [...new Set(hits)];
}
