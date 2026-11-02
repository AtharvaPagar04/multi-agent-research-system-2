# Stage 5A — Local Project Stabilization Audit Report

**Date:** 2026-11-01  
**Harness:** Antigravity  
**Primary Auditor Model:** Gemini 3.8 Flash  
**Repository:** `~/DEV/multi-agent-research-system-2`  
**Branch:** `main`  
**Target Environment:** Local Full-Stack (Next.js 15, FastAPI, CrewAI, PostgreSQL, Mesh API)  
**Final Verdict:** **PASS WITH ISSUES** (Operational and verified; minor file permission and documentation hardening recommended)

---

## Executive Summary

A comprehensive stabilization audit of the **Multi-Agent Research System** was conducted across configuration, security, backend reliability, research pipeline integrity, frontend UI state handling, and overall project quality.

The local system is **fully functional and operational end-to-end**:
- **Backend API & Persistence:** FastAPI service running on port 8001 with active PostgreSQL persistence (`research_jobs` and `research_runs` tables) and seamless historical artifact resolution from `evidence_runs/`.
- **Frontend Workspace:** Next.js 15 (React 19) web workspace running on port 3000, communicating exclusively via REST API endpoints (`/api/runs`, `/report`, `/claims`, `/evidence`, `/feedback`, `/audit`), with zero direct filesystem imports.
- **Research Engine:** Multi-agent pipeline (Planner, Search, Validator, Extractor, Synthesizer) powered by Mesh API (`gemini-3.5-flash-lite`) and Exa neural search, with bounded claim verification feedback loops and deterministic citation auditing.
- **Empirical Test Verification:** **516 / 516 automated tests passed** across all suites (98 backend tests, 391 research crew tests, 27 frontend tests).
- **OpenCode Removal:** Confirmed 100% clean removal; zero lingering OpenCode references remain in the repository.

No critical or high-severity vulnerabilities were found. A few medium- and low-severity documentation and configuration hardening items were identified.

---

## Comprehensive Domain Assessments

### 1. Configuration & Environment Assessment

| Check | Status | Verification Details |
|---|---|---|
| Environment Variable Loading | **PASS** | `python-dotenv` explicitly loaded in `backend/app.py:74`, `backend/config.py:22`, and `research_crew/main.py:46`. |
| `.env.example` Completeness | **PASS** | Contains clean templates for `MESH_API_KEY`, `MESH_MODEL`, `EXA_API_KEY`, `TAVILY_API_KEY`, `DATABASE_URL`, `BACKEND_PORT`, `PORT`, `FRONTEND_ORIGIN`, `RESEARCH_ARTIFACTS_DIR`, `CLAIM_VERIFICATION_POLICY`, and `MAX_RESEARCH_ROUNDS` without real credentials. |
| OpenCode Removal | **PASS** | Repository-wide case-insensitive regex search for `opencode` yielded **0 matches**. |
| Mesh API & Exa Configuration | **PASS** | Mesh API configured with endpoint `https://api.meshapi.ai/v1`, model `gemini-3.5-flash-lite`, and timeout/retry wrapper. Exa search configured via `EXA_API_KEY`. Verified via `/api/health`. |
| Default Ports & URLs | **PASS** | Backend configured to port 8001 (fallback if `PORT`/`BACKEND_PORT` unset); frontend configured to port 3000 with `DEFAULT_API_BASE_URL = "http://127.0.0.1:8001"`. |
| Missing Config Error Handling | **PASS** | Missing required keys (`MESH_API_KEY`, `EXA_API_KEY`) cause immediate, descriptive failure in `_check_env()` (`sys.exit(1)`) and runtime exceptions before pipeline execution. |

### 2. Security Assessment

| Check | Status | Verification Details |
|---|---|---|
| Hardcoded Credentials | **PASS** | Scanned tracked files: 0 real credentials found. Only mock keys (e.g. `dummy_offline_key`) in unit tests. |
| Accidental Secret Exposure | **PASS** | `_SecretRedactor` in `backend/jobs.py` automatically scrubs `MESH_API_KEY`, `EXA_API_KEY`, `TAVILY_API_KEY`, and `OPENAI_API_KEY` from all stdout, stderr, and snapshot payloads. |
| Health Endpoint Exposure | **PASS** | `/api/health` reports boolean presence (`true`/`false`) only, never secret strings. |
| CORS Configuration | **PASS** | `CORSMiddleware` strictly limited to `config.frontend_origins` (`http://localhost:3000`, `http://127.0.0.1:3000`), `allow_credentials=False`, and limited methods (`GET`, `POST`). |
| Input Validation | **PASS** | Topics bounded to non-empty trimmed strings with a strict cap of 500 characters (`MAX_TOPIC_LENGTH`). |
| Path Traversal Defense | **PASS** | All run and artifact endpoints strictly validate run IDs against `_RUN_ID_RE = ^[0-9a-f]{32}$`. Input containing `..` or path delimiters returns HTTP 404 / 422 immediately. |
| Subprocess Execution | **PASS** | Process spawning in `backend/jobs.py` uses argument vectors (`[python, "-m", "backend.engine_runner", ...]`) with `shell=False`, `stdin=subprocess.DEVNULL`, and bounded 64 KB stream buffers. |
| Local File Permissions | **ISSUE (Low)** | Local `.env` file created with mode `0644` (world-readable on multi-user systems). Should be hardened to `0600`. |

### 3. Backend Reliability & Persistence Assessment

| Check | Status | Verification Details |
|---|---|---|
| Job Lifecycle & Execution | **PASS** | Full state transition pipeline implemented: `QUEUED` → `STARTING` → `RUNNING` → `COMPLETED` / `FAILED` / `CANCELLED` / `INTERRUPTED`. |
| Concurrency & Queue Capacity | **PASS** | Strict FIFO single-worker processing (`max_concurrency = 1`), with configurable `queue_capacity` (default 10) rejecting overflows with HTTP 429. |
| PostgreSQL Persistence | **PASS** | `DatabaseManager` in `backend/db.py` persists submissions, timestamps, exit codes, failure diagnostics, and run ID links to `research_jobs` and `research_runs` tables. |
| Backend Restart Recovery | **PASS** | `JobManager.start()` calls `self._db.recover_stale_jobs()`, automatically transitioning unfinalized jobs (`queued`, `starting`, `running`) from interrupted instances to `interrupted`. |
| Process & Stream Isolation | **PASS** | Background worker communicates with runner via subprocess pipes; child termination is guarded by timeout (600s) and two-stage termination (SIGTERM, then SIGKILL after 5.0s grace period). |

### 4. Research Pipeline Assessment

| Check | Status | Verification Details |
|---|---|---|
| Agent & Task Connectivity | **PASS** | 5 agents (Planner, Search, Validator, Extractor, Synthesizer) linked sequentially via CrewAI tasks and `PipelineFlowCoordinator` callbacks. |
| Claim Verification & Feedback | **PASS** | Claims extracted and verified against captured evidence store; feedback loop executes bounded rounds (controlled by `MAX_RESEARCH_ROUNDS`) and falls back safely to standalone verification if interrupted. |
| Citation & Evidence Depth Audit | **PASS** | Deterministic citation audit validates all inline citations against the run's evidence store, calculating metrics, baseline disclosures, and thin-evidence warnings. |
| Historical Artifact Protection | **PASS** | Every run writes to isolated files tagged by run ID (`{run_id}.evidence.json`, `claim_verifications_{run_id}.json`, `feedback_loop_{run_id}.json`, `research_report_{run_id}.md`). New or failed runs cannot overwrite or corrupt existing historical artifacts. |
| API Call Bounding | **PASS** | Search is bounded per task; LLM calls wrapped with max 2 retries on empty responses; telemetry and tracing are disabled. |

### 5. Frontend UI & State Handling Assessment

| Check | Status | Verification Details |
|---|---|---|
| API Decoupling | **PASS** | 100% of research run data fetched via REST API client (`frontend/src/lib/api.ts`). `tests/api.test.mjs` verifies zero references to `node:fs` exist in `frontend/src`. |
| State Handling | **PASS** | Dedicated UI components and pages for Loading (`loading.tsx`), Error boundaries (`error.tsx`), Empty states, and Offline backend states. |
| Navigation & Run Inspection | **PASS** | Verified via live browser subagent across all tabs: Overview (`/overview`), Report (`/report`), Claims (`/claims`), Evidence (`/evidence`), Feedback (`/feedback`), and Audit (`/audit`). |
| Console & Network Health | **PASS** | Live browser navigation showed zero console errors or unhandled client-side exceptions. |

### 6. Project Quality & Dependencies Assessment

| Check | Status | Verification Details |
|---|---|---|
| TypeScript & Linter | **PASS** | `npm run typecheck` and `npm run lint` pass with **0 errors**. |
| Python Environment | **PASS** | Python 3.13 virtual environment with all dependencies resolved and functional. |
| Deprecation Warnings | **ISSUE (Low)** | 37 upstream deprecation warnings emitted by CrewAI library during test execution. |
| Documentation Consistency | **ISSUE (Medium)** | `README.md` step 1 instructs `pip install -r requirements.txt`, but dependencies are split into `research_crew/` and `backend/`. Also lacks backend and frontend startup instructions. |

---

## Detailed Findings Table

| ID | Severity | File & Location | What Is Wrong | Why It Matters | Recommended Fix | Status |
|:---:|:---:|:---|:---|:---|:---|:---:|
| **SEC-01** | **Low** | [`.env`](file:///home/arch/DEV/multi-agent-research-system-2/.env) | File permissions on disk are set to `0644` (`-rw-r--r--`). | World-readable to other unprivileged system users on a shared OS. | Run `chmod 600 .env` to restrict file access to the owner. | **Confirmed** |
| **DOC-01** | **Medium** | [`README.md:48`](file:///home/arch/DEV/multi-agent-research-system-2/README.md#L48) | Instructs `pip install -r requirements.txt`, but no root `requirements.txt` exists. | New developers following clone instructions encounter an immediate install failure. | Create a root `requirements.txt` chaining sub-requirements or update the README. | **Confirmed** |
| **DOC-02** | **Medium** | [`README.md:67-79`](file:///home/arch/DEV/multi-agent-research-system-2/README.md#L67-L79) | Missing instructions for launching FastAPI (`8001`), Next.js (`3000`), and PostgreSQL setup. | Developers cannot launch the full-stack system using only the README. | Add documented service launch commands for backend, frontend, and DB. | **Confirmed** |
| **DOC-03** | **Low** | [`README.md:94`](file:///home/arch/DEV/multi-agent-research-system-2/README.md#L94) | Typo in tree diagram: `plannsing_task.py`. | Minor documentation inaccuracy. | Fix spelling to `planning_task.py`. | **Confirmed** |
| **DOC-04** | **Low** | [`frontend/src/lib/api.ts:4-6`](file:///home/arch/DEV/multi-agent-research-system-2/frontend/src/lib/api.ts#L4-L6) | JSDoc header comment states default port is `8000`, while code constant is `8001`. | Minor developer confusion when reading code comments. | Update JSDoc comment to reflect `http://127.0.0.1:8001`. | **Confirmed** |
| **DEP-01** | **Low** | CrewAI library internals | Emits 37 `DeprecationWarning` notices during test execution. | Future CrewAI library updates may alter deprecated arguments. | Monitor upstream CrewAI updates; filter third-party warnings in `pytest.ini`. | **Confirmed** |
| **ARCH-01** | **Low** | [`backend/db.py:101-149`](file:///home/arch/DEV/multi-agent-research-system-2/backend/db.py#L101-L149) | DDL relies on `CREATE TABLE IF NOT EXISTS` without a migration tool (e.g. Alembic). | Future schema modifications cannot be versioned or rolled back systematically. | Introduce Alembic migrations for production database tracking. | **Confirmed** |

---

## Tests Actually Executed and Empirical Results

Every test listed below was directly executed during this audit session:

### 1. Backend Test Suite
- **Command:** `.venv313/bin/python -m pytest backend/tests/ -v`
- **Result:** **98 passed in 3.79s** (0 failed)
- **Coverage Summary:**
  - Artifact endpoints (`/claims`, `/evidence`, `/audit`, `/feedback`): 5 tests passed
  - Configuration & path resolution: 12 tests passed
  - PostgreSQL persistence & restart recovery: 6 tests passed
  - End-to-end integration & shutdown: 3 tests passed
  - Engine runner CLI & validation: 13 tests passed
  - JobManager concurrency, queueing, timeouts, redaction: 21 tests passed
  - REST API routes (`/api/runs`, `/report`, health, CORS): 38 tests passed

### 2. Research Crew Test Suite
- **Command:** `PYTHONPATH=research_crew .venv313/bin/python -m pytest research_crew/tests/`
- **Result:** **391 passed, 37 warnings in 19.06s** (0 failed)
- **Coverage Summary:**
  - Evidence adapters: 16 tests passed
  - Claim extraction & verification: 104 tests passed
  - Feedback loop execution & recovery: 37 tests passed
  - LLM reliability & retry interceptor: 7 tests passed
  - Data models & provenance: 23 tests passed
  - Pipeline flow coordination: 10 tests passed
  - Report evidence citation audit: 143 tests passed
  - Search tools (Exa & Tavily): 10 tests passed
  - EvidenceStore atomicity & file locking: 25 tests passed
  - Validator passage integrity: 16 tests passed

### 3. Frontend Test Suite & Static Analysis
- **Command 1:** `npm test` (in `frontend/`)
  - **Result:** **27 passed in 92.9ms** (0 failed)
  - **Coverage Summary:** Topic validation, API client normalization, 404/422/429/503 handling, historical run resolution, and **verification that zero `node:fs` calls exist in frontend code**.
- **Command 2:** `npm run typecheck` (in `frontend/`)
  - **Result:** Exited with code 0 (TypeScript clean).
- **Command 3:** `npm run lint` (in `frontend/`)
  - **Result:** Exited with code 0 (ESLint clean).

### 4. Live Service Health Checks
- **Command:** `curl -s http://127.0.0.1:8001/api/health`
  - **Result:**
    ```json
    {
      "status": "ok",
      "engine": {
        "configured": true,
        "checks": {
          "repo_root_found": true,
          "engine_module_found": true,
          "runner_found": true,
          "artifacts_dir_found": true
        },
        "required_env_present": {
          "MESH_API_KEY": true,
          "EXA_API_KEY": true
        },
        "optional_env_present": {
          "TAVILY_API_KEY": false
        }
      }
    }
    ```
- **Live Browser Navigation:** Verified via automated browser inspection at `http://127.0.0.1:3000`:
  - Main workspace loaded cleanly.
  - Historical run selection (`14fa200c...`) inspected across all 6 tabs (Overview, Report, Claims, Evidence, Feedback, Audit).
  - Browser console checked: **0 errors**.

---

## Prioritized List of Recommended Fixes

### Priority 1: Security & File Permissions
1. **Restrict `.env` file permissions:**
   ```bash
   chmod 600 .env
   ```

### Priority 2: Documentation & Developer Onboarding
2. **Create root `requirements.txt`:**
   Add a root `requirements.txt` containing:
   ```text
   -r backend/requirements.txt
   -r research_crew/requirements.txt
   ```
3. **Update `README.md`:**
   - Add full-stack startup commands for FastAPI (`uvicorn backend.app:app --host 127.0.0.1 --port 8001`) and Next.js (`npm run dev -p 3000`).
   - Document PostgreSQL configuration requirement (`DATABASE_URL`).
   - Fix project tree typo (`planning_task.py`) and replace outdated OpenAI references.
4. **Update comment in `frontend/src/lib/api.ts`:**
   - Correct comment from port `8000` to `8001`.

### Priority 3: Future Architectural Maintenance (Post-Delivery)
5. **Add Alembic migrations:** Implement formal schema migration scripts for the PostgreSQL database tables.
6. **Filter third-party deprecation warnings:** Add warning filter rules in `pytest.ini` for CrewAI internals.

---

## Final Verdict

### **PASS WITH ISSUES**

The system is fully functional, robustly tested (516/516 passing tests), stable, and meets all core engineering and security requirements. The identified issues are non-breaking documentation and local file permission hardening items that can be quickly addressed without code architecture changes.
