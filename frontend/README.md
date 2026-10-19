# Multi-Agent Research Citation Engine — Frontend

Artifact-backed research workbench for historical runs under `../evidence_runs/`.

## Stack

- Next.js 15 (App Router)
- TypeScript
- Tailwind CSS 4
- Lightweight local UI components (no live research execution)

## Launch

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:3000

## Data

The UI reads:

- `evidence_runs/evidence_*.json`
- `evidence_runs/claim_verifications_*.json`
- `evidence_runs/feedback_loop_*.json`
- Optional Markdown reports in the repo root or `evidence_runs/` that reference a run ID

Missing values render as unavailable. No Mesh API or research pipeline is invoked.

Optional overrides:

- `RESEARCH_REPO_ROOT`
- `RESEARCH_ARTIFACTS_DIR`
