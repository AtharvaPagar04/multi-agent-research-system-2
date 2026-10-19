import Link from "next/link";
import type { ReactNode } from "react";

import { AnimatedBackground } from "@/components/animated-background";
import { Badge } from "@/components/badge";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen text-zinc-100">
      <AnimatedBackground />
      <header className="sticky top-0 z-20 border-b border-zinc-900/80 bg-[#050505]/85 backdrop-blur-sm">
        <div className="mx-auto flex max-w-[1600px] items-center justify-between gap-4 px-4 py-3 sm:px-6">
          <div className="flex items-center gap-3">
            <Link href="/" className="group">
              <div className="font-mono text-[12px] uppercase tracking-[0.18em] text-zinc-100 group-hover:text-white">
                Citation Engine
              </div>
              <div className="mt-0.5 font-mono text-[10px] tracking-[0.2em] text-zinc-500">
                Multi-Agent Research Workbench
              </div>
            </Link>
          </div>
          <div className="flex items-center gap-2">
            <Badge>Artifact-backed</Badge>
            <Badge>Local backend</Badge>
          </div>
        </div>
      </header>
      <main className="mx-auto w-full max-w-[1600px] px-4 py-6 sm:px-6 sm:py-8">
        {children}
      </main>
      <footer className="mx-auto w-full max-w-[1600px] px-4 pb-10 sm:px-6">
        <p className="font-mono text-[11px] leading-5 text-zinc-600">
          Frontend reads local evidence_runs artifacts and submits runs to the
          local FastAPI backend; research executes in the local engine
          subprocess, never in the browser.
        </p>
      </footer>
    </div>
  );
}
