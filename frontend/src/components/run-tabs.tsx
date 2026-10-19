"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

export type RunTabKey =
  | "overview"
  | "report"
  | "claims"
  | "evidence"
  | "audit"
  | "feedback";

const TABS: { key: RunTabKey; segment: string; label: string }[] = [
  { key: "overview", segment: "overview", label: "Overview" },
  { key: "report", segment: "report", label: "Report" },
  { key: "claims", segment: "claims", label: "Claims" },
  { key: "evidence", segment: "evidence", label: "Evidence" },
  { key: "audit", segment: "audit", label: "Quality & audit" },
  { key: "feedback", segment: "feedback", label: "Feedback loop" },
];

export function RunTabs({
  runId,
  availability,
}: {
  runId: string;
  availability: Record<RunTabKey, boolean>;
}) {
  const pathname = usePathname();

  return (
    <nav
      aria-label="Research run views"
      className="flex flex-wrap gap-1.5 rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 p-1.5"
    >
      {TABS.map((tab) => {
        const href = `/runs/${runId}/${tab.segment}`;
        const active = pathname === href;
        const available = availability[tab.key];
        return (
          <Link
            key={tab.key}
            href={href}
            aria-current={active ? "page" : undefined}
            title={available ? undefined : "No artifact for this view in this run"}
            className={`rounded-md px-3 py-1.5 font-mono text-[11px] uppercase tracking-wide transition-colors ${
              active
                ? "bg-zinc-900 text-zinc-100 shadow-[inset_0_0_0_1px_rgba(82,82,91,0.55)]"
                : available
                  ? "text-zinc-400 hover:bg-zinc-900/50 hover:text-zinc-200"
                  : "text-zinc-600 hover:bg-zinc-900/50 hover:text-zinc-400"
            }`}
          >
            {tab.label}
          </Link>
        );
      })}
    </nav>
  );
}
