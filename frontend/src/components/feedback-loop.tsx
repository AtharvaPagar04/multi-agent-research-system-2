import { Panel, PanelBody, PanelHeader, StatCard } from "@/components/panel";
import { formatNumber, UNAVAILABLE } from "@/lib/format";
import type { FeedbackArtifact } from "@/lib/types";

export function FeedbackLoopView({ feedback }: { feedback: FeedbackArtifact | null }) {
  if (!feedback || feedback.iterations.length === 0) {
    return (
      <Panel>
        <PanelHeader
          title="Feedback loop"
          description="Targeted re-retrieval rounds executed by the verification feedback loop."
        />
        <PanelBody>
          <p className="text-sm text-zinc-500">
            Feedback loop artifact unavailable for this run.
          </p>
        </PanelBody>
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader
        title="Feedback loop"
        description="Targeted re-retrieval rounds executed by the verification feedback loop."
      />
      <PanelBody>
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <StatCard
              label="Executed rounds"
              value={`${formatNumber(feedback.total_rounds_executed)} / ${formatNumber(feedback.max_rounds)}`}
            />
            <StatCard
              label="Terminated reason"
              value={feedback.terminated_reason || UNAVAILABLE}
            />
            <StatCard
              label="Final action policy"
              value={feedback.final_action_policy || UNAVAILABLE}
            />
          </div>
          <ol className="space-y-3">
            {feedback.iterations.map((iteration, iterationIndex) => {
              const breakdown = iteration.verdict_breakdown ?? {};
              const roundLabel = Number.isFinite(iteration.iteration_index)
                ? iteration.iteration_index + 1
                : iterationIndex + 1;
              return (
                <li
                  key={iterationIndex}
                  className="rounded-md border border-zinc-900 bg-black/20 px-4 py-3"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="font-mono text-[12px] text-zinc-300">
                      Round {roundLabel}
                    </div>
                    <div className="font-mono text-[11px] text-zinc-500">
                      {formatNumber(iteration.claims_checked_count)} claims checked ·{" "}
                      {iteration.new_evidence_ids.length} new evidence IDs
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {Object.entries(breakdown).map(([key, value]) => (
                      <span
                        key={key}
                        className="rounded-full border border-zinc-800 px-2.5 py-0.5 font-mono text-[11px] text-zinc-400"
                      >
                        {key}: {value}
                      </span>
                    ))}
                    {Object.keys(breakdown).length === 0 ? (
                      <span className="font-mono text-[11px] text-zinc-600">
                        Verdict breakdown unavailable
                      </span>
                    ) : null}
                  </div>
                  {iteration.queries_issued.length > 0 ? (
                    <div className="mt-3">
                      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-zinc-500">
                        Queries issued
                      </div>
                      <ul className="mt-1 space-y-1 text-[12px] text-zinc-400">
                        {iteration.queries_issued.map((query, index) => (
                          <li key={index}>{query}</li>
                        ))}
                      </ul>
                    </div>
                  ) : (
                    <div className="mt-3 font-mono text-[11px] text-zinc-600">
                      No additional queries issued in this round
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      </PanelBody>
    </Panel>
  );
}
