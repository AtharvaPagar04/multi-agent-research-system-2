"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { Badge } from "@/components/badge";
import { Panel, PanelBody, PanelHeader } from "@/components/panel";
import { ReportView } from "@/components/report-view";
import { TopicEntry } from "@/components/topic-entry";
import {
  activeJobIds,
  artifactLabels,
  artifactRunHref,
  canCancel,
  cancelOutcomeMessage,
  cancelRun,
  describeApiError,
  fetchRunReport,
  getRun,
  listRuns,
  shouldFetchReport,
  type ApiHistoricalRun,
  type ApiJobSnapshot,
} from "@/lib/api";
import { UNAVAILABLE, formatDateTime } from "@/lib/format";
import { parseReportMarkdown } from "@/lib/report-parser";
import type { ResearchReportArtifact } from "@/lib/types";

const POLL_INTERVAL_MS = 2500;

type BackendStatus = "connecting" | "ok" | "unavailable";

const STATE_CHIP_STYLES: Record<string, string> = {
  queued: "border-zinc-700 bg-zinc-900/40 text-zinc-300",
  starting: "border-sky-900/70 bg-sky-950/20 text-sky-200",
  running: "border-emerald-900/70 bg-emerald-950/20 text-emerald-200",
  completed: "border-emerald-800/70 bg-emerald-950/30 text-emerald-100",
  failed: "border-red-900/70 bg-red-950/20 text-red-200",
  cancelled: "border-amber-900/70 bg-amber-950/20 text-amber-200",
  interrupted: "border-orange-900/70 bg-orange-950/20 text-orange-200",
};

const SMALL_BUTTON_CLASSES =
  "h-8 rounded-md border border-zinc-700 bg-zinc-950 px-3 font-mono text-[11px] uppercase tracking-[0.12em] text-zinc-200 transition-colors hover:border-zinc-500 hover:text-white disabled:cursor-not-allowed disabled:opacity-50";

function formatEpochSeconds(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return UNAVAILABLE;
  return formatDateTime(new Date(value * 1000).toISOString());
}

function StateChip({ job }: { job: ApiJobSnapshot }) {
  const state = job.state ?? "unknown";
  const style =
    STATE_CHIP_STYLES[state] ?? "border-zinc-700 bg-zinc-900/40 text-zinc-300";
  return (
    <span
      className={`rounded border px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.12em] ${style}`}
    >
      {state}
      {job.failure_kind ? ` · ${job.failure_kind}` : ""}
    </span>
  );
}

function LiveJobRow({
  job,
  cancelling,
  onCancel,
  onOpenReport,
}: {
  job: ApiJobSnapshot;
  cancelling: boolean;
  onCancel: (jobId: string) => void;
  onOpenReport: (jobId: string, runId: string | null) => void;
}) {
  const elapsed =
    job.elapsed_seconds != null && job.started_at != null
      ? `${Math.max(0, job.elapsed_seconds).toFixed(1)}s elapsed`
      : null;
  const artifactLabelList = artifactLabels(job.artifacts);
  const artifactHref = artifactRunHref(job);

  return (
    <li className="rounded-md border border-zinc-900 bg-black/20 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span
            title={job.job_id}
            className="font-mono text-[12px] text-zinc-400"
          >
            {job.job_id.slice(0, 8)}
          </span>
          <StateChip job={job} />
        </div>
        <span className="font-mono text-[11px] text-zinc-600">
          {formatEpochSeconds(job.created_at)}
        </span>
      </div>

      <p className="mt-2 text-[13px] leading-5 text-zinc-200">
        {job.topic || "Topic unavailable"}
      </p>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-zinc-500">
        {job.stage ? <span>stage {job.stage}</span> : null}
        {elapsed ? <span>{elapsed}</span> : null}
        {job.run_id ? (
          <span title={job.run_id}>run {job.run_id.slice(0, 8)}…</span>
        ) : null}
        {job.exit_code != null ? <span>exit {job.exit_code}</span> : null}
        {job.artifacts ? (
          <span>
            artifacts:{" "}
            {artifactLabelList.length > 0
              ? artifactLabelList.join(" · ")
              : "none yet"}
          </span>
        ) : null}
      </div>

      {job.error ? (
        <p className="mt-2 text-[12px] text-red-200/80">{job.error}</p>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {canCancel(job) ? (
          <button
            type="button"
            disabled={cancelling}
            onClick={() => onCancel(job.job_id)}
            className={SMALL_BUTTON_CLASSES}
          >
            {cancelling ? "Cancelling…" : "Cancel"}
          </button>
        ) : null}
        {shouldFetchReport(job) ? (
          <button
            type="button"
            onClick={() => onOpenReport(job.job_id, job.run_id)}
            className={SMALL_BUTTON_CLASSES}
          >
            Open report
          </button>
        ) : null}
        {artifactHref && artifactLabelList.length > 0 ? (
          <Link href={artifactHref} className={SMALL_BUTTON_CLASSES}>
            Open workspace
          </Link>
        ) : null}
      </div>
    </li>
  );
}

export function LiveRunsConsole() {
  const [jobs, setJobs] = useState<ApiJobSnapshot[]>([]);
  const [historical, setHistorical] = useState<ApiHistoricalRun[]>([]);
  const [backendStatus, setBackendStatus] = useState<BackendStatus>("connecting");
  const [listError, setListError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cancellingId, setCancellingId] = useState<string | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);

  const [reportTarget, setReportTarget] = useState<{
    jobId: string;
    runId: string | null;
  } | null>(null);
  const [reportArtifact, setReportArtifact] =
    useState<ResearchReportArtifact | null>(null);
  const [reportError, setReportError] = useState<string | null>(null);
  const [reportLoading, setReportLoading] = useState(false);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const data = await listRuns();
        if (disposed) return;
        setJobs(data.jobs);
        setHistorical(data.historical_runs);
        setBackendStatus("ok");
        setListError(null);

        const active = activeJobIds(data.jobs);
        const details = await Promise.all(
          active.map(async (jobId) => {
            try {
              return await getRun(jobId);
            } catch {
              return null;
            }
          }),
        );
        if (disposed) return;
        const byId = new Map(
          details
            .filter((detail): detail is ApiJobSnapshot => detail !== null)
            .map((detail) => [detail.job_id, detail] as const),
        );
        if (byId.size > 0) {
          setJobs((previous) =>
            previous.map((job) => byId.get(job.job_id) ?? job),
          );
        }
      } catch (error) {
        if (!disposed) {
          setBackendStatus("unavailable");
          setListError(describeApiError(error));
        }
      } finally {
        if (!disposed) {
          timer = setTimeout(() => void tick(), POLL_INTERVAL_MS);
        }
      }
    };

    void tick();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
    };
  }, [refreshTick]);

  const openReport = useCallback(async (jobId: string, runId: string | null) => {
    setReportTarget({ jobId, runId });
    setReportArtifact(null);
    setReportError(null);
    setReportLoading(true);
    try {
      const markdown = await fetchRunReport(jobId);
      setReportArtifact(
        parseReportMarkdown(
          markdown,
          runId ? `research_report_${runId}.md` : `backend report ${jobId.slice(0, 8)}`,
        ),
      );
    } catch (error) {
      setReportError(describeApiError(error));
    } finally {
      setReportLoading(false);
    }
  }, []);

  const handleSubmitted = useCallback(() => {
    setNotice(null);
    setRefreshTick((value) => value + 1);
  }, []);

  async function handleCancel(jobId: string) {
    if (cancellingId) return;
    setCancellingId(jobId);
    setNotice(null);
    try {
      const result = await cancelRun(jobId);
      setJobs((previous) =>
        previous.map((job) => (job.job_id === jobId ? result.job : job)),
      );
      setNotice(cancelOutcomeMessage(result.accepted, result.job));
      setRefreshTick((value) => value + 1);
    } catch (error) {
      setNotice(describeApiError(error));
    } finally {
      setCancellingId(null);
    }
  }

  function closeReport() {
    setReportTarget(null);
    setReportArtifact(null);
    setReportError(null);
    setReportLoading(false);
  }

  return (
    <div className="space-y-4">
      <TopicEntry
        onSubmitted={handleSubmitted}
        backendAvailable={backendStatus !== "unavailable"}
      />

      {backendStatus === "unavailable" ? (
        <Panel>
          <PanelHeader
            title="Backend unavailable"
            description="Live run submission, monitoring, and cancellation need the local FastAPI backend."
          />
          <PanelBody>
            <p className="text-[13px] text-amber-200/80">
              {listError ?? "Could not reach the research backend."}
            </p>
            <p className="mt-2 text-[13px] text-zinc-400">
              Historical artifact browsing (sidebar, claims, evidence, and
              on-disk reports) remains available while the backend is offline.
              Retrying automatically…
            </p>
          </PanelBody>
        </Panel>
      ) : null}

      {notice ? (
        <p className="font-mono text-[11px] text-zinc-400">{notice}</p>
      ) : null}

      <Panel>
        <PanelHeader
          title="Live runs"
          description="Manager-owned jobs reported by the backend, polled every 2.5 seconds while active. Stage labels are backend-supplied; no progress percentages are inferred."
          actions={
            <Badge tone={backendStatus === "ok" ? "success" : "neutral"}>
              {backendStatus === "ok"
                ? "Backend connected"
                : backendStatus === "connecting"
                  ? "Connecting…"
                  : "Backend offline"}
            </Badge>
          }
        />
        <PanelBody>
          {jobs.length === 0 ? (
            <p className="text-sm text-zinc-400">
              No runs were submitted from this dashboard yet. Submitted runs
              appear here with live state, stage, and cancellation.
            </p>
          ) : (
            <ul className="space-y-3">
              {jobs.map((job) => (
                <LiveJobRow
                  key={job.job_id}
                  job={job}
                  cancelling={cancellingId === job.job_id}
                  onCancel={(jobId) => void handleCancel(jobId)}
                  onOpenReport={(jobId, runId) => void openReport(jobId, runId)}
                />
              ))}
            </ul>
          )}
        </PanelBody>
      </Panel>

      {historical.length > 0 ? (
        <Panel>
          <PanelHeader
            title="Artifact-only historical runs"
            description="Discovered by the backend from evidence_runs/ file names. These runs are not executing and carry no live state — open one to browse its artifacts in the workspace."
          />
          <PanelBody>
            <ul className="flex flex-wrap gap-2">
              {historical.map((run) => (
                <li key={run.run_id}>
                  <Link
                    href={`/runs/${run.run_id}/report`}
                    title={run.run_id}
                    className="inline-flex items-center gap-2 rounded-full border border-zinc-800 bg-zinc-950/60 px-3 py-1 font-mono text-[11px] text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200"
                  >
                    <span className="uppercase tracking-[0.12em] text-zinc-600">
                      historical
                    </span>
                    <span>{run.run_id.slice(0, 8)}…</span>
                    {run.artifacts.report_available ? (
                      <span className="text-zinc-500">report</span>
                    ) : null}
                    {run.artifacts.evidence_available ? (
                      <span className="text-zinc-500">evidence</span>
                    ) : null}
                  </Link>
                </li>
              ))}
            </ul>
          </PanelBody>
        </Panel>
      ) : null}

      {reportTarget ? (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
              Live report · {reportTarget.jobId.slice(0, 8)}
              {reportTarget.runId ? ` · run ${reportTarget.runId.slice(0, 8)}…` : ""}
            </p>
            <button
              type="button"
              onClick={closeReport}
              className={SMALL_BUTTON_CLASSES}
            >
              Close report
            </button>
          </div>
          {reportLoading ? (
            <Panel>
              <PanelBody>
                <p className="font-mono text-[12px] text-zinc-500">
                  Fetching report from the backend…
                </p>
              </PanelBody>
            </Panel>
          ) : null}
          {reportError ? (
            <Panel>
              <PanelHeader
                title="Report unavailable"
                description="The backend did not return a report for this run."
              />
              <PanelBody>
                <p className="text-[13px] text-zinc-400">{reportError}</p>
                <button
                  type="button"
                  onClick={() =>
                    void openReport(reportTarget.jobId, reportTarget.runId)
                  }
                  className={`mt-3 ${SMALL_BUTTON_CLASSES}`}
                >
                  Retry
                </button>
              </PanelBody>
            </Panel>
          ) : null}
          {reportArtifact ? <ReportView report={reportArtifact} /> : null}
        </div>
      ) : null}
    </div>
  );
}
