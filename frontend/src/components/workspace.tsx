import Link from "next/link";

import { LiveRunsConsole } from "@/components/live-runs-console";
import { Panel, PanelBody, PanelHeader } from "@/components/panel";
import { RunSidebar } from "@/components/run-sidebar";
import { formatDate, formatNumber, formatPercent } from "@/lib/format";
import type { RunSummary } from "@/lib/types";

export function Workspace({ runs }: { runs: RunSummary[] }) {
  const latest = runs[0] ?? null;

  return (
    <div className="grid gap-4 lg:grid-cols-[auto_minmax(0,1fr)]">
      <div className="lg:sticky lg:top-20 lg:h-[calc(100vh-6rem)]">
        <RunSidebar runs={runs} activeRunId={null} />
      </div>
      <div className="space-y-4">
        <LiveRunsConsole />
        <Panel>
          <PanelHeader
            title="Select a research run"
            description="Live manager-owned jobs are monitored in the console above. Artifact-only historical runs open in a modular workspace with report, claims, evidence, quality audit, and feedback views."
          />
          <PanelBody className="space-y-4">
            {!latest ? (
              <p className="text-sm text-zinc-400">
                No research artifacts were found. Expected JSON files under{" "}
                <code className="font-mono text-zinc-300">evidence_runs/</code>.
              </p>
            ) : (
              <>
                <div className="font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
                  Latest artifact run
                </div>
                <Link
                  href={`/runs/${latest.runId}/report`}
                  className="block rounded-md border border-zinc-800 bg-black/20 px-4 py-3 transition-colors hover:border-zinc-600"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-mono text-[11px] tracking-wide text-zinc-500">
                      {latest.shortId}
                    </span>
                    <span className="font-mono text-[11px] text-zinc-600">
                      {formatDate(latest.createdAt)}
                    </span>
                  </div>
                  <p className="mt-1.5 text-[14px] leading-6 text-zinc-200">
                    {latest.topic || "Topic unavailable"}
                  </p>
                  <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-zinc-500">
                    <span>
                      support{" "}
                      {latest.supportRate != null
                        ? formatPercent(latest.supportRate, 0)
                        : "n/a"}
                    </span>
                    <span>
                      src{" "}
                      {latest.hasEvidence ? formatNumber(latest.evidenceCount) : "n/a"}
                    </span>
                    {latest.hasReport ? (
                      <span className="text-zinc-400">report</span>
                    ) : null}
                    {!latest.hasClaims ? (
                      <span className="text-amber-200/60">no claims</span>
                    ) : null}
                    <span className="text-zinc-400">Open workspace →</span>
                  </div>
                </Link>
                <p className="text-[12px] leading-5 text-zinc-500">
                  Browse all {formatNumber(runs.length)} artifact-linked run
                  {runs.length === 1 ? "" : "s"} in the sidebar — on small
                  screens open the research runs drawer.
                </p>
              </>
            )}
          </PanelBody>
        </Panel>
      </div>
    </div>
  );
}
