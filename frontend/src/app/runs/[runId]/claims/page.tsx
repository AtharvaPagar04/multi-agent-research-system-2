import { notFound } from "next/navigation";

import { ClaimSummary } from "@/components/claim-summary";
import { getResearchRun } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function ClaimsPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  const run = getResearchRun(runId.toLowerCase());
  if (!run) notFound();

  return (
    <ClaimSummary
      claims={run.claims}
      supported={run.supportedClaims}
      contradicted={run.contradictedClaims}
      insufficient={run.insufficientClaims}
      total={run.totalClaims}
      warnings={run.claimWarnings}
    />
  );
}
