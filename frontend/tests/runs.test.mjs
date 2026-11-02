/**
 * Offline unit tests for frontend run integration (src/lib/runs.ts).
 *
 * Verifies that research runs and artifacts are assembled entirely via the
 * backend API client without directly reading research artifacts from the local
 * filesystem.
 */

import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { ApiError } from "../.test-build/api.js";
import { getLatestRunId, getResearchRun, listResearchRuns } from "../.test-build/runs.js";

const RUN_ID = "d".repeat(32);

function jsonResponse(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function textResponse(body, status = 200) {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/markdown" },
  });
}

function mockFetchRouter(routes) {
  return async (url) => {
    const parsed = new URL(String(url));
    const pathname = parsed.pathname;
    const handler = routes[pathname];
    if (handler) {
      return handler(pathname);
    }
    return jsonResponse({ detail: "Not found" }, 404);
  };
}

async function withFetch(fake, fn) {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = fake;
  try {
    await fn();
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test("getResearchRun integrates claims, evidence, feedback, report, and audit", async () => {
  const fake = mockFetchRouter({
    [`/api/runs/${RUN_ID}/report`]: () =>
      textResponse("# Research Summary: Open-Weight Models on 4GB VRAM\n\nBody content.\n\n*Source: [1]*\n"),
    [`/api/runs/${RUN_ID}/claims`]: () =>
      jsonResponse({
        total_claims: 2,
        records: [
          { claim_id: "c1", claim_text: "Q4 fits in 4GB", verdict: "supported", source_url: "https://example.com" },
          { claim_id: "c2", claim_text: "Q8 fits in 4GB", verdict: "contradicted", source_url: "https://example.com" },
        ],
        warnings: ["Warning 1"],
      }),
    [`/api/runs/${RUN_ID}/evidence`]: () =>
      jsonResponse({
        version: 1,
        run_id: RUN_ID,
        created_at: "2026-10-18T15:39:55Z",
        record_count: 1,
        records: [
          {
            evidence_id: "e1",
            source_url: "https://example.com",
            retrieval_status: "full_text",
            captured_text: "Benchmark data...",
          },
        ],
      }),
    [`/api/runs/${RUN_ID}/feedback`]: () =>
      jsonResponse({
        run_id: RUN_ID,
        max_rounds: 3,
        total_rounds_executed: 1,
        terminated_reason: "all_supported",
        final_action_policy: "label",
        iterations: [
          {
            iteration_index: 0,
            queries_issued: ["test query"],
            new_evidence_ids: ["e1"],
            claims_checked_count: 2,
            verdict_breakdown: { supported: 1, contradicted: 1 },
          },
        ],
      }),
    [`/api/runs/${RUN_ID}/audit`]: () =>
      jsonResponse({
        run_id: RUN_ID,
        report_available: true,
        audit_lines: [{ label: "Source", detail: "[1]", raw: "Source: [1]" }],
        citation_audit: { matched: 1, total: 1, detail: "1/1 verified" },
        claim_audit: { supported: 1, total: 2, detail: "1/2 supported" },
        metric_audit: { traceable: 1, total: 1, detail: "1/1 traceable" },
        warnings: ["Audit warning"],
      }),
  });

  await withFetch(fake, async () => {
    const run = await getResearchRun(RUN_ID);
    assert.ok(run, "Run detail should be returned");
    assert.equal(run.runId, RUN_ID);
    assert.equal(run.topic, "Open-Weight Models on 4GB VRAM");
    assert.equal(run.hasReport, true);
    assert.equal(run.hasClaims, true);
    assert.equal(run.hasEvidence, true);
    assert.equal(run.hasFeedback, true);
    assert.equal(run.totalClaims, 2);
    assert.equal(run.supportedClaims, 1);
    assert.equal(run.contradictedClaims, 1);
    assert.equal(run.supportRate, 0.5);
    assert.equal(run.evidenceCount, 1);
    assert.equal(run.fullTextCount, 1);
    assert.equal(run.claims.length, 2);
    assert.equal(run.evidence.length, 1);
    assert.equal(run.feedback?.total_rounds_executed, 1);
    assert.ok(run.claimWarnings.includes("Warning 1"));
    assert.ok(run.claimWarnings.includes("Audit warning"));
  });
});

test("getResearchRun handles missing report and missing feedback gracefully", async () => {
  const missingPartialsRunId = "e".repeat(32);
  const fake = mockFetchRouter({
    [`/api/runs/${missingPartialsRunId}/claims`]: () =>
      jsonResponse({
        total_claims: 1,
        records: [
          { claim_id: "c1", claim_text: "Headless run claim", verdict: "supported" },
        ],
        warnings: [],
      }),
    [`/api/runs/${missingPartialsRunId}/evidence`]: () =>
      jsonResponse({
        version: 1,
        run_id: missingPartialsRunId,
        created_at: "2026-10-18T16:00:00Z",
        record_count: 1,
        records: [
          { evidence_id: "e1", source_url: "https://example.com", retrieval_status: "snippet_only" },
        ],
      }),
    // report, feedback, and audit return 404 (default)
  });

  await withFetch(fake, async () => {
    const run = await getResearchRun(missingPartialsRunId);
    assert.ok(run, "Run detail should load even without report or feedback");
    assert.equal(run.hasReport, false);
    assert.equal(run.report, null);
    assert.equal(run.hasFeedback, false);
    assert.equal(run.feedback, null);
    assert.equal(run.hasClaims, true);
    assert.equal(run.hasEvidence, true);
    assert.equal(run.snippetOnlyCount, 1);
    assert.equal(run.topic, "Headless run claim");
  });
});

test("getResearchRun returns null when all artifact endpoints return 404", async () => {
  const unknownRunId = "0".repeat(32);
  const fake = mockFetchRouter({}); // All routes return 404

  await withFetch(fake, async () => {
    const run = await getResearchRun(unknownRunId);
    assert.equal(run, null);
  });
});

test("getResearchRun throws ApiError when backend has connection failure", async () => {
  const fake = async () => {
    throw new TypeError("fetch failed");
  };

  await withFetch(fake, async () => {
    await assert.rejects(getResearchRun("c".repeat(32)), (err) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.kind, "network");
      return true;
    });
  });
});

test("listResearchRuns resolves runs and handles empty/offline state", async () => {
  const run1 = "1".repeat(32);
  const fake = mockFetchRouter({
    "/api/runs": () =>
      jsonResponse({
        jobs: [],
        historical_runs: [
          {
            origin: "artifact",
            run_id: run1,
            artifacts: {
              report_available: false,
              evidence_available: true,
              claim_verifications_available: true,
              feedback_loop_available: false,
            },
          },
        ],
      }),
    [`/api/runs/${run1}/claims`]: () =>
      jsonResponse({
        total_claims: 1,
        records: [{ claim_id: "c1", claim_text: "Historical claim", verdict: "supported" }],
        warnings: [],
      }),
    [`/api/runs/${run1}/evidence`]: () =>
      jsonResponse({
        version: 1,
        run_id: run1,
        created_at: "2026-10-18T12:00:00Z",
        record_count: 1,
        records: [{ evidence_id: "e1", source_url: "https://example.com", retrieval_status: "full_text" }],
      }),
  });

  await withFetch(fake, async () => {
    const summaries = await listResearchRuns();
    assert.ok(Array.isArray(summaries));
    assert.equal(summaries.length, 1);
    assert.equal(summaries[0].runId, run1);
    assert.equal(summaries[0].hasEvidence, true);
    assert.equal(summaries[0].hasClaims, true);
    assert.equal(summaries[0].hasReport, false);

    const latest = await getLatestRunId();
    assert.equal(latest, run1);
  });
});

test("frontend/src has zero imports or references to node:fs", () => {
  const srcDir = path.resolve(import.meta.dirname, "../src");
  const violatingFiles = [];

  function checkDir(dir) {
    const entries = fs.readdirSync(dir, { withFileTypes: true });
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        checkDir(full);
      } else if (/\.(ts|tsx|js|jsx)$/.test(entry.name)) {
        const content = fs.readFileSync(full, "utf8");
        if (
          /from\s+["'](node:)?fs["']/.test(content) ||
          /require\(["'](node:)?fs["']\)/.test(content)
        ) {
          violatingFiles.push(path.relative(srcDir, full));
        }
      }
    }
  }

  checkDir(srcDir);
  assert.deepEqual(
    violatingFiles,
    [],
    `Files in frontend/src must not import node:fs: ${violatingFiles.join(", ")}`,
  );
});
