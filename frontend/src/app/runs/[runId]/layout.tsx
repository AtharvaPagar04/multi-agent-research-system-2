import Link from "next/link";
import { notFound } from "next/navigation";
import type { ReactNode } from "react";

import { Badge } from "@/components/badge";
import { RunSidebar } from "@/components/run-sidebar";
import { RunTabs, type RunTabKey } from "@/components/run-tabs";
import { formatDateTime } from "@/lib/format";
import { getResearchRun, listResearchRuns } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function RunLayout({
  children,
  params,
}: {
  children: ReactNode;
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  const runs = listResearchRuns();
  const availability: Record<RunTabKey, boolean> = {
    overview: true,
    report: run.hasReport,
    claims: run.hasClaims,
    evidence: run.hasEvidence,
    audit: run.hasClaims || run.hasReport,
    feedback: run.hasFeedback,
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[auto_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-20 lg:h-[calc(100vh-6rem)]">
        <RunSidebar runs={runs} activeRunId={run.runId} />
      </div>
      <div className="space-y-4">
        <div className="rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 px-5 py-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
                Research run
              </div>
              <h1 className="mt-1 text-xl font-medium leading-7 tracking-tight text-zinc-50">
                {run.topic || "Topic unavailable — no linked report or claim text"}
              </h1>
              <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-zinc-500">
                <span className="break-all">{run.runId}</span>
                <span>created {formatDateTime(run.createdAt)}</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Badge>Artifact-backed</Badge>
              {run.hasReport ? <Badge>Report</Badge> : null}
              {run.hasClaims ? <Badge>Claims</Badge> : null}
              {run.hasEvidence ? <Badge>Evidence</Badge> : null}
              <Link
                href="/"
                className="rounded-md border border-zinc-800 px-3 py-1.5 font-mono text-[11px] tracking-wide text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200"
              >
                Live runs console →
              </Link>
            </div>
          </div>
          <p className="mt-3 font-mono text-[11px] leading-5 text-zinc-600">
            Artifact-only view — this page reads local evidence_runs files and
            never executes research. Live job status and cancellation stay in
            the runs console.
          </p>
        </div>
        <RunTabs runId={run.runId} availability={availability} />
        {children}
      </div>
    </div>
  );
}
