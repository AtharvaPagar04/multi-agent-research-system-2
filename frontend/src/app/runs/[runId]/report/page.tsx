import { notFound } from "next/navigation";

import { ReportView } from "@/components/report-view";
import { getResearchRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function ReportPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  return <ReportView report={run.report} />;
}
