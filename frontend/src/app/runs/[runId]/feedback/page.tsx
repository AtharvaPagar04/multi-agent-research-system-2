import { notFound } from "next/navigation";

import { FeedbackLoopView } from "@/components/feedback-loop";
import { getResearchRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function FeedbackPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  return <FeedbackLoopView feedback={run.feedback} />;
}
