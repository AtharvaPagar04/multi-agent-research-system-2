import { Workspace } from "@/components/workspace";
import { listResearchRuns } from "@/lib/runs";

export const dynamic = "force-dynamic";

export default function HomePage() {
  const runs = listResearchRuns();

  return <Workspace runs={runs} />;
}
