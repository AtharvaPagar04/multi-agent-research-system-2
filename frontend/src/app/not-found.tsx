import Link from "next/link";

export default function NotFound() {
  return (
    <div className="mx-auto flex max-w-xl flex-col items-start gap-4 py-24">
      <div className="font-mono text-[11px] uppercase tracking-[0.16em] text-zinc-500">
        Run unavailable
      </div>
      <h1 className="text-2xl font-medium tracking-tight text-zinc-50">
        This research run could not be loaded
      </h1>
      <p className="text-[14px] leading-7 text-zinc-400">
        No valid artifacts were found for this run ID. The artifact may be
        missing, malformed, or associated with a different run ID.
      </p>
      <Link
        href="/"
        className="rounded-md border border-zinc-800 bg-[#111111] px-4 py-2 font-mono text-[12px] uppercase tracking-[0.14em] text-zinc-300 transition-colors hover:border-zinc-600 hover:text-zinc-100"
      >
        Return to latest run
      </Link>
    </div>
  );
}
