export default function RunLoading() {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 p-6">
        <div className="h-4 w-36 bg-zinc-800 rounded"></div>
        <div className="mt-3 h-7 w-3/4 bg-zinc-800 rounded"></div>
        <div className="mt-3 flex gap-3">
          <div className="h-4 w-28 bg-zinc-900 rounded"></div>
          <div className="h-4 w-32 bg-zinc-900 rounded"></div>
        </div>
      </div>
      <div className="rounded-lg border border-zinc-800/90 bg-[#0B0B0B]/92 p-6">
        <div className="h-4 w-48 bg-zinc-800 rounded"></div>
        <div className="mt-4 space-y-3">
          <div className="h-20 bg-zinc-900/40 rounded"></div>
          <div className="h-20 bg-zinc-900/40 rounded"></div>
          <div className="h-20 bg-zinc-900/40 rounded"></div>
        </div>
      </div>
    </div>
  );
}
