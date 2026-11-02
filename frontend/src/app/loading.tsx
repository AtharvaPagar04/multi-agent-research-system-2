export default function Loading() {
  return (
    <div className="grid gap-4 lg:grid-cols-[auto_minmax(0,1fr)]">
      <div className="hidden lg:block lg:w-[280px]">
        <div className="h-96 rounded-lg border border-zinc-800/80 bg-[#0B0B0B]/92 p-4 animate-pulse">
          <div className="h-4 w-28 bg-zinc-800 rounded"></div>
          <div className="mt-4 h-8 bg-zinc-900 rounded"></div>
          <div className="mt-4 space-y-3">
            <div className="h-12 bg-zinc-900/50 rounded"></div>
            <div className="h-12 bg-zinc-900/50 rounded"></div>
            <div className="h-12 bg-zinc-900/50 rounded"></div>
          </div>
        </div>
      </div>
      <div className="space-y-4">
        <div className="rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 p-6 animate-pulse">
          <div className="h-4 w-32 bg-zinc-800 rounded"></div>
          <div className="mt-3 h-6 w-64 bg-zinc-800 rounded"></div>
          <div className="mt-4 h-10 w-full bg-zinc-900 rounded"></div>
        </div>
        <div className="rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 p-6 animate-pulse">
          <div className="h-4 w-40 bg-zinc-800 rounded"></div>
          <div className="mt-4 space-y-3">
            <div className="h-16 bg-zinc-900/40 rounded"></div>
            <div className="h-16 bg-zinc-900/40 rounded"></div>
          </div>
        </div>
      </div>
    </div>
  );
}
