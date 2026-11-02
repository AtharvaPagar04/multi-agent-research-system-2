import { Workspace } from "@/components/workspace";
import { listResearchRuns } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default async function HomePage() {
  const runs = await listResearchRuns();

  return <Workspace runs={runs} />;
}
