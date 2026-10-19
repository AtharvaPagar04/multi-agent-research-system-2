import { notFound, redirect } from "next/navigation";

import { getResearchRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function RunIndexPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const normalized = runId.toLowerCase();
  const run = getResearchRun(normalized);
  if (!run) notFound();

  redirect(`/runs/${run.runId}/report`);
}
