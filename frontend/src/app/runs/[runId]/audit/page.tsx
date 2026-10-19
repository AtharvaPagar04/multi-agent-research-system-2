import { notFound } from "next/navigation";

import { QualityFindings } from "@/components/quality-findings";
import { getResearchRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function AuditPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  return <QualityFindings run={run} />;
}
