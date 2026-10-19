/**
 * Offline unit tests for the backend API client (src/lib/api.ts).
 *
 * No test framework dependency: the TypeScript module is compiled with the
 * project's own tsc (tsconfig.test.json -> .test-build/api.js) and executed
 * with Node's built-in test runner. `fetch` is stubbed per test; no network
 * calls are made and no research topic is ever submitted to a real backend.
 *
 * Run with: npm test
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  DEFAULT_API_BASE_URL,
  MAX_TOPIC_LENGTH,
  activeJobIds,
  apiBaseUrl,
  artifactLabels,
  artifactRunHref,
  canCancel,
  cancelOutcomeMessage,
  cancelRun,
  createRun,
  describeApiError,
  fetchRunReport,
  getRun,
  isActiveState,
  isTerminalState,
  listRuns,
  shouldFetchReport,
  validateTopic,
} from "../.test-build/api.js";

const JOB_ID = "a".repeat(32);
const RUN_ID = "f".repeat(32);

function jobFixture(overrides = {}) {
  return {
    origin: "manager",
    job_id: JOB_ID,
    topic: "sample topic",
    state: "running",
    stage: "running",
    created_at: 1000,
    started_at: 1001,
    finished_at: null,
    elapsed_seconds: 1,
    exit_code: null,
    run_id: null,
    error: null,
    failure_kind: null,
    stdout: "",
    stderr: "",
    stdout_truncated: false,
    stderr_truncated: false,
    artifacts: null,
    ...overrides,
  };
}

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

function captureFetch(responder) {
  const calls = [];
  const fake = async (url, init) => {
    calls.push({ url: String(url), init: init ?? {} });
    return responder(calls.length, { url: String(url), init: init ?? {} });
  };
  return { calls, fake };
}

async function withFetch(fake, fn) {
  const originalFetch = globalThis.fetch;
  const originalEnv = process.env.NEXT_PUBLIC_API_BASE_URL;
  globalThis.fetch = fake;
  try {
    await fn();
  } finally {
    globalThis.fetch = originalFetch;
    if (originalEnv === undefined) {
      delete process.env.NEXT_PUBLIC_API_BASE_URL;
    } else {
      process.env.NEXT_PUBLIC_API_BASE_URL = originalEnv;
    }
  }
}

test("validateTopic enforces trimmed non-empty topics up to the backend cap", () => {
  assert.deepEqual(validateTopic("  space physics  "), {
    ok: true,
    topic: "space physics",
  });
  assert.equal(validateTopic("").ok, false);
  assert.equal(validateTopic("   \n\t ").ok, false);
  assert.equal(validateTopic("x".repeat(MAX_TOPIC_LENGTH)).ok, true);
  const overlong = validateTopic("x".repeat(MAX_TOPIC_LENGTH + 1));
  assert.equal(overlong.ok, false);
  if (!overlong.ok) {
    assert.match(overlong.message, /at most 500 characters/);
  }
  // The cap applies to the trimmed value, not the raw input.
  assert.equal(
    validateTopic(` ${"x".repeat(MAX_TOPIC_LENGTH)} `).ok,
    true,
  );
});

test("apiBaseUrl uses the localhost default and trims env overrides", () => {
  delete process.env.NEXT_PUBLIC_API_BASE_URL;
  assert.equal(apiBaseUrl(), DEFAULT_API_BASE_URL);
  assert.equal(DEFAULT_API_BASE_URL, "http://127.0.0.1:8000");

  process.env.NEXT_PUBLIC_API_BASE_URL = "  http://127.0.0.1:9001///  ";
  assert.equal(apiBaseUrl(), "http://127.0.0.1:9001");
  delete process.env.NEXT_PUBLIC_API_BASE_URL;
});

test("createRun posts the topic and normalizes the snapshot", async () => {
  const { calls, fake } = captureFetch(() =>
    jsonResponse(jobFixture({ state: "queued", stage: "queued" }), 202),
  );

  await withFetch(fake, async () => {
    const job = await createRun("quantum error correction");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `${DEFAULT_API_BASE_URL}/api/runs`);
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.body, JSON.stringify({ topic: "quantum error correction" }));
    assert.equal(calls[0].init.headers["Content-Type"], "application/json");

    assert.equal(job.job_id, JOB_ID);
    assert.equal(job.origin, "manager");
    assert.equal(job.state, "queued");
  });
});

test("createRun surfaces backend 422 validation details", async () => {
  const { fake } = captureFetch(() =>
    jsonResponse({ detail: "topic must be a non-empty string" }, 422),
  );

  await withFetch(fake, async () => {
    await assert.rejects(createRun(""), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.kind, "http");
      assert.equal(error.status, 422);
      assert.equal(error.detail, "topic must be a non-empty string");
      return true;
    });
  });
});

test("createRun surfaces queue-full 429 with a readable message", async () => {
  const { fake } = captureFetch(() =>
    jsonResponse({ detail: "job queue is full (16 queued)" }, 429),
  );

  await withFetch(fake, async () => {
    try {
      await createRun("anything");
      assert.fail("expected ApiError");
    } catch (error) {
      assert.equal(error.status, 429);
      assert.match(describeApiError(error), /queue is full/i);
      assert.match(describeApiError(error), /16 queued/);
    }
  });
});

test("network failures map to a friendly, non-throwing ApiError", async () => {
  const fake = async () => {
    throw new TypeError("fetch failed");
  };

  await withFetch(fake, async () => {
    await assert.rejects(createRun("topic"), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.kind, "network");
      assert.equal(error.status, null);
      assert.match(describeApiError(error), /Could not reach the research backend/);
      return true;
    });
  });
});

test("aborted requests map to a timeout ApiError", async () => {
  const fake = async () => {
    throw new DOMException("The operation was aborted.", "AbortError");
  };

  await withFetch(fake, async () => {
    try {
      await listRuns();
      assert.fail("expected ApiError");
    } catch (error) {
      assert.ok(error instanceof ApiError);
      assert.equal(error.kind, "timeout");
      assert.match(describeApiError(error), /did not respond in time/);
    }
  });
});

test("unreadable JSON payloads map to invalid_response", async () => {
  const { fake } = captureFetch(() => textResponse("not json", 200));

  await withFetch(fake, async () => {
    try {
      await listRuns();
      assert.fail("expected ApiError");
    } catch (error) {
      assert.ok(error instanceof ApiError);
      assert.equal(error.kind, "invalid_response");
    }
  });
});

test("listRuns normalizes manager jobs and historical runs separately", async () => {
  const payload = {
    jobs: [jobFixture()],
    historical_runs: [
      {
        origin: "artifact",
        run_id: RUN_ID,
        artifacts: {
          report_available: true,
          evidence_available: true,
          claim_verifications_available: false,
          feedback_loop_available: false,
        },
      },
    ],
  };
  const { calls, fake } = captureFetch(() => jsonResponse(payload));

  await withFetch(fake, async () => {
    const data = await listRuns();
    assert.equal(calls[0].url, `${DEFAULT_API_BASE_URL}/api/runs`);
    assert.equal(data.jobs.length, 1);
    assert.equal(data.jobs[0].job_id, JOB_ID);
    assert.equal(data.historical_runs.length, 1);
    assert.equal(data.historical_runs[0].origin, "artifact");
    assert.equal(data.historical_runs[0].run_id, RUN_ID);
    assert.equal(data.historical_runs[0].artifacts.report_available, true);
  });
});

test("listRuns rejects malformed list payloads instead of inventing runs", async () => {
  const { fake } = captureFetch(() =>
    jsonResponse({ jobs: "not-an-array", historical_runs: [] }),
  );

  await withFetch(fake, async () => {
    await assert.rejects(listRuns(), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.kind, "invalid_response");
      return true;
    });
  });
});

test("getRun surfaces unknown-job 404 details", async () => {
  const { fake } = captureFetch(() =>
    jsonResponse({ detail: "unknown job ID" }, 404),
  );

  await withFetch(fake, async () => {
    try {
      await getRun("b".repeat(32));
      assert.fail("expected ApiError");
    } catch (error) {
      assert.equal(error.status, 404);
      assert.equal(error.detail, "unknown job ID");
    }
  });
});

test("cancelRun reflects the server's accepted flag and returned state", async () => {
  const rejected = jobFixture({ state: "completed", finished_at: 1005 });
  const accepted = jobFixture({ state: "cancelled", failure_kind: "cancelled" });
  let response = { accepted: true, job: accepted };
  const { calls, fake } = captureFetch(() => jsonResponse(response));

  await withFetch(fake, async () => {
    const result = await cancelRun(JOB_ID);
    assert.equal(calls[0].url, `${DEFAULT_API_BASE_URL}/api/runs/${JOB_ID}/cancel`);
    assert.equal(calls[0].init.method, "POST");
    assert.equal(result.accepted, true);
    assert.equal(result.job.state, "cancelled");

    response = { accepted: false, job: rejected };
    const declined = await cancelRun(JOB_ID);
    assert.equal(declined.accepted, false);
    assert.equal(declined.job.state, "completed");
  });
});

test("fetchRunReport returns markdown text and maps missing reports to 404", async () => {
  let status = 200;
  const { calls, fake } = captureFetch(() =>
    status === 200
      ? textResponse("# Report\n\nBody.\n")
      : jsonResponse({ detail: "report not found" }, 404),
  );

  await withFetch(fake, async () => {
    const markdown = await fetchRunReport(JOB_ID);
    assert.equal(markdown, "# Report\n\nBody.\n");
    assert.equal(calls[0].url, `${DEFAULT_API_BASE_URL}/api/runs/${JOB_ID}/report`);

    status = 404;
    try {
      await fetchRunReport(JOB_ID);
      assert.fail("expected ApiError");
    } catch (error) {
      assert.equal(error.status, 404);
      assert.equal(error.detail, "report not found");
    }
  });
});

test("state predicates gate cancellation and report retrieval correctly", () => {
  for (const state of ["queued", "starting", "running"]) {
    assert.equal(isActiveState(state), true);
    assert.equal(isTerminalState(state), false);
  }
  for (const state of ["completed", "failed", "cancelled", "interrupted"]) {
    assert.equal(isActiveState(state), false);
    assert.equal(isTerminalState(state), true);
  }
  assert.equal(isActiveState(null), false);
  assert.equal(isTerminalState(undefined), false);

  const running = jobFixture();
  const completedWithReport = jobFixture({
    state: "completed",
    artifacts: {
      report_available: true,
      evidence_available: true,
      claim_verifications_available: false,
      feedback_loop_available: false,
    },
  });
  const completedWithoutReport = jobFixture({ state: "completed", artifacts: null });
  const historical = jobFixture({
    origin: "artifact",
    artifacts: {
      report_available: true,
      evidence_available: true,
      claim_verifications_available: false,
      feedback_loop_available: false,
    },
  });

  assert.equal(canCancel(running), true);
  assert.equal(canCancel(completedWithReport), false);
  assert.equal(canCancel(historical), false);

  assert.equal(shouldFetchReport(completedWithReport), true);
  assert.equal(shouldFetchReport(completedWithoutReport), false);
  assert.equal(shouldFetchReport(running), false);
  // Historical entries are never treated as fetchable live jobs.
  assert.equal(shouldFetchReport(historical), false);

  const jobs = [running, completedWithReport, { ...jobFixture({ job_id: "c".repeat(32), state: "queued" }) }];
  assert.deepEqual(activeJobIds(jobs), [JOB_ID, "c".repeat(32)]);
});

test("describeApiError covers 503 and unknown errors without leaking internals", () => {
  const unavailable = new ApiError("http", 503, "job manager is not accepting jobs");
  assert.match(describeApiError(unavailable), /not accepting requests/);

  assert.equal(
    describeApiError(new Error("boom")),
    "Unexpected error while talking to the research backend.",
  );
});

test("artifactLabels and artifactRunHref describe browsable artifacts", () => {
  const full = {
    report_available: true,
    evidence_available: true,
    claim_verifications_available: false,
    feedback_loop_available: true,
  };
  assert.deepEqual(artifactLabels(full), ["report", "evidence", "feedback"]);
  assert.deepEqual(artifactLabels(null), []);
  assert.deepEqual(
    artifactLabels({
      report_available: false,
      evidence_available: false,
      claim_verifications_available: false,
      feedback_loop_available: false,
    }),
    [],
  );

  assert.equal(
    artifactRunHref(jobFixture({ run_id: RUN_ID })),
    `/runs/${RUN_ID}/report`,
  );
  assert.equal(artifactRunHref(jobFixture({ run_id: null })), null);
  assert.equal(artifactRunHref({ ...jobFixture({ run_id: RUN_ID }), origin: "artifact" }), null);
});

test("cancelOutcomeMessage states best-effort semantics without overclaiming", () => {
  const running = jobFixture({ state: "running" });
  const requested = cancelOutcomeMessage(true, running);
  assert.match(requested, /Cancellation requested/);
  assert.match(requested, /best-effort/);

  const cancelled = jobFixture({ state: "cancelled", failure_kind: "cancelled" });
  assert.equal(
    cancelOutcomeMessage(true, cancelled),
    `Job ${JOB_ID.slice(0, 8)}… is now cancelled.`,
  );

  const completed = jobFixture({ state: "completed" });
  const declined = cancelOutcomeMessage(false, completed);
  assert.match(declined, /did not accept/);
  assert.match(declined, /completed/);
});
