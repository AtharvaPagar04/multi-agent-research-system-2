import { notFound } from "next/navigation";

import { ClaimMiniList } from "@/components/claim-summary";
import { Badge } from "@/components/badge";
import { Panel, PanelBody, PanelHeader, StatCard } from "@/components/panel";
import {
  formatDateTime,
  formatNumber,
  formatPercent,
  UNAVAILABLE,
} from "@/lib/format";
import { getResearchRun } from "@/lib/runs";
import type { ResearchRunDetail } from "@/lib/types";

export const dynamic = "force-dynamic";

function OverviewSection({ run }: { run: ResearchRunDetail }) {
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
      <Panel>
        <PanelHeader
          title="Run overview"
          description="Artifact-backed research run context. Missing fields are unavailable, never zero-filled."
          actions={
            <div className="flex flex-wrap gap-2">
              <Badge>{run.hasEvidence ? "Evidence store" : "No evidence"}</Badge>
              <Badge>{run.hasClaims ? "Claims" : "No claims"}</Badge>
              <Badge>{run.hasReport ? "Report" : "No report"}</Badge>
            </div>
          }
        />
        <PanelBody className="space-y-5">
          <div>
            <div className="font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
              Topic
            </div>
            <h2 className="mt-2 text-2xl font-medium leading-8 tracking-tight text-zinc-50">
              {run.topic || "Topic unavailable — no linked report or claim text"}
            </h2>
          </div>
          <dl className="grid gap-4 sm:grid-cols-2">
            <div>
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                Run ID
              </dt>
              <dd className="mt-1 break-all font-mono text-[13px] text-zinc-300">
                {run.runId}
              </dd>
            </div>
            <div>
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                Created
              </dt>
              <dd className="mt-1 font-mono text-[13px] text-zinc-300">
                {formatDateTime(run.createdAt)}
              </dd>
            </div>
            <div>
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                Feedback termination
              </dt>
              <dd className="mt-1 font-mono text-[13px] text-zinc-300">
                {run.terminatedReason || UNAVAILABLE}
              </dd>
            </div>
            <div>
              <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                Support rate
              </dt>
              <dd className="mt-1 font-mono text-[13px] text-zinc-300">
                {run.supportRate != null
                  ? formatPercent(run.supportRate)
                  : UNAVAILABLE}
              </dd>
            </div>
          </dl>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard
              label="Evidence records"
              value={run.hasEvidence ? formatNumber(run.evidenceCount) : UNAVAILABLE}
            />
            <StatCard
              label="Claims total"
              value={run.hasClaims ? formatNumber(run.totalClaims) : UNAVAILABLE}
            />
            <StatCard
              label="Full text"
              value={run.hasEvidence ? formatNumber(run.fullTextCount) : UNAVAILABLE}
            />
            <StatCard
              label="Warnings"
              value={
                run.hasClaims ? formatNumber(run.claimWarnings.length) : UNAVAILABLE
              }
            />
          </div>
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHeader
          title="Needs attention"
          description="Contradicted and insufficient claims prioritized for audit."
        />
        <PanelBody>
          <ClaimMiniList claims={run.claims} />
        </PanelBody>
      </Panel>
    </div>
  );
}

export default async function OverviewPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  return <OverviewSection run={run} />;
}
