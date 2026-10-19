"use client";

import { useMemo, useState } from "react";

import { Badge } from "@/components/badge";
import { Panel, PanelBody, PanelHeader } from "@/components/panel";
import { retrievalLabel, truncate, UNAVAILABLE } from "@/lib/format";
import type { EvidenceRecord } from "@/lib/types";

type StatusFilter = "all" | "full_text" | "snippet_only" | "failed";

export function EvidenceExplorer({ records }: { records: EvidenceRecord[] }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<StatusFilter>("all");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return records.filter((record) => {
      if (status !== "all" && record.retrieval_status !== status) return false;
      if (!q) return true;
      const haystack = [
        record.title,
        record.source_url,
        record.final_url,
        record.provider,
        record.snippet_text,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [records, query, status]);

  const selected =
    filtered.find((record) => record.evidence_id === selectedId) ?? filtered[0] ?? null;

  if (records.length === 0) {
    return (
      <Panel>
        <PanelHeader
          title="Evidence explorer"
          description="No evidence records are available for this run."
        />
        <PanelBody>
          <p className="text-sm text-zinc-500">Evidence store empty or missing for this run.</p>
        </PanelBody>
      </Panel>
    );
  }

  return (
    <Panel>
      <PanelHeader
        title="Evidence explorer"
        description="Inspect captured source text, retrieval status, and provider provenance."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter sources…"
              className="h-9 w-56 rounded-md border border-zinc-800 bg-[#111111] px-3 text-[13px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            />
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value as StatusFilter)}
              className="h-9 rounded-md border border-zinc-800 bg-[#111111] px-3 text-[13px] text-zinc-200 outline-none focus:border-zinc-600"
            >
              <option value="all">All statuses</option>
              <option value="full_text">Full text</option>
              <option value="snippet_only">Snippet</option>
              <option value="failed">Failed</option>
            </select>
          </div>
        }
      />
      <div className="grid gap-0 lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
        <div className="max-h-[420px] overflow-y-auto border-b border-zinc-800/80 lg:border-b-0 lg:border-r">
          <ul className="divide-y divide-zinc-900/80">
            {filtered.map((record) => {
              const active = selected?.evidence_id === record.evidence_id;
              return (
                <li key={record.evidence_id}>
                  <button
                    type="button"
                    onClick={() => setSelectedId(record.evidence_id)}
                    className={`w-full px-5 py-3 text-left transition-colors ${
                      active ? "bg-zinc-900/60" : "hover:bg-zinc-900/30"
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <div className="truncate text-[13px] text-zinc-200">
                          {record.title || record.source_url || UNAVAILABLE}
                        </div>
                        <div className="mt-1 truncate font-mono text-[11px] text-zinc-500">
                          {record.source_url || UNAVAILABLE}
                        </div>
                      </div>
                      <Badge>{retrievalLabel(record.retrieval_status)}</Badge>
                    </div>
                  </button>
                </li>
              );
            })}
            {filtered.length === 0 ? (
              <li className="px-5 py-8 text-center text-sm text-zinc-500">
                No evidence records match the current filter.
              </li>
            ) : null}
          </ul>
        </div>
        <div className="max-h-[420px] overflow-y-auto bg-[#0d0d0d]/60 px-5 py-4">
          {selected ? (
            <div className="space-y-4">
              <div>
                <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                  Selected source
                </div>
                <div className="mt-2 text-sm text-zinc-200">
                  {selected.title || UNAVAILABLE}
                </div>
                <a
                  href={selected.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="mt-1 break-all font-mono text-[12px] text-zinc-400 underline decoration-zinc-700 underline-offset-2 hover:text-zinc-200"
                >
                  {selected.source_url}
                </a>
              </div>
              <dl className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                    Retrieval
                  </dt>
                  <dd className="mt-1 text-sm text-zinc-200">
                    {retrievalLabel(selected.retrieval_status)}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                    Provider
                  </dt>
                  <dd className="mt-1 text-sm text-zinc-200">
                    {selected.provider || UNAVAILABLE}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                    Evidence ID
                  </dt>
                  <dd className="mt-1 break-all font-mono text-[12px] text-zinc-300">
                    {selected.evidence_id}
                  </dd>
                </div>
                <div>
                  <dt className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                    Final URL
                  </dt>
                  <dd className="mt-1 break-all font-mono text-[12px] text-zinc-300">
                    {selected.final_url || UNAVAILABLE}
                  </dd>
                </div>
              </dl>
              <div>
                <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                  Captured text
                </div>
                <pre className="mt-2 max-h-56 overflow-y-auto whitespace-pre-wrap rounded-md border border-zinc-800 bg-black/40 p-3 font-mono text-[12px] leading-6 text-zinc-300">
                  {selected.captured_text?.trim() ||
                    selected.snippet_text?.trim() ||
                    "Unavailable — no captured text for this retrieval status."}
                </pre>
              </div>
              {selected.snippet_text?.trim() && selected.captured_text?.trim() ? (
                <div>
                  <div className="font-mono text-[11px] uppercase tracking-[0.14em] text-zinc-500">
                    Snippet observation
                  </div>
                  <p className="mt-2 text-[13px] leading-6 text-zinc-400">
                    {truncate(selected.snippet_text, 600)}
                  </p>
                </div>
              ) : null}
            </div>
          ) : (
            <p className="text-sm text-zinc-500">Select a source to inspect evidence.</p>
          )}
        </div>
      </div>
    </Panel>
  );
}
