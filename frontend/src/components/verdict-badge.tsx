import type { ClaimVerdict } from "@/lib/types";

const TONES: Record<
  ClaimVerdict | "unknown",
  { label: string; className: string; dot: string }
> = {
  supported: {
    label: "Supported",
    className: "border-emerald-900/70 bg-emerald-950/40 text-emerald-300",
    dot: "bg-emerald-400",
  },
  contradicted: {
    label: "Contradicted",
    className: "border-rose-900/70 bg-rose-950/40 text-rose-300",
    dot: "bg-rose-400",
  },
  insufficient_evidence: {
    label: "Insufficient evidence",
    className: "border-amber-900/70 bg-amber-950/40 text-amber-200",
    dot: "bg-amber-400",
  },
  unknown: {
    label: "Unavailable",
    className: "border-zinc-700/80 bg-zinc-900/70 text-zinc-400",
    dot: "bg-zinc-500",
  },
};

export function VerdictBadge({ verdict }: { verdict: ClaimVerdict | null }) {
  const tone = TONES[verdict ?? "unknown"];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 font-mono text-[11px] tracking-wide ${tone.className}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
      {tone.label}
    </span>
  );
}
