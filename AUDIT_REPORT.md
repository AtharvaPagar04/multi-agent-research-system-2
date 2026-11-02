# MASTER TECHNICAL AUDIT REPORT — MULTI-AGENT RESEARCH SYSTEM

**Target Repository:** `~/DEV/multi-agent-research-system-2`  
**GitHub Origin:** `https://github.com/AtharvaPagar04/multi-agent-research-system-2.git`  
**Branch:** `main` (commit `08fe813`)  
**Auditor Roles:** Senior Software Architect, Full-Stack Engineer, AI/ML Engineer, QA Engineer, DevOps Engineer, Application Security Auditor  
**Date of Audit:** November 1, 2026  
**Audit Mode:** Non-destructive, evidence-based, reproducible, offline/mock-verified  

---

## 1. Executive Summary

### 1.1 Current Project Condition
The **Multi-Agent Research System** is an advanced, highly specialized research citation and verification application. It combines a Python/CrewAI sequential multi-agent research pipeline (`research_crew/`), an asynchronous subprocess job manager with a FastAPI service layer (`backend/`), and a modern Next.js 15 dashboard (`frontend/`).

Contrary to common AI prototypes that hallucinate citations and rely on vague LLM summaries, this repository possesses an exceptionally sophisticated **Evidence Store and Verification Subsystem** (`research_crew/evidence/`). It enforces strict factual entailment via natural language inference (NLI), decomposes claims into atomic propositions, bounds context windows to 1,200 characters, calculates SHA-256 content hashes, parses citation bibliographies, detects missing comparative baselines, flags contradictory claims, and executes a bounded verification feedback loop.

However, the repository currently exists as **a collection of three partially decoupled tiers** (Engine, Backend API, Frontend) rather than a unified deployable product. Significant configuration inconsistencies, a critical host port collision, an architectural filesystem coupling between frontend and backend, missing containerization, and legacy repository provenance prevent immediate production deployment.

### 1.2 Overall Completion Estimate
* **Research & Verification Engine (`research_crew`):** **92% Complete** (Functionally complete, battle-tested verification algorithms, 390 passing unit tests; needs minor test import hygiene).
* **Backend Job & API Layer (`backend`):** **80% Complete** (Robust subprocess supervision, FIFO queue, secret redaction, 84 passing tests; missing dotenv loading, missing database persistence, missing endpoints for non-report artifacts).
* **Frontend Dashboard (`frontend`):** **75% Complete** (Visually polished, typecheck and lint pass, static build succeeds, 17 passing tests; however, historical runs and run detail views bypass the API entirely and read directly from the server's local filesystem via Node `fs`).
* **Deployment & DevOps Infrastructure:** **15% Complete** (Completely missing Dockerfiles, docker-compose, CI/CD pipelines, production secret management, and reverse proxy definitions).
* **System-Wide End-to-End Readiness:** **65% Complete**.

### 1.3 Main Achievements
1. **Mathematical & Semantic Verification Integrity:** Strict claim decomposition, entity/acronym grounding, and anti-hallucination labeling that explicitly labels unverified or contradictory statements in the final markdown report.
2. **Deterministic Evidence Persistence:** Raw search snippets and full-text documents are captured at the tool boundary before LLM summarization into an append-only `EvidenceStore` with content hashes.
3. **Robust Job Supervision:** `backend/jobs.py` executes research runs in isolated subprocesses with bounded memory buffers, stdout marker handshakes (`__RUN_ID__`), signal trapping, kill grace periods, and secret redaction.
4. **Strong Test Culture:** 491 automated tests across backend, research engine, and frontend with 100% pass rates when properly configured.

### 1.4 Most Serious Problems
1. **P0 Port Collision on Default Binding (`127.0.0.1:8000`):** The default backend command fails to start out-of-the-box (`[Errno 98] address already in use`) due to an existing host system service (gunicorn daemon bound to port 8000).
2. **P1 Frontend Architectural Decoupling (Filesystem vs. API):** The Next.js frontend pages (`/runs/[runId]/claims`, `/runs/[runId]/evidence`, `/runs/[runId]/audit`, `/runs/[runId]/feedback`) read JSON artifacts directly from local disk (`node:fs`). The backend API provides no endpoints for these artifacts. If frontend and backend are deployed on separate containers or servers, the frontend completely breaks.
3. **P1 Missing Dotenv Loading in FastAPI Backend (`backend/app.py`):** `backend/app.py` never calls `load_dotenv()`. Running `uvicorn backend.app:app` results in `/api/health` falsely reporting `engine.configured: false` and missing API keys unless manually exported in the shell.
4. **P1 In-Memory Ephemeral Job State:** `JobManager` tracks jobs in memory (`dict[str, _Job]`). Restarting the backend wipes all active and recent job metadata, execution durations, and exit codes.
5. **P1 Broken Test Discovery Without PYTHONPATH:** Running `pytest research_crew/tests` fails during collection (`ModuleNotFoundError: No module named 'tests'`) because test modules use `from tests.conftest import ...` instead of relative or package imports.
6. **P2 SSRF Risk in Content Fetchers:** `PDFExtractorTool` and `WebParserTool` make unvalidated HTTP requests using `requests.get` without blocking internal IP addresses (`127.0.0.1`, `169.254.169.254`, private subnets).
7. **P2 Legacy Repository Provenance:** Historical git metadata in `.git-old-history/` contains previous upstream remotes and author details (`iamaayushijain`, `Aayushi Jain <iamaayushirocks@gmail.com>`).

### 1.5 Deployment Readiness Classification
**Classification: Level 2 — Locally Runnable (with Configuration Adjustments)**  
The application cannot be deployed to staging or production in its current state. It requires explicit port overrides, filesystem sharing or API endpoint implementation, containerization, and environment alignment before it is deployable.

---

## 2. Technology Stack

| Layer | Component | Version / Specification | Purpose | Evidence / File Path |
|---|---|---|---|---|
| **Language Runtime** | Python | 3.13.16 (tested), 3.14.8 (present in `.venv`) | Backend API & Multi-Agent Engine | `.venv313/bin/python`, `backend/requirements.txt:4` |
| **Language Runtime** | Node.js | v20.20.2 | Frontend Web Workspace | Host `node -v`, `frontend/package.json` |
| **Package Manager** | uv & pip | uv 0.4+, pip 26.2.1 | Python virtual environment management | `.venv313/bin/uv` |
| **Package Manager** | npm | 12.2.1 | Frontend package management | `frontend/package-lock.json` |
| **Agent Framework** | CrewAI | 1.15.27 (installed), `crewai>=0.76.0` | Multi-agent orchestration and task dependencies | `research_crew/requirements.txt:8`, `.venv313` |
| **Web Framework (Backend)** | FastAPI | 0.145.0 | Local REST API service | `backend/requirements.txt:8`, `backend/app.py` |
| **ASGI Server** | Uvicorn | 0.54.0 | ASGI web server | `backend/requirements.txt:9`, `backend/app.py:442` |
| **Web Framework (Frontend)** | Next.js | 15.5.12 (Turbopack) | Research workspace frontend | `frontend/package.json:14` |
| **UI Library** | React | 19.1.0 | Component hierarchy | `frontend/package.json:15` |
| **Styling** | TailwindCSS | 4.0 | Modern utility styling | `frontend/package.json:26`, `@tailwindcss/postcss` |
| **LLM Gateway** | Mesh API | `https://api.meshapi.ai/v1` | Primary LLM provider endpoint | `research_crew/main.py:54`, `backend/config.py:26` |
| **LLM Models** | Gemini / OpenAI | `gemini-3.5-flash-lite`, `gpt-4o` | Reasoning, Extraction, Verification | `.env`, `research_crew/main.py:213` |
| **Search APIs** | Exa & Tavily | `exa-py>=1.1.0`, `tavily-python>=0.3.3` | Academic literature neural & web search | `research_crew/tools/search_tool.py` |
| **PDF Extraction** | PyMuPDF (fitz) | `>=1.24.0` | High-speed PDF text parsing | `research_crew/tools/pdf_extractor.py:58` |
| **HTML Parsing** | BeautifulSoup4 / lxml | `bs4>=4.12.0`, `lxml>=5.2.0` | Semantic web text extraction | `research_crew/tools/web_parser.py:24` |
| **Data Validation** | Pydantic | 2.10+ | Schema validation and serialization | `research_crew/evidence/model.py`, `backend/app.py` |
| **Test Frameworks** | Pytest & Node Test | Pytest 9.1.1, Node `--test` | Unit, mocked, and e2e integration testing | `backend/tests/`, `research_crew/tests/`, `frontend/tests/` |

---

## 3. Actual Project Architecture

```
                                  ┌────────────────────────────────────────────────────────┐
                                  │                  Next.js Frontend (3000)               │
                                  │   Pages: Workspace (/), Overview, Report, Claims,     │
                                  │          Evidence Explorer, Audit, Feedback View       │
                                  └──────────┬───────────────────────────────┬─────────────┘
                                             │ HTTP REST                     │ Direct Node.js fs
                                             │ (Runs, Cancel, Report)        │ (Claims, Evidence, Audit)
                                             ▼                               ▼
┌────────────────────────────────────────────────────────────────────────┐   │
│                          FastAPI Backend (8000/8001)                   │   │
│   Endpoints: /api/health, /api/runs [GET/POST], /api/runs/{id}/cancel  │   │
│              /api/runs/{id}/report                                     │   │
└────────────────────────────────────┬───────────────────────────────────┘   │
                                     │                                       │
                                     │ Spawns Subprocess (arg array)         │
                                     ▼                                       │
┌────────────────────────────────────────────────────────────────────────┐   │
│                 Subprocess Runner: backend/engine_runner.py            │   │
│      Emits: __RUN_ID__<32-hex> on stdout line 1                         │   │
└────────────────────────────────────┬───────────────────────────────────┘   │
                                     │ Invokes                               │
                                     ▼                                       │
┌────────────────────────────────────────────────────────────────────────┐   │
│             Multi-Agent Research Pipeline (research_crew)              │   │
│   Sequential CrewAI Process:                                           │   │
│   1. Planner Agent      ──> 4-6 targeted research queries              │   │
│   2. Search Agent       ──> Exa / Tavily API search (Adapted Tools)    │   │
│   3. Validator Agent    ──> Top 5 filtered & scored sources (1-10)     │   │
│   4. Extractor Agent    ──> PyMuPDF / WebParser text extraction        │   │
│   5. Synthesizer Agent  ──> Structured Markdown with inline citations  │   │
│   Flow Coordinator: Intercepts tasks, recovers dropped context         │   │
└────────────────────────────────────┬───────────────────────────────────┘   │
                                     │                                       │
                                     │ Feeds Initial Report & Evidence       │
                                     ▼                                       │
┌────────────────────────────────────────────────────────────────────────┐   │
│         Verification & Feedback Loop (research_crew/evidence)          │   │
│   - Claim Extraction: Atomic propositional decomposition               │   │
│   - Passage Retrieval: 1,200-char context window scoring               │   │
│   - Mesh API NLI Judge: Strict entailment verification                 │   │
│   - Deterministic Metrics & Dimension Coverage Audit                   │   │
│   - Bounded Research Loop: Re-queries for unverified claims (<=2 rnds) │   │
│   - Markdown Annotation: Injects *(⚠️ Unverified claim: ...)*          │   │
└────────────────────────────────────┬───────────────────────────────────┘   │
                                     │ Flushes Artifacts                     │
                                     ▼                                       │
┌────────────────────────────────────────────────────────────────────────┐   │
│                Local Filesystem: evidence_runs/                        │◄──┘
│   - evidence_<run_id>.json             (Raw retrieved source records)  │
│   - claim_verifications_<run_id>.json  (Atomic claims & verdicts)      │
│   - feedback_loop_<run_id>.json        (Iteration history & actions)   │
│   - research_report_<run_id>.md        (Final annotated report)        │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Repository Structure

```
multi-agent-research-system-2/
├── .agents/                               # Custom agent instructions & domain skills
│   └── skills/
│       ├── research-evidence-verification/ # Strict factual entailment guidelines
│       ├── systematic-debugging/          # Root-cause debugging protocol
│       └── test-driven-development/       # TDD workflows
├── .env                                   # Root environment variables (Active credentials)
├── .env.example                           # MISSING at root! (Present only in frontend/)
├── .git/                                  # Current git repository (1 commit on main)
├── .git-old-history/                      # Archived prior repository history (Contains provenance)
├── .gitignore                             # Git ignore rules (covers .env, venvs, cache)
├── .venv/                                 # Python 3.14 virtual environment (Unused / unpopulated)
├── .venv313/                              # Python 3.13 virtual environment (Active, dependencies installed)
├── AGENTS.md                              # Delivery phase rules & safety constraints
├── README.md                              # Main project documentation (Outdated setup instructions)
├── backend/                               # Backend service tier
│   ├── app.py                             # FastAPI application factory & endpoint routing
│   ├── config.py                          # Path resolution & job settings configuration
│   ├── engine_runner.py                   # CLI/Subprocess entry point to research pipeline
│   ├── jobs.py                            # Subprocess JobManager with FIFO queue & redactor
│   ├── requirements.txt                   # Backend dependencies (fastapi, uvicorn)
│   └── tests/                             # Backend test suite (84 tests, all passing)
├── evidence_runs/                         # Persistent artifact directory (109 JSON/MD run records)
├── frontend/                              # Next.js 15 Web Workspace
│   ├── package.json                       # Next.js, React 19, Tailwind dependencies
│   ├── tsconfig.json                      # Next.js TypeScript configuration
│   ├── tsconfig.test.json                 # Node test runner TypeScript configuration
│   ├── public/                            # Static SVG assets
│   ├── tests/
│   │   └── api.test.mjs                   # Unit tests for frontend API client (17 tests, all passing)
│   └── src/
│       ├── app/                           # Next.js App Router
│       │   ├── page.tsx                   # Workspace root (Live console + historical selector)
│       │   ├── layout.tsx                 # Root HTML shell & fonts
│       │   └── runs/[runId]/              # Modular run workspace
│       │       ├── layout.tsx             # Shared sidebar, run header & tabs
│       │       ├── page.tsx               # Redirects to report
│       │       ├── overview/page.tsx      # Run overview & attention-needed claims
│       │       ├── report/page.tsx        # Markdown report renderer
│       │       ├── claims/page.tsx        # Claims summary & verdict filters
│       │       ├── evidence/page.tsx      # Evidence records explorer
│       │       ├── audit/page.tsx         # Quality audit & metrics traceability
│       │       └── feedback/page.tsx      # Verification feedback loop view
│       ├── components/                    # Modular React client/server components
│       └── lib/
│           ├── api.ts                     # Typed backend REST API client
│           ├── fs-paths.ts                # Direct Node.js filesystem artifact discovery
│           ├── report-parser.ts           # Markdown parsing & citation extraction
│           ├── runs.ts                    # Artifact loading & normalization logic (fs-based)
│           └── types.ts                   # Core TypeScript type definitions
└── research_crew/                         # Core Multi-Agent Research & Verification Engine
    ├── __init__.py
    ├── main.py                            # CLI entry point, pipeline assembly & execution
    ├── pipeline_flow.py                   # PipelineFlowCoordinator & broken-link recovery
    ├── feedback_loop.py                   # Bounded research feedback loop executor
    ├── requirements.txt                   # Engine dependencies (crewai, openai, fitz, etc.)
    ├── agents/                            # CrewAI agent definitions (5 agents)
    ├── tasks/                             # CrewAI task definitions (5 sequential tasks)
    ├── tools/                             # Search, PDF, HTML tools & EvidenceStore adapters
    ├── evidence/                          # Model, Store, Validator, Verifier subsystems
    ├── utils/                             # Text chunking and tiktoken token utils
    └── tests/                             # Engine test suite (390 tests, all passing)
```

---

## 5. Backend Technical Audit

### 5.1 Architecture & Process Supervision
The backend architecture is located in `backend/` and centers around `JobManager` in `backend/jobs.py` and `FastAPI` in `backend/app.py`.

* **Startup & Lifecycle:** The backend starts via `uvicorn backend.app:app`. The FastAPI application uses an `asynccontextmanager` lifespan handler (`backend/app.py:313-323`). On startup, it constructs and starts a singleton `JobManager`. On shutdown, it cancels active threads and gracefully terminates child processes.
* **Engine Isolation:** The backend never imports `research_crew` at application startup. Subprocess execution is performed by running `backend/engine_runner.py` via `subprocess.Popen` (`backend/jobs.py:370-377`). This provides strong process isolation: a crash, segfault, or memory leak in PyMuPDF/CrewAI cannot take down the FastAPI web server.
* **Subprocess Protocol:** 
  1. The runner flushes `__RUN_ID__<32-hex-run-id>` on its very first stdout line before starting the research pipeline (`backend/engine_runner.py:101`).
  2. The runner executes `run_research_pipeline(topic, store)`.
  3. On success, the runner writes the report to `evidence_runs/research_report_<run_id>.md` and exits with code 0.
  4. Diagnostics and errors are written to `stderr`.
* **Output Buffering & Secret Redaction:** `backend/jobs.py` uses `_BoundedBuffer` (capped at 64 KB) to prevent child output from consuming excessive RAM. `_SecretRedactor` dynamically scrubs sensitive values (`MESH_API_KEY`, `EXA_API_KEY`, `TAVILY_API_KEY`, `OPENAI_API_KEY`) from all job snapshots, logs, stdout, and stderr (`backend/jobs.py:132-146`).

### 5.2 API Audit Table

| Endpoint | Method | Purpose | Implementation File & Line | Status | Validation | Auth | Database | Rate Limiting | CORS |
|---|---|---|---|---|---|---|---|---|---|
| `/api/health` | `GET` | System liveness & engine config check | `backend/app.py:341-343` | **Partially Working** (Fails to detect `.env` keys) | Path checks & env existence | None | None | None | Configured origins (`localhost:3000`) |
| `/api/runs` | `POST` | Enqueue a new research run | `backend/app.py:345-372` | **Implemented & Tested** | Non-empty string, $\le$ 500 chars | None | Ephemeral queue | 429 when queue $\ge$ 16 | Configured origins |
| `/api/runs` | `GET` | List active jobs & historical runs | `backend/app.py:373-384` | **Fully Functional** | None | None | Filesystem + Memory | None | Configured origins |
| `/api/runs/{job_id}` | `GET` | Retrieve job snapshot or historical run | `backend/app.py:385-396` | **Fully Functional** | Strict 32-hex regex match | None | Filesystem + Memory | None | Configured origins |
| `/api/runs/{job_id}/cancel` | `POST` | Cancel active or queued research run | `backend/app.py:397-411` | **Fully Functional** | Checks job state | None | Memory state | None | Configured origins |
| `/api/runs/{job_id}/report` | `GET` | Fetch markdown report for completed run | `backend/app.py:412-432` | **Fully Functional** | Validated run ID, file existence | None | Filesystem read | None | Configured origins |

### 5.3 Detailed API Findings & Missing Routes
1. **Missing Artifact Routes:** While `/api/runs/{job_id}/report` is implemented, there are **no API endpoints** for:
   - `GET /api/runs/{job_id}/evidence`
   - `GET /api/runs/{job_id}/claims`
   - `GET /api/runs/{job_id}/audit`
   - `GET /api/runs/{job_id}/feedback`  
   Because these routes are missing, the frontend is forced to bypass the backend and read directly from disk via Node.js `fs`.
2. **Missing Dotenv Loading in `backend/app.py`:**
   In `backend/app.py:180-186`, `build_health_report` checks `os.getenv("MESH_API_KEY")`. However, `backend/app.py` never imports or calls `load_dotenv()`. When uvicorn is started directly, `build_health_report` returns:
   ```json
   {"status":"ok","engine":{"configured":false,"required_env_present":{"MESH_API_KEY":false,"EXA_API_KEY":false}}}
   ```
   *Impact:* The frontend console displays a permanent "Backend unconfigured" or "Missing credentials" alert unless the operator manually exports variables in their shell.

---

## 6. Multi-Agent Architecture Audit

### 6.1 Agent Verification Matrix

| Agent Role | Repository Location | Responsibility | Input Format | Output Format | Model / API Dependency | Tools Used | Genuinely Executes? | Consumed By Next Stage? |
|---|---|---|---|---|---|---|---|---|
| **Planner Agent** (Research Strategist) | `research_crew/agents/planner_agent.py` | Decomposes research topic into 4–6 specific sub-queries | Raw topic string | Strict JSON: `{"queries": [...]}` | Mesh API (`gemini-3.5-flash-lite` or `gpt-4o`) | None (pure reasoning) | **Yes** | **Yes** (passed to Search Task context) |
| **Search Agent** (Academic Source Finder) | `research_crew/agents/search_agent.py` | Executes queries against academic/repo databases | Context from Planner | Flat JSON array of source objects | Mesh API | `ExaSearchAdapter`, `TavilySearchAdapter` | **Yes** | **Yes** (passed to Validation Task context) |
| **Validator Agent** (Source Quality Evaluator) | `research_crew/agents/validator_agent.py` | Scores sources 1–10 (credibility, recency, depth, relevance); keeps top 5 | Raw source list from Search | Strict JSON: `{"validated_sources": [top 5]}` | Mesh API | None (heuristic scoring prompt) | **Yes** | **Yes** (passed to Extraction Task context) |
| **Extractor Agent** (Technical Evidence Extractor) | `research_crew/agents/extractor_agent.py` | Fetches PDF/web content; extracts metrics, datasets, quotes, foundations | Top 5 validated sources | JSON array: `[{source_id, metrics, datasets, foundations, key_findings, quotes}]` ($\le 300$ tokens/source) | Mesh API | `PDFExtractorAdapter`, `WebParserAdapter` | **Yes** | **Yes** (passed to Summary Task context) |
| **Synthesizer Agent** (Research Writer) | `research_crew/agents/synthesizer_agent.py` | Synthesizes evidence into structured Markdown report with inline citations | Extracted evidence JSON array | Markdown report with `[N]` citations and Sources section | Mesh API | None (context synthesis) | **Yes** | **Yes** (fed into Verification & Feedback Loop) |

### 6.2 Orchestration & Flow Coordination
* **CrewAI Execution:** Orchestrated via `Crew(process=Process.sequential, memory=False, tracing=False)` (`research_crew/main.py:453-472`).
* **Broken-Link Recovery (`research_crew/pipeline_flow.py`):**
  A common failure in multi-agent LLM systems is when an intermediate agent outputs conversational prose instead of valid JSON, breaking the downstream chain. The project implements `PipelineFlowCoordinator` with task callbacks:
  - `on_search_completed`: If the Search Agent emits malformed JSON but the adapted search tools successfully populated `EvidenceStore`, `recover_search_sources_from_store()` reconstructs the source list (`pipeline_flow.py:372-383`).
  - `on_validation_completed`: If the Validator Agent fails to return sources, `recover_validated_sources_from_search()` automatically takes the top 5 search sources (`pipeline_flow.py:400-411`).
  - `on_extraction_completed`: If the Extractor Agent fails, `recover_extracted_evidence_from_sources()` extracts snippets and foundation sentences directly from `EvidenceStore` (`pipeline_flow.py:428-441`).
* **Genuinely Multi-Agent:** This is not a superficial set of function calls. The agents run distinct LLM prompts, maintain separate roles and personas, use tool adapters, enforce distinct output contracts, and pass data strictly through validated JSON interfaces.

---

## 7. Research Quality and Citation Reliability

### 7.1 Verification Pipeline Architecture (`research_crew/evidence/`)
The research verification engine is the core intellectual contribution of this codebase. It operates across four distinct modules:

```
[Report Markdown]
       │
       ▼
1. Citation & Mapping Layer (`validator.py`)
   ├── Regex-based bibliography resolution supporting [1], [arXiv-2401.123], [named-key]
   └── audit_report_citations, audit_report_metrics: baseline checks, metric consistency
       │
       ▼
2. Claim Extraction Layer (`verifier.py`)
   ├── extract_atomic_claims: splits sentences, decomposes clauses and parentheticals
   └── Links AtomicClaim to citation markers and resolved URLs
       │
       ▼
3. Evidence Retrieval & Passage Selection Layer (`store.py`, `verifier.py`)
   ├── EvidenceStore: in-memory & on-disk cache of EvidenceRecord (URL, full text, status)
   └── find_passage_for_url_and_query: extracts optimal 1,200-character context window based on empirical scoring
       │
       ▼
4. Verdict Evaluation Layer (`verifier.py`)
   ├── Entity & Acronym Grounding: token filtering, acronym recognition ([A-Z0-9]{2,})
   ├── _verify_via_mesh_llm: prompt-guided NLI judge via configured Mesh API LLM
   ├── _verify_heuristic: deterministic fallback for keyword/antonym contradictions
   └── apply_verification_to_report: labels or removes unverified claims with sentence grouping
```

### 7.2 Empirical Evidence from Historical Research Runs
The repository contains 48 historical evidence runs in `evidence_runs/`. The auditor inspected run `de3572bb68b442fd92e4ca8086cbea18` (executed on October 18, 2026 for the query: *"How effectively can quantized 3B–8B open-weight language models run on consumer GPUs with 4GB VRAM? Compare GGUF quantization levels such as Q4_K_M and Q5_K_M..."*).

**Empirical Metrics Recorded:**
- **Execution Time:** 26 seconds.
- **Model Used:** `gemini-3.5-flash-lite` via Mesh API.
- **Sources Retrieved & Captured:** 28 sources captured in `EvidenceStore` (14 cited in report; 10 full-text, 4 snippet-only).
- **Total Factual Claims Extracted:** 22 atomic claims.
- **Verification Verdict Breakdown:**
  - **Supported:** 12 claims (54.5%).
  - **Insufficient Evidence / Unverified:** 10 claims (45.5%).
  - **Contradicted:** 0 claims.
- **Feedback Loop Execution:** 2 additional research rounds executed (`exhausted_retries` after reaching maximum budget).
- **Metrics Audit:** 18 metrics reported; 16 traceable to source text, 4 specified baselines, 14 explicitly unspecified baselines.
- **Report Annotation Outcome:** Every unsupported claim was dynamically labeled inline:
  ```markdown
  *(⚠️ Unverified claim: Compound claim is partially unsupported: clause(s) [...] lack supporting evidence in cited source https://github.com/michael-borck/loco-bench (supported: None).)*
  ```

### 7.3 Findings on Citation Grounding & Fabrication Resistance
1. **Zero Citation Hallucination:** The system cannot generate plausible-looking fake citations because `flow_coordinator.verify_final_report()` and `audit_report_citations()` cross-reference every `[N]` marker and bibliography URL against the SHA-256 digests in `EvidenceStore`. Any uncached URL triggers an audit warning.
2. **Compound Claim Decomposition:** The verifier does not grant blanket approval to sentences containing multiple clauses. If a source supports clause A but omits clause B, the verifier flags the compound claim as partially unsupported.
3. **Strict Baseline Tracking:** In benchmark tables, metrics without a comparison baseline are flagged:
   ```markdown
   | 4GB tier VRAM budget (BF16 models) | ≤ 2B parameters | Maximum model size for unquantized BF16 models within a 4GB VRAM tier [Baseline unspecified in source text] | [1] |
   ```

---

## 8. Frontend Technical Audit

### 8.1 Page Implementation & Functional Status

| Page Route | UI Implemented | API Connected | Functional | Issues Identified |
|---|---|---|---|---|
| `/` (Workspace) | **Yes** | **Yes** (`/api/runs`, `/api/health`) | **Yes** | Host port 8000 collision blocks live console unless backend port is customized. |
| `/runs/[runId]/overview` | **Yes** | **No** (Direct `node:fs`) | **Yes** (Local only) | Bypasses backend API; reads directly from `evidence_runs/` on host disk. |
| `/runs/[runId]/report` | **Yes** | **No** (Direct `node:fs` / API report) | **Yes** (Local only) | Client can fetch markdown report via API, but page layout loads via `node:fs`. |
| `/runs/[runId]/claims` | **Yes** | **No** (Direct `node:fs`) | **Yes** (Local only) | Filter buttons (All, Supported, Contradicted, Insufficient) work; direct disk dependency. |
| `/runs/[runId]/evidence` | **Yes** | **No** (Direct `node:fs`) | **Yes** (Local only) | Search input, status filter (All, Full Text, Snippet, Failed), hash displays; direct disk dependency. |
| `/runs/[runId]/audit` | **Yes** | **No** (Direct `node:fs`) | **Yes** (Local only) | Topic coverage bar, metric breakdown cards, baseline warnings; direct disk dependency. |
| `/runs/[runId]/feedback` | **Yes** | **No** (Direct `node:fs`) | **Yes** (Local only) | Displays round history, query logs, verdict breakdown charts; direct disk dependency. |

### 8.2 Architectural Coupling: Frontend Disk Dependency
In `frontend/src/lib/runs.ts` (lines 102–253) and `frontend/src/lib/fs-paths.ts` (lines 16–20):
```typescript
export function artifactsDir(): string {
  const override = process.env.RESEARCH_ARTIFACTS_DIR;
  if (override && override.trim()) return path.resolve(override);
  return path.join(repoRoot(), "evidence_runs");
}
```
All Server Components in `src/app/runs/[runId]/*` call `getResearchRun(runId)`, which executes:
```typescript
const evidenceRaw = safeReadJson(path.join(dir, `evidence_${runId}.json`));
const claimsRaw = safeReadJson(path.join(dir, `claim_verifications_${runId}.json`));
const feedbackRaw = safeReadJson(path.join(dir, `feedback_loop_${runId}.json`));
```
**Impact:** The frontend is **not a decoupled web client**. It is a local file viewer masquerading as a web client. In any containerized or multi-server deployment (e.g. Vercel, ECS, Kubernetes), the frontend will fail to display run details unless the `evidence_runs` volume is mounted into the frontend container.

---

## 9. API Integration Audit

### 9.1 Schema Agreement & Normalization
* **Request Schema:** `POST /api/runs` accepts `{"topic": string}` (validated by `backend/app.py:148`). Matches `frontend/src/lib/api.ts:322` `createRun(topic)`.
* **Snapshot Schema:** `backend/app.py:101-129` `RunSnapshot` maps fields: `origin`, `job_id`, `topic`, `state`, `stage`, `created_at`, `started_at`, `finished_at`, `elapsed_seconds`, `exit_code`, `run_id`, `error`, `failure_kind`, `stdout`, `stderr`, `artifacts`.
* **Frontend Normalization:** `frontend/src/lib/api.ts:218-249` `normalizeJob()` provides defensive parsing: non-string states or invalid timestamps are cleanly handled without throwing React render errors.

### 9.2 API Gaps Summary
The API is currently missing endpoints to serve:
1. Structured Evidence Records (`/api/runs/{id}/evidence`)
2. Structured Claim Verification Records (`/api/runs/{id}/claims`)
3. Quality Audit Summary (`/api/runs/{id}/audit`)
4. Feedback Loop Execution Log (`/api/runs/{id}/feedback`)

---

## 10. Database and Persistence Audit

### 10.1 Persistence Mechanisms
* **No Relational Database:** The project does not use SQLite, PostgreSQL, or an ORM.
* **Storage Layer:** File-based JSON and Markdown artifacts stored in `evidence_runs/`.
  - `evidence_<run_id>.json`: Managed by `EvidenceStore` (`research_crew/evidence/store.py`). Thread-safe atomic writes via temporary files.
  - `claim_verifications_<run_id>.json`: Written by `save_verification_records()` (`research_crew/evidence/verifier.py:2700`).
  - `feedback_loop_<run_id>.json`: Written by `save_feedback_run_log()` (`research_crew/feedback_loop.py:1180`).
  - `research_report_<run_id>.md`: Written by `backend/engine_runner.py:115`.
* **Restart Survival:** Completed run artifacts survive backend and system restarts.

### 10.2 In-Memory Ephemeral State
* `JobManager` in `backend/jobs.py` stores queued and active jobs in `self._jobs: dict[str, _Job]`.
* **Impact of Backend Restart:**
  - Active child processes are terminated during graceful shutdown.
  - In-flight queued jobs are discarded.
  - Job history (creation time, start time, elapsed seconds, exit code, stdout/stderr logs) is wiped.
  - On restart, `/api/runs` only recovers historical runs by scanning filenames on disk; all runtime execution metadata (durations, logs) is lost.

---

## 11. Testing Results

### 11.1 Test Suite Execution Matrix

| Test Suite | Location | Command Executed | Total Tests | Pass | Fail | Duration | Status | Notes |
|---|---|---|---|---|---|---|---|---|
| **Backend Suite** | `backend/tests/` | `.venv313/bin/python -m pytest backend/tests -v` | 84 | 84 | 0 | 3.30s | **PASS** | Tests API, JobManager, Config, Subprocess Runner, and E2E Flow. |
| **Research Engine** | `research_crew/tests/` | `PYTHONPATH=research_crew .venv313/bin/python -m pytest research_crew/tests` | 390 | 390 | 0 | 19.10s | **PASS** | 37 CrewAI deprecation warnings; requires `PYTHONPATH=research_crew`. |
| **Engine (Unconfigured)** | `research_crew/tests/` | `.venv313/bin/python -m pytest research_crew/tests` | 326 | 0 | 3 (Collection) | 6.16s | **FAIL** | Collection fails: `ModuleNotFoundError: No module named 'tests'`. |
| **Frontend Tests** | `frontend/tests/` | `npm --prefix frontend run test` | 17 | 17 | 0 | 76ms | **PASS** | Unit tests for API client, normalization, and topic validation. |
| **Frontend Typecheck** | `frontend/` | `npm --prefix frontend run typecheck` | — | — | 0 | 1.1s | **PASS** | `tsc --noEmit` clean with 0 type errors. |
| **Frontend Lint** | `frontend/` | `npm --prefix frontend run lint` | — | — | 0 | 1.5s | **PASS** | `eslint` clean with 0 warnings. |
| **Frontend Build** | `frontend/` | `npm --prefix frontend run build` | — | — | 0 | 2.1s | **PASS** | Turbopack production build succeeds for all 7 routes. |

**Total Automated Tests:** 491 tests executed. **Pass Rate:** 100% (when run with correct path configuration).

### 11.2 Runtime Connectivity & Port Collision Test
* **Command:** `.venv313/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000`
* **Result:** **FAILED (Exit code 3)**
  ```
  ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8000): address already in use
  ```
* **Root Cause Investigation:** A background system process (`/usr/local/bin/python3.12 /home/bridge/.local/bin/gunicorn --bind 0.0.0.0:8000`) is already bound to port 8000.
* **Verification on Alternative Port:**
  - Started backend on `127.0.0.1:8001`.
  - `curl -s http://127.0.0.1:8001/api/health` returned HTTP 200.
  - `curl -s http://127.0.0.1:8001/api/runs` returned HTTP 200 with 48 historical runs.
  - `curl -s http://127.0.0.1:8001/api/runs/de3572bb68b442fd92e4ca8086cbea18/report` returned HTTP 200 with full Markdown report.
* **Frontend Server Test:**
  - Started frontend on port 3000 (`npm run start -- -p 3000`).
  - Checked all 7 page routes via curl (`/`, `/overview`, `/report`, `/claims`, `/evidence`, `/audit`, `/feedback`): all returned HTTP 200.

---

## 12. Security and Privacy Findings

| Issue ID | Severity | Category | Description | Affected Files | Evidence / Reproduction | Recommendation |
|---|---|---|---|---|---|---|
| **SEC-001** | **P2** | Network / SSRF | Unrestricted HTTP requests in PDF and Web extractors; no validation against internal IP addresses. | `research_crew/tools/pdf_extractor.py:79`, `research_crew/tools/web_parser.py:73` | Tools accept arbitrary URL strings and execute `requests.get()` without checking for private IP ranges (`127.0.0.1`, `169.254.169.254`, `10.0.0.0/8`). | Implement an IP and domain allow-list/block-list that rejects loopback, RFC 1918 private subnets, and cloud metadata endpoints. |
| **SEC-002** | **P2** | Authentication | Backend API has no authentication or authorization layer. | `backend/app.py` | All `/api/*` endpoints are unauthenticated. Anyone on the local network or reverse proxy can submit runs or cancel jobs. | Add bearer token authentication (`HTTPBearer` in FastAPI) or require API keys for all non-health routes. |
| **SEC-003** | **P3** | Secrets Exposure | Root `.env` file containing live API keys is present in the local workspace directory. | `~/DEV/multi-agent-research-system-2/.env` | Root `.env` contains `MESH_API_KEY`, `EXA_API_KEY`, and `TAVILY_API_KEY`. (It is ignored by `.gitignore`, but risks accidental exposure). | Ensure `.env` permissions are set to `600`; provide `.env.example` with blank keys; use external secret managers in production. |
| **SEC-004** | **P3** | Information Leakage | Missing `.env.example` at root directory while referenced in README. | `README.md:54` | README instructs users to `cp .env.example .env`, but file does not exist at root. | Create a clean `.env.example` at root documenting all required and optional configuration keys. |

---

## 13. Repository Provenance and Metadata Findings

During the Phase 7 provenance audit, the auditor discovered an archived git history directory: `.git-old-history/`.

### 13.1 Findings
1. **Previous Upstream Remote:**
   Inspection of `.git-old-history/config` reveals:
   ```ini
   [remote "upstream"]
       url = https://github.com/iamaayushijain/multi-agent-research-system-2.git
   ```
2. **Prior Root Commit & Author Metadata:**
   Running `git --git-dir=.git-old-history log --format="%h %an <%ae> %ad : %s"` identified the root commit:
   ```
   e6153e8 Aayushi Jain <iamaayushirocks@gmail.com> Sat Mar 14 11:07:18 2026 +0530 : final working project
   ```
3. **Previous Project Name:**
   The uncommitted working tree diff in `README.md` shows references to a previous directory name: `multi-agent-researcher-2`.
4. **Current Repository Status:**
   The active `.git/` repository has been squashed to a single commit:
   ```
   08fe813 Atharva Pagar <atharvapagar04@gmail.com> Mon Oct 19 13:09:21 2026 +0530 : Initial commit: multi-agent research citation engine
   ```
   Origin is configured as: `https://github.com/AtharvaPagar04/multi-agent-research-system-2.git`.

### 13.2 Remediation Recommendation
* The directory `.git-old-history` is untracked by the active git repository.
* When preparing a clean public release or final deployment archive, `.git-old-history/` should be archived securely outside the repository and removed from the active project root to prevent leaking previous author email addresses and upstream repository links.

---

## 14. Performance and Scalability

1. **Pipeline Latency:** The CrewAI pipeline runs 5 agents sequentially. A single research run takes between 25 and 90 seconds depending on LLM response time and PDF fetch speed.
2. **Subprocess Concurrency:** By default, `JOB_MAX_CONCURRENT` is set to `1` (`backend/config.py:61`). Research runs are strictly serialized. While this prevents CPU/memory exhaustion and API rate limits, it limits throughput to ~40–60 runs per hour.
3. **Frontend Synchronous Parsing Overhead:** In `frontend/src/lib/runs.ts`, `listResearchRuns()` iterates over every JSON file in `evidence_runs/` (109 files, ~8.7 MB) using `fs.readFileSync` and `JSON.parse`. As the number of research runs grows past a few hundred, home page rendering latency will degrade significantly unless pagination or index caching is introduced.

---

## 15. Deployment Readiness

### 15.1 Deployment Checklist

| Component | Status | Details / Deficiencies |
|---|---|---|
| **Dockerfile (Backend)** | **MISSING** | No Dockerfile present for backend or research engine. |
| **Dockerfile (Frontend)** | **MISSING** | No Dockerfile present for Next.js workspace. |
| **Docker Compose** | **MISSING** | No multi-container orchestration definition exists. |
| **CI/CD Pipelines** | **MISSING** | No `.github/workflows` or CI configuration files exist. |
| **Production Server Config** | **Partial** | Backend uses development Uvicorn invocation; needs Gunicorn process manager. |
| **Frontend Production Build** | **Ready** | `next build --turbopack` succeeds cleanly. |
| **Database Migrations** | **N/A** | File-based storage; no migration tooling required. |
| **Secret Management** | **Incomplete** | Relies on local `.env` file on host disk. |
| **Reverse Proxy / SSL** | **MISSING** | No Nginx or Caddy configuration provided. |

### 15.2 Deployment Blockers
1. **Port 8000 Conflict:** Cannot bind to 8000 on environments where system daemons run on port 8000.
2. **Shared Disk Architecture:** Frontend cannot be deployed independently of the backend artifact storage without mounting a shared persistent volume or implementing API endpoints for claims/evidence.
3. **No Containerization:** Neither tier can be deployed to standard cloud container registries (ECR, GCR, DockerHub) without creating Dockerfiles.

---

## 16. Missing Features

1. **API Endpoints for Run Details:** No REST routes for evidence records, claims verification, or audit findings.
2. **Persistent Job Store:** No persistent metadata store (e.g. SQLite) for job submission timestamps, durations, and exit logs.
3. **Pagination & Search in Historical Runs:** No server-side pagination for historical runs; the frontend parses all files on disk.
4. **Export Capabilities:** No PDF or LaTeX export feature for research reports (markdown only).
5. **SSRF Protection:** No domain/IP validation on URL fetchers.

---

## 17. Technical Debt

1. **Dual Virtual Environments:** Two virtual environments exist in root (`.venv` for Python 3.14 with missing packages; `.venv313` for Python 3.13 with installed packages).
2. **Missing Root `requirements.txt`:** `README.md` documents `pip install -r requirements.txt`, but requirements are split into `backend/requirements.txt` and `research_crew/requirements.txt`.
3. **CrewAI Deprecations:** CrewAI 1.15.27 raises 37 deprecation warnings regarding `function_calling_llm`, `allow_code_execution`, and `reasoning`.
4. **Pytest Import Path Fragility:** `research_crew/tests/` uses `from tests.conftest import ...`, requiring `PYTHONPATH=research_crew` to run.
5. **Node / npm Version Warning:** npm 12.2.1 emits warnings against Node.js v20.20.2.

---

## 18. Critical Bugs

### Bug BUG-001 (P0): Backend Port Collision with Existing Host Service
* **Description:** Default backend command `uvicorn backend.app:app --host 127.0.0.1 --port 8000` crashes on startup with `[Errno 98] address already in use`.
* **Affected Files:** `backend/app.py:8`, `frontend/src/lib/api.ts:15`, `frontend/.env.example:3`.
* **Evidence:** Process PID 3022 (`/home/bridge/.local/bin/gunicorn --bind 0.0.0.0:8000`) is active. Uvicorn exits with code 3.
* **Impact:** Application cannot be started following documented instructions.
* **Fix:** Change backend default port to `8001` or support configurable `PORT` environment variable across both backend and frontend.

### Bug BUG-002 (P1): Backend Health Check Fails to Load `.env`
* **Description:** `backend/app.py` never calls `load_dotenv()`.
* **Affected Files:** `backend/app.py:180-186`.
* **Evidence:** `curl http://127.0.0.1:8001/api/health` returns `{"engine": {"configured": false, "required_env_present": {"MESH_API_KEY": false, "EXA_API_KEY": false}}}` even when `.env` is populated.
* **Impact:** Backend reports itself as unconfigured, triggering UI warnings in the frontend console.
* **Fix:** Add `from dotenv import load_dotenv; load_dotenv()` at the top of `backend/app.py`.

### Bug BUG-003 (P1): Frontend Bypasses Backend API via Direct Local Disk Access
* **Description:** Server Components in `frontend/src/app/runs/[runId]/*` load data via `node:fs` instead of making HTTP requests to the backend.
* **Affected Files:** `frontend/src/lib/runs.ts`, `frontend/src/lib/fs-paths.ts`.
* **Evidence:** The backend has no endpoints for claims, evidence, audit, or feedback.
* **Impact:** Breaks distributed or containerized deployment where frontend and backend run in separate containers.
* **Fix:** Implement `/api/runs/{id}/claims`, `/api/runs/{id}/evidence`, `/api/runs/{id}/audit`, `/api/runs/{id}/feedback` in `backend/app.py` and update `frontend/src/lib/runs.ts` to fetch via HTTP client.

### Bug BUG-004 (P1): Research Crew Pytest Suite Fails Collection Without Explicit PYTHONPATH
* **Description:** Running `pytest research_crew/tests` fails during test collection with `ModuleNotFoundError: No module named 'tests'`.
* **Affected Files:** `research_crew/tests/test_model.py:22`, `research_crew/tests/test_store.py:15`, `research_crew/tests/test_validator.py:26`.
* **Evidence:** Test files use `from tests.conftest import ...` instead of relative imports.
* **Impact:** Standard test runners and CI/CD pipelines fail out-of-the-box.
* **Fix:** Change imports to `from research_crew.tests.conftest import ...` or `from .conftest import ...`.

---

## 19. Recommended Fixes

1. **Unify Configuration & Ports:**
   - Add `PORT` variable support in `backend/app.py` (default to `8001`).
   - Update `frontend/.env.example` and `frontend/src/lib/api.ts` default to `http://127.0.0.1:8001`.
   - Add `load_dotenv()` to `backend/app.py`.
2. **Create Root Dependency & Environment Files:**
   - Create root `requirements.txt` bundling `-r backend/requirements.txt` and `-r research_crew/requirements.txt`.
   - Create root `.env.example` documenting all configuration keys.
3. **Decouple Frontend via API Endpoints:**
   - Expose the 4 missing artifact endpoints in `backend/app.py`.
   - Update `frontend/src/lib/runs.ts` to call the typed API client rather than `node:fs`.
4. **Fix Test Imports:**
   - Refactor test imports in `research_crew/tests/` to use package-relative paths.
5. **Mitigate SSRF in Tools:**
   - Add URL validation in `PDFExtractorTool` and `WebParserTool` rejecting internal IP addresses.
6. **Containerize Application:**
   - Provide `backend/Dockerfile`, `frontend/Dockerfile`, and a root `docker-compose.yml`.

---

## 20. Final Completion Roadmap

```
Stage 1: Critical Fixes (Port, Env, Test Imports)
   │
   ▼
Stage 2: Core Persistence & API Decoupling (Missing Endpoints, SQLite Job State)
   │
   ▼
Stage 3: Frontend-to-API Integration (Remove node:fs dependency)
   │
   ▼
Stage 4: Security & Quality Hardening (SSRF mitigation, CrewAI deprecation cleanup)
   │
   ▼
Stage 5: Containerization & CI/CD (Dockerfiles, docker-compose, GitHub Actions)
   │
   ▼
Stage 6: Staging & Production Deployment
```

### Stage 1 — Critical Fixes (Estimated Effort: 1 Day)
* **Tasks:**
  1. Add `load_dotenv()` in `backend/app.py` to fix `/api/health`.
  2. Change default API port to `8001` to resolve host conflict.
  3. Create root `requirements.txt` and root `.env.example`.
  4. Fix `from tests.conftest` imports in `research_crew/tests/`.
* **Outcome:** Backend starts cleanly on port 8001; all tests pass without manual environment flags.

### Stage 2 — Core Persistence & API Decoupling (Estimated Effort: 2 Days)
* **Tasks:**
  1. Add FastAPI endpoints in `backend/app.py` for `/evidence`, `/claims`, `/audit`, `/feedback`.
  2. Add SQLite persistence for `JobManager` metadata so job logs and durations survive restarts.
* **Outcome:** Complete API coverage for all research artifact types; persistent execution logs.

### Stage 3 — Frontend-to-API Integration (Estimated Effort: 2 Days)
* **Tasks:**
  1. Refactor `frontend/src/lib/runs.ts` to fetch run details through the API client instead of `node:fs`.
  2. Keep local fallback only for standalone development mode.
* **Outcome:** Complete architectural decoupling; frontend can run in separate containers or on Vercel.

### Stage 4 — Security & Quality Hardening (Estimated Effort: 1.5 Days)
* **Tasks:**
  1. Implement IP range validation in `pdf_extractor.py` and `web_parser.py` (SSRF mitigation).
  2. Resolve CrewAI 1.15.27 deprecation warnings in agent task configs.
  3. Securely archive `.git-old-history/` outside the working tree.
* **Outcome:** Hardened network security; elimination of runtime deprecation warnings.

### Stage 5 — Containerization & CI/CD (Estimated Effort: 1.5 Days)
* **Tasks:**
  1. Author `backend/Dockerfile` using Python 3.13-slim and PyMuPDF dependencies.
  2. Author `frontend/Dockerfile` using Node 20-alpine multi-stage build.
  3. Create root `docker-compose.yml` defining `backend`, `frontend`, and shared volume for artifacts.
  4. Create GitHub Actions workflow (`.github/workflows/ci.yml`) running backend, engine, and frontend tests.
* **Outcome:** One-command reproducible local setup (`docker compose up --build`); automated CI verification.

### Stage 6 — Staging & Production Deployment (Estimated Effort: 1 Day)
* **Tasks:**
  1. Configure Nginx reverse proxy with SSL termination.
  2. Configure production process manager (Gunicorn with Uvicorn workers).
  3. Document production deployment runbook.
* **Outcome:** Production-ready multi-agent research system.

---

## Answers to the Five Core Questions

### 1. What is actually working right now?
* **Research Execution & Verification Engine (`research_crew`):** Decomposes topics, queries Exa/Tavily, scores sources, extracts content, generates cited markdown reports, decomposes claims into atomic propositions, checks factual entailment against 1,200-character source passages, detects missing comparison baselines, executes feedback loops, and persists artifacts with SHA-256 content hashes.
* **Subprocess Job Supervisor (`backend/jobs.py`):** Spawns `engine_runner.py` with process isolation, FIFO queuing, secret redaction, and graceful cancellation.
* **Frontend UI Components & Build:** All 7 Next.js routes render cleanly, Tailwind 4 styling works, TypeScript check and ESLint pass with 0 errors, static build succeeds, and artifact-backed historical browsing works locally.
* **Test Suites:** 84 backend tests, 390 engine tests, and 17 frontend tests (491 total) pass when properly configured.

### 2. What is implemented but not working correctly?
* **Default Backend Port Binding:** The documented command binds to port 8000, which crashes immediately due to a collision with an existing host gunicorn process.
* **Backend `/api/health` Configuration Status:** Reports required API keys as missing because `backend/app.py` fails to invoke `load_dotenv()`.
* **Frontend Architecture:** Run detail pages (`/overview`, `/claims`, `/evidence`, `/audit`, `/feedback`) are implemented as Server Components that read directly from the host filesystem (`node:fs`) rather than the backend API.
* **Pytest Discovery:** `research_crew/tests/` collection crashes unless `PYTHONPATH=research_crew` is manually supplied.

### 3. What is still missing?
* **Missing API Endpoints:** Endpoints for `/evidence`, `/claims`, `/audit`, and `/feedback` are absent from `backend/app.py`.
* **Missing Root Configuration Files:** Root `requirements.txt` and root `.env.example` are missing.
* **Missing Persistent Database:** `JobManager` job history is in-memory only and lost on server restart.
* **Missing Containerization & CI/CD:** No Dockerfiles, docker-compose, or CI/CD pipelines exist.
* **Missing SSRF Protection:** Extractor tools do not restrict private or cloud metadata IP addresses.

### 4. What are the biggest risks preventing successful deployment?
* **Network & Port Binding Failures:** Running on port 8000 causes immediate crash on machines with conflicting local daemons.
* **Deployment Decoupling Failure:** Deploying frontend and backend to separate servers/containers will cause all run detail views to return 404/Empty due to the `node:fs` disk dependency.
* **SSRF Vulnerability:** Unrestricted HTTP requests in web/PDF extraction tools pose risks if queries lead to internal private network endpoints.
* **Ephemeral Memory State:** In-flight or recent jobs are completely lost if the backend server restarts.

### 5. What is the exact recommended order of work to finish and deploy this project?
1. **Day 1:** Fix port binding (move to 8001), add `load_dotenv()` in `backend/app.py`, fix `test_*.py` imports, and create root `requirements.txt` and `.env.example`.
2. **Day 2–3:** Implement the 4 missing artifact endpoints in `backend/app.py` and add SQLite metadata persistence to `JobManager`.
3. **Day 4–5:** Update frontend `src/lib/runs.ts` to consume the new API endpoints instead of `node:fs`.
4. **Day 6:** Add SSRF validation to extraction tools and clean up legacy `.git-old-history/`.
5. **Day 7–8:** Build Dockerfiles, `docker-compose.yml`, and GitHub Actions CI workflow.
6. **Day 9–10:** Configure Nginx, verify staging deployment, and conduct production readiness test.
