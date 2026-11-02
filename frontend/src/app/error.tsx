"use client";

import Link from "next/link";
import { useEffect } from "react";

export default function ErrorBoundary({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Log error to console for diagnostic purposes
    console.error("Workspace error caught by boundary:", error);
  }, [error]);

  const isBackendUnreachable =
    error.message?.includes("fetch failed") ||
    error.message?.includes("ECONNREFUSED") ||
    error.message?.includes("backend");

  return (
    <div className="mx-auto flex max-w-xl flex-col items-start gap-4 py-24">
      <div className="font-mono text-[11px] uppercase tracking-[0.16em] text-amber-500">
        Communication Error
      </div>
      <h1 className="text-2xl font-medium tracking-tight text-zinc-50">
        {isBackendUnreachable
          ? "Unable to reach research backend"
          : "An unexpected error occurred"}
      </h1>
      <p className="text-[14px] leading-7 text-zinc-400">
        {error.message ||
          "Could not communicate with the local FastAPI backend service."}
      </p>
      {isBackendUnreachable ? (
        <div className="rounded-md border border-zinc-800 bg-black/40 p-3 font-mono text-[12px] text-zinc-400">
          Backend target: <code className="text-zinc-200">http://127.0.0.1:8001</code>
          <br />
          Verify backend is running with:{" "}
          <code className="text-zinc-300">
            .venv313/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8001
          </code>
        </div>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => reset()}
          className="rounded-md border border-zinc-700 bg-zinc-900 px-4 py-2 font-mono text-[12px] uppercase tracking-[0.14em] text-zinc-200 transition-colors hover:border-zinc-500 hover:text-white"
        >
          Try again
        </button>
        <Link
          href="/"
          className="rounded-md border border-zinc-800 bg-[#111111] px-4 py-2 font-mono text-[12px] uppercase tracking-[0.14em] text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200"
        >
          Return to workspace
        </Link>
      </div>
    </div>
  );
}
