import type { ReactNode } from "react";

export function Badge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: "neutral" | "success" | "danger" | "warning";
}) {
  const tones: Record<string, string> = {
    neutral: "border-zinc-700/80 bg-zinc-900/80 text-zinc-300",
    success: "border-emerald-800/70 bg-emerald-950/50 text-emerald-300",
    danger: "border-rose-900/70 bg-rose-950/40 text-rose-300",
    warning: "border-amber-900/70 bg-amber-950/40 text-amber-200",
  };

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 font-mono text-[11px] tracking-wide ${tones[tone] ?? tones.neutral}`}
    >
      {children}
    </span>
  );
}
