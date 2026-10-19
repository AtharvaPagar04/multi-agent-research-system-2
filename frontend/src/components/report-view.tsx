import { Markdown } from "@/components/markdown";
import { Panel, PanelBody, PanelHeader } from "@/components/panel";
import { UNAVAILABLE } from "@/lib/format";
import type { ResearchReportArtifact } from "@/lib/types";

export function ReportView({ report }: { report: ResearchReportArtifact | null }) {
  if (!report) {
    return (
      <Panel>
        <PanelHeader
          title="Research report"
          description="No Markdown research report was found on disk for this run."
        />
        <PanelBody>
          <div className="rounded-md border border-dashed border-zinc-800 bg-black/20 px-4 py-8 text-center">
            <p className="font-mono text-[12px] uppercase tracking-[0.16em] text-zinc-500">
              Report unavailable
            </p>
            <p className="mx-auto mt-3 max-w-xl text-[13px] leading-6 text-zinc-400">
              Claim verification and evidence records for this run remain available.
              Place a report file matching the run ID in the repository root or{" "}
              <code className="font-mono text-zinc-300">evidence_runs/</code> to render
              the full narrative with citations.
            </p>
          </div>
        </PanelBody>
      </Panel>
    );
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Research report"
          description={
            report.title
              ? "Structured research output with inline citations, metric tables, and verification annotations."
              : "Report artifact loaded without a recognized title heading."
          }
          actions={
            <span className="font-mono text-[11px] text-zinc-500">
              {report.path || UNAVAILABLE}
            </span>
          }
        />
        <PanelBody className="max-h-[720px] overflow-y-auto">
          <article className="mx-auto max-w-3xl">
            {report.title ? (
              <h1 className="mb-6 border-b border-zinc-900 pb-4 text-2xl font-medium tracking-tight text-zinc-50">
                {report.title}
              </h1>
            ) : null}
            <Markdown source={report.markdown} />
          </article>
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHeader
          title="Bibliography"
          description="Citation markers resolved from the report sources section."
          actions={
            <span className="font-mono text-[11px] text-zinc-500">
              {report.sources.length > 0
                ? `${report.sources.length} sources`
                : "No sources parsed"}
            </span>
          }
        />
        <PanelBody>
          {report.sources.length === 0 ? (
            <p className="text-sm text-zinc-500">
              Bibliography unavailable — no numbered source blocks were recognized.
            </p>
          ) : (
            <ol className="space-y-3">
              {report.sources.map((source) => (
                <li
                  key={source.marker}
                  className="grid gap-1 rounded-md border border-zinc-900 bg-black/20 px-3 py-2 sm:grid-cols-[2.5rem_minmax(0,1fr)]"
                >
                  <span className="font-mono text-[12px] text-zinc-500">
                    [{source.marker}]
                  </span>
                  <div className="min-w-0">
                    <div className="text-[13px] text-zinc-200">
                      {source.title || UNAVAILABLE}
                    </div>
                    {source.url ? (
                      <a
                        href={source.url}
                        target="_blank"
                        rel="noreferrer"
                        className="break-all font-mono text-[11px] text-zinc-500 underline decoration-zinc-800 underline-offset-2 hover:text-zinc-300"
                      >
                        {source.url}
                      </a>
                    ) : (
                      <div className="font-mono text-[11px] text-amber-200/70">
                        URL unavailable
                      </div>
                    )}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </PanelBody>
      </Panel>
    </div>
  );
}
