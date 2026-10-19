import { Panel, PanelBody, PanelHeader, StatCard } from "@/components/panel";
import { formatPercent, UNAVAILABLE } from "@/lib/format";
import type { ResearchRunDetail } from "@/lib/types";

export function QualityFindings({ run }: { run: ResearchRunDetail }) {
  const report = run.report;

  const metricTraceable = report?.metricAudit.traceable ?? null;
  const metricTotal = report?.metricAudit.total ?? null;
  const claimSupported = report?.claimAudit.supported ?? run.supportedClaims;
  const claimTotal = report?.claimAudit.total ?? run.totalClaims;

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Quality & metric audit"
          description="Audit signals preserved from verification artifacts and report footers. Missing values stay unavailable."
        />
        <PanelBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatCard
              label="Claim support rate"
              value={
                claimTotal != null && claimSupported != null && claimTotal > 0
                  ? formatPercent(claimSupported / claimTotal)
                  : UNAVAILABLE
              }
              hint={
                claimSupported != null && claimTotal != null
                  ? `${claimSupported}/${claimTotal}`
                  : "Verification totals unavailable"
              }
            />
            <StatCard
              label="Metrics traced"
              value={
                metricTotal != null && metricTraceable != null
                  ? `${metricTraceable}/${metricTotal}`
                  : UNAVAILABLE
              }
              hint={report?.metricAudit.detail || "No metrics audit line in report footer"}
            />
            <StatCard
              label="Full-text sources"
              value={
                run.hasEvidence
                  ? `${run.fullTextCount}/${run.evidenceCount}`
                  : UNAVAILABLE
              }
              hint={`${run.snippetOnlyCount} snippet-only · ${run.failedCount} failed`}
            />
          </div>

          {report?.auditLines?.length ? (
            <div className="rounded-md border border-zinc-900 bg-black/20">
              <div className="border-b border-zinc-900 px-4 py-2 font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                Report audit footer
              </div>
              <ul className="divide-y divide-zinc-900/80">
                {report.auditLines.map((line, index) => (
                  <li key={index} className="px-4 py-2.5">
                    <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-zinc-500">
                      {line.label}
                    </span>
                    <p className="mt-1 text-[13px] leading-6 text-zinc-300">{line.detail}</p>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="text-sm text-zinc-500">
              No structured report audit footer was found for this run.
            </p>
          )}
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHeader
          title="Verification warnings"
          description="Uncertainty, unresolved claims, and retrieval degradation retained from the claim verification artifact."
          actions={
            <span className="font-mono text-[11px] text-zinc-500">
              {run.claimWarnings.length} warning{run.claimWarnings.length === 1 ? "" : "s"}
            </span>
          }
        />
        <PanelBody>
          {run.claimWarnings.length === 0 ? (
            <p className="text-sm text-zinc-500">
              No warnings recorded in the claim verification artifact for this run.
            </p>
          ) : (
            <ul className="space-y-2">
              {run.claimWarnings.map((warning, index) => (
                <li
                  key={index}
                  className="rounded-md border border-amber-900/30 bg-amber-950/10 px-3 py-2 text-[13px] leading-6 text-amber-100/80"
                >
                  {warning}
                </li>
              ))}
            </ul>
          )}
        </PanelBody>
      </Panel>
    </div>
  );
}
