import { notFound } from "next/navigation";

import { EvidenceExplorer } from "@/components/evidence-explorer";
import { getResearchRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function EvidencePage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  return <EvidenceExplorer records={run.evidence} />;
}
