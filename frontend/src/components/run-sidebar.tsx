"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { formatDate, formatNumber, formatPercent } from "@/lib/format";
import type { RunSummary } from "@/lib/types";

export function RunSidebar({
  runs,
  activeRunId,
}: {
  runs: RunSummary[];
  activeRunId: string | null;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  useEffect(() => {
    if (!drawerOpen) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [drawerOpen]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return runs;
    return runs.filter((run) =>
      [run.topic, run.runId, run.shortId, run.terminatedReason]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(q),
    );
  }, [runs, query]);

  const visibilityClasses = drawerOpen
    ? "flex fixed inset-y-0 left-0 z-40 w-[280px] max-w-[85vw] rounded-l-none lg:static lg:inset-auto lg:z-auto lg:rounded-lg"
    : "hidden lg:flex";

  return (
    <>
      <button
        type="button"
        onClick={() => setDrawerOpen(true)}
        aria-expanded={drawerOpen}
        aria-controls="run-sidebar"
        aria-label="Open research runs sidebar"
        className="mb-2 flex w-full items-center justify-between rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 px-4 py-3 lg:hidden"
      >
        <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
          Research runs
        </span>
        <span className="font-mono text-[11px] text-zinc-400">
          {formatNumber(runs.length)} · open
        </span>
      </button>

      {drawerOpen ? (
        <button
          type="button"
          aria-label="Dismiss research runs sidebar"
          onClick={() => setDrawerOpen(false)}
          className="fixed inset-0 z-30 bg-black/60 lg:hidden"
        />
      ) : null}

      <aside
        id="run-sidebar"
        className={`h-full min-h-0 flex-col rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 ${visibilityClasses} ${
          collapsed ? "lg:w-14" : "lg:w-[280px]"
        }`}
      >
        <div className="flex items-center justify-between gap-2 border-b border-zinc-800/80 px-3 py-3">
          {!collapsed ? (
            <div className="min-w-0">
              <div className="font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
                Research runs
              </div>
              <p className="mt-1 text-[12px] text-zinc-500">
                {formatNumber(runs.length)} artifact-linked run{runs.length === 1 ? "" : "s"}
              </p>
            </div>
          ) : null}
          <button
            type="button"
            onClick={() => setCollapsed((value) => !value)}
            aria-expanded={!collapsed}
            aria-controls="run-sidebar-body"
            aria-label={
              collapsed
                ? "Expand research runs sidebar"
                : "Collapse research runs sidebar"
            }
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className="ml-auto hidden h-8 w-8 shrink-0 items-center justify-center rounded-md border border-zinc-800 font-mono text-[13px] text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-100 lg:flex"
          >
            <span aria-hidden="true">{collapsed ? "»" : "«"}</span>
          </button>
          <button
            type="button"
            onClick={() => setDrawerOpen(false)}
            aria-label="Close research runs sidebar"
            className="ml-auto flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-zinc-800 font-mono text-[13px] text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-100 lg:hidden"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <div
          id="run-sidebar-body"
          className={`min-h-0 flex-1 flex-col ${collapsed ? "flex lg:hidden" : "flex"}`}
        >
          <div className="border-b border-zinc-800/80 px-4 py-4">
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Filter runs…"
              className="h-9 w-full rounded-md border border-zinc-800 bg-[#111111] px-3 text-[13px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-zinc-600"
            />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            {filtered.length === 0 ? (
              <p className="px-4 py-8 text-center text-[13px] text-zinc-500">
                No runs match the filter.
              </p>
            ) : (
              <ul className="divide-y divide-zinc-900/70">
                {filtered.map((run) => {
                  const active = run.runId === activeRunId;
                  return (
                    <li key={run.runId}>
                      <Link
                        href={`/runs/${run.runId}/report`}
                        className={`block px-4 py-3.5 transition-colors ${
                          active
                            ? "bg-zinc-900/70 shadow-[inset_2px_0_0_rgba(228,228,231,0.35)]"
                            : "hover:bg-zinc-900/35"
                        }`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-mono text-[11px] tracking-wide text-zinc-500">
                            {run.shortId}
                          </span>
                          <span className="font-mono text-[11px] text-zinc-600">
                            {formatDate(run.createdAt)}
                          </span>
                        </div>
                        <p className="mt-1.5 line-clamp-2 text-[13px] leading-5 text-zinc-200">
                          {run.topic || "Topic unavailable"}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-zinc-500">
                          <span>
                            support{" "}
                            {run.supportRate != null
                              ? formatPercent(run.supportRate, 0)
                              : "n/a"}
                          </span>
                          <span>
                            src{" "}
                            {run.hasEvidence ? formatNumber(run.evidenceCount) : "n/a"}
                          </span>
                          {run.hasReport ? <span className="text-zinc-400">report</span> : null}
                          {!run.hasClaims ? <span className="text-amber-200/60">no claims</span> : null}
                        </div>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        </div>

        {collapsed ? (
          <div className="hidden flex-1 flex-col items-center gap-3 px-2 py-4 lg:flex">
            <span
              className="font-mono text-[11px] text-zinc-500"
              title={`${runs.length} artifact-linked runs`}
            >
              {runs.length}
            </span>
          </div>
        ) : null}
      </aside>
    </>
  );
}
