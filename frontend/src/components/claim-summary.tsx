"use client";

import { useMemo, useState } from "react";

import { DistributionBar } from "@/components/distribution-bar";
import { Panel, PanelBody, PanelHeader, StatCard } from "@/components/panel";
import { VerdictBadge } from "@/components/verdict-badge";
import { formatNumber, formatPercent, truncate } from "@/lib/format";
import type { ClaimRecord } from "@/lib/types";

type VerdictFilter = "all" | "supported" | "contradicted" | "insufficient_evidence";

export function ClaimSummary({
  claims,
  supported,
  contradicted,
  insufficient,
  total,
  warnings,
}: {
  claims: ClaimRecord[];
  supported: number | null;
  contradicted: number | null;
  insufficient: number | null;
  total: number | null;
  warnings: string[];
}) {
  const [filter, setFilter] = useState<VerdictFilter>("all");
  const [query, setQuery] = useState("");
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return claims.filter((claim) => {
      if (filter !== "all" && claim.verdict !== filter) return false;
      if (!q) return true;
      return [claim.claim_text, claim.rationale, claim.source_url]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(q);
    });
  }, [claims, filter, query]);

  if (claims.length === 0) {
    return (
      <Panel>
        <PanelHeader
          title="Claim verification"
          description="No claim verification records are available for this run."
        />
        <PanelBody>
          <p className="text-sm text-zinc-500">
            Verification artifact missing or empty. Values remain unavailable rather than zero.
          </p>
        </PanelBody>
      </Panel>
    );
  }

  return (
    <div className="space-y-4">
      <Panel>
        <PanelHeader
          title="Claim verification summary"
          description="Atomic claim verdicts from the evidence verification pipeline. Missing values are shown as unavailable."
          actions={
            <div className="flex flex-wrap gap-2">
              {(
                [
                  ["all", "All"],
                  ["supported", "Supported"],
                  ["contradicted", "Contradicted"],
                  ["insufficient_evidence", "Insufficient"],
                ] as const
              ).map(([value, label]) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setFilter(value)}
                  className={`rounded-md border px-3 py-1.5 font-mono text-[11px] tracking-wide transition-colors ${
                    filter === value
                      ? "border-zinc-500 bg-zinc-900 text-zinc-100"
                      : "border-zinc-800 bg-transparent text-zinc-400 hover:border-zinc-700 hover:text-zinc-200"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          }
        />
        <PanelBody className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatCard label="Total claims" value={formatNumber(total)} />
            <StatCard
              label="Supported"
              value={formatNumber(supported)}
              hint={
                total != null && supported != null && total > 0
                  ? formatPercent(supported / total)
                  : "Rate unavailable"
              }
            />
            <StatCard label="Contradicted" value={formatNumber(contradicted)} />
            <StatCard label="Insufficient" value={formatNumber(insufficient)} />
          </div>
          <div className="space-y-2">
            <DistributionBar
              total={total ?? 0}
              segments={[
                {
                  label: "Supported",
                  value: supported ?? 0,
                  className: "bg-emerald-500/80",
                },
                {
                  label: "Contradicted",
                  value: contradicted ?? 0,
                  className: "bg-rose-500/80",
                },
                {
                  label: "Insufficient",
                  value: insufficient ?? 0,
                  className: "bg-amber-400/70",
                },
              ]}
            />
            <p className="font-mono text-[11px] text-zinc-500">
              Supported / contradicted / insufficient evidence — exact counts preserved from the run artifact.
            </p>
          </div>
          {warnings.length > 0 ? (
            <div className="rounded-md border border-amber-900/40 bg-amber-950/10 px-4 py-3">
              <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-amber-200/80">
                Verification warnings ({warnings.length})
              </div>
              <ul className="mt-2 space-y-1.5 text-[12px] leading-5 text-amber-100/70">
                {warnings.slice(0, 8).map((warning, index) => (
                  <li key={index}>{warning}</li>
                ))}
                {warnings.length > 8 ? (
                  <li className="text-amber-200/50">
                    +{warnings.length - 8} additional warnings retained in artifact.
                  </li>
                ) : null}
              </ul>
            </div>
          ) : null}
        </PanelBody>
      </Panel>

      <Panel>
        <PanelHeader
          title="Claim records"
          description="Clause-level verdicts with cited source, selected passage, and verifier rationale."
          actions={
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search claims…"
              className="h-9 w-56 rounded-md border border-zinc-800 bg-[#111111] px-3 text-[13px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            />
          }
        />
        <ul className="divide-y divide-zinc-900/80">
          {filtered.map((claim) => {
            const open = expandedId === claim.claim_id;
            return (
              <li key={claim.claim_id || claim.claim_text}>
                <button
                  type="button"
                  onClick={() => setExpandedId(open ? null : claim.claim_id)}
                  className="w-full px-5 py-4 text-left hover:bg-zinc-900/25"
                >
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 space-y-2">
                      <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                        {claim.claim_id || "claim"}
                      </div>
                      <p className="text-[14px] leading-6 text-zinc-200">
                        {claim.claim_text || "Claim text unavailable"}
                      </p>
                      <div className="flex flex-wrap items-center gap-2 font-mono text-[11px] text-zinc-500">
                        <span>{claim.source_url || "No source URL"}</span>
                        {claim.action_taken ? <span>· {claim.action_taken}</span> : null}
                      </div>
                    </div>
                    <VerdictBadge verdict={claim.verdict} />
                  </div>
                </button>
                {open ? (
                  <div className="space-y-4 border-t border-zinc-900/80 bg-black/20 px-5 py-4">
                    <div>
                      <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                        Verifier rationale
                      </div>
                      <p className="mt-2 text-[13px] leading-6 text-zinc-300">
                        {claim.rationale || "Rationale unavailable"}
                      </p>
                    </div>
                    <div>
                      <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                        Selected passage
                      </div>
                      <pre className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap rounded-md border border-zinc-800 bg-black/40 p-3 font-mono text-[12px] leading-6 text-zinc-400">
                        {claim.passage_text?.trim() ||
                          "Passage unavailable for this claim."}
                      </pre>
                    </div>
                    <div className="font-mono text-[11px] text-zinc-500">
                      Passage ID: {claim.passage_id || "Unavailable"} · Retrieval:{" "}
                      {claim.retrieval_status || "Unavailable"}
                    </div>
                  </div>
                ) : null}
              </li>
            );
          })}
          {filtered.length === 0 ? (
            <li className="px-5 py-8 text-center text-sm text-zinc-500">
              No claims match the current filter.
            </li>
          ) : null}
        </ul>
      </Panel>
    </div>
  );
}

const VERDICT_PRIORITY: Record<string, number> = {
  contradicted: 0,
  insufficient_evidence: 1,
};

export function ClaimMiniList({ claims }: { claims: ClaimRecord[] }) {
  const unsupported = claims
    .filter((claim) => claim.verdict !== "supported")
    .sort(
      (a, b) =>
        (VERDICT_PRIORITY[a.verdict ?? ""] ?? 2) -
        (VERDICT_PRIORITY[b.verdict ?? ""] ?? 2),
    )
    .slice(0, 5);
  if (unsupported.length === 0) {
    return (
      <p className="text-sm text-zinc-500">
        No contradicted or insufficient claims in the verification artifact.
      </p>
    );
  }
  return (
    <ul className="space-y-3">
      {unsupported.map((claim) => (
        <li key={claim.claim_id} className="rounded-md border border-zinc-900 bg-black/20 px-3 py-2">
          <div className="flex items-start justify-between gap-3">
            <p className="text-[13px] leading-5 text-zinc-300">
              {truncate(claim.claim_text, 220)}
            </p>
            <VerdictBadge verdict={claim.verdict} />
          </div>
        </li>
      ))}
    </ul>
  );
}
