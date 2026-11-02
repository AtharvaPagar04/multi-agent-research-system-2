# Stage 5B — Targeted Stabilization Implementation Report

**Date:** 2026-11-02  
**Harness:** Antigravity  
**Model:** Gemini 3.8 Flash  
**Repository:** `~/DEV/multi-agent-research-system-2`  
**Branch:** `main`  
**Final Verdict:** **PASS** (All targeted stabilization fixes implemented, verified, and regression-free)

---

## 1. Executive Summary

During Stage 5B, all low- and medium-priority findings identified in the Stage 5A Local Stabilization Audit were systematically addressed. The project remains fully functional across all three tiers (FastAPI backend, Next.js 15 frontend, and CrewAI research engine), with **516 / 516 automated tests passing**, zero type or lint errors, a clean production build, and strict security hardening on local secrets.

No architectural changes, feature additions, or regressions were introduced. Historical research artifacts remain intact.

---

## 2. Stage 5A Findings Reviewed & Resolved

| Finding ID | Severity | Location | Issue Description | Stage 5B Resolution | Status |
|:---:|:---:|:---|:---|:---|:---:|
| **SEC-01** | **Low** | [`.env`](file:///home/arch/DEV/multi-agent-research-system-2/.env) | World-readable file permissions (`0644`). | Changed mode to `0600` (`-rw-------`) via `chmod 600 .env`. Verified access is restricted to the file owner only. | **RESOLVED** |
| **DOC-01** | **Medium** | [`README.md`](file:///home/arch/DEV/multi-agent-research-system-2/README.md) | Instructed `pip install -r requirements.txt`, but no root-level file existed. | Updated README installation commands to target the actual project structure: `pip install -r research_crew/requirements.txt` and `pip install -r backend/requirements.txt`. | **RESOLVED** |
| **DOC-02** | **Medium** | [`README.md`](file:///home/arch/DEV/multi-agent-research-system-2/README.md) | Lacked startup documentation for FastAPI (`8001`), Next.js (`3000`), and PostgreSQL. | Added a comprehensive, reproducible Arch Linux local setup guide documenting system packages, venv setup, PostgreSQL commands, service startups on ports 8001 and 3000, and health-checks. | **RESOLVED** |
| **DOC-03** | **Low** | [`README.md`](file:///home/arch/DEV/multi-agent-research-system-2/README.md) | Typo in tree diagram: `plannsing_task.py`. | Corrected to actual filename: `planning_task.py`. | **RESOLVED** |
| **DOC-04** | **Low** | [`frontend/src/lib/api.ts`](file:///home/arch/DEV/multi-agent-research-system-2/frontend/src/lib/api.ts) | JSDoc comment cited port `8000` instead of `8001`. | Corrected JSDoc comment to `http://127.0.0.1:8001`. Also updated `frontend/.env.example` and `backend/app.py` docstrings. | **RESOLVED** |
| **DEP-01** | **Low** | Upstream CrewAI | 37 `DeprecationWarning` notices during test runs. | Investigated root cause: 100% upstream Pydantic attribute access within CrewAI 1.15.x. No project code uses deprecated fields. Documented for future dependency maintenance. | **INVESTIGATED & DOCUMENTED** |
| **ARCH-01** | **Low** | [`backend/db.py`](file:///home/arch/DEV/multi-agent-research-system-2/backend/db.py) | No Alembic migration tooling for schema changes. | Documented as deferred production-hardening work per Stage 5B scope constraint; existing PostgreSQL DDL preserved. | **DEFERRED (DOCUMENTED)** |

---

## 3. Files Modified

1. [`.env`](file:///home/arch/DEV/multi-agent-research-system-2/.env): Changed file mode permissions from `0644` to `0600`.
2. [`README.md`](file:///home/arch/DEV/multi-agent-research-system-2/README.md):
   - Added complete Arch Linux full-stack setup guide (prerequisites, venv, PostgreSQL init, `.env` config, service launch, health check, teardown, and troubleshooting).
   - Documented explicit dependency installation for `research_crew/requirements.txt` and `backend/requirements.txt`.
   - Corrected typo `plannsing_task.py` → `planning_task.py`.
   - Replaced legacy OpenAI reference with Mesh API documentation.
3. [`frontend/src/lib/api.ts`](file:///home/arch/DEV/multi-agent-research-system-2/frontend/src/lib/api.ts):
   - Corrected JSDoc comment from `http://127.0.0.1:8000` to `http://127.0.0.1:8001`.
4. [`frontend/.env.example`](file:///home/arch/DEV/multi-agent-research-system-2/frontend/.env.example):
   - Updated default backend URL from port `8000` to `http://127.0.0.1:8001`.
5. [`backend/app.py`](file:///home/arch/DEV/multi-agent-research-system-2/backend/app.py):
   - Updated module docstring uvicorn example command from `--port 8000` to `--port 8001`.

---

## 4. Commands Executed & Empirical Results

### A. Environment & Security Checks
- **Command:** `chmod 600 .env && stat -c "%a %n" .env`
  - **Result:** `600 .env` (Verified: Owner read/write only).
- **Command:** `git check-ignore -v .env`
  - **Result:** `.gitignore:2:.env .env` (Verified: Ignored by Git).
- **Command:** `git grep -E -i "(sk-[a-zA-Z0-9]{20,}|key\s*=\s*['\"][a-zA-Z0-9_-]{20,}['\"])"`
  - **Result:** 0 matches in tracked files (Verified: No secrets committed).
- **Command:** `git status -- evidence_runs/`
  - **Result:** Working tree clean (Verified: Historical artifacts untouched).

### B. Backend Verification
- **Command:** `.venv313/bin/python -m pytest backend/tests/ -v`
  - **Result:** **98 passed in 3.85s** (100% pass rate).
- **Command:** `curl -s http://127.0.0.1:8001/api/health | jq .`
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
- **Command:** `.venv313/bin/python -c "from backend.db import DatabaseManager; db = DatabaseManager(); print('PostgreSQL connected:', db.health_check())"`
  - **Result:** `PostgreSQL connected: True`.

### C. Research Engine Verification
- **Command:** `.venv313/bin/python -c "import research_crew.main; from research_crew.main import run_research_pipeline; print('Import OK')"`
  - **Result:** `Import OK`.
- **Command:** `PYTHONPATH=research_crew .venv313/bin/python -m pytest research_crew/tests/`
  - **Result:** **391 passed, 37 warnings in 18.85s** (100% pass rate).

### D. Frontend Verification
- **Command:** `npm test` (in `frontend/`)
  - **Result:** **27 passed in 80.2ms** (100% pass rate, zero `node:fs` calls verified).
- **Command:** `npm run typecheck` (in `frontend/`)
  - **Result:** Code 0 (TypeScript clean).
- **Command:** `npm run lint` (in `frontend/`)
  - **Result:** Code 0 (ESLint clean).
- **Command:** `npm run build` (in `frontend/`)
  - **Result:** Code 0 (Optimized Turbopack production build compiled successfully).

### E. Regression & Tab Health Checks
- **Command:** 
  ```bash
  for tab in overview report claims evidence feedback audit; do
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:3000/runs/f50eb7886a0b432781b7853bc92d87ef/$tab")
    echo "Tab $tab: $code"
  done
  ```
  - **Result:**
    - Tab overview: `200`
    - Tab report: `200`
    - Tab claims: `200`
    - Tab evidence: `200`
    - Tab feedback: `200`
    - Tab audit: `200`

---

## 5. Investigation of Deprecation Warnings (DEP-01)

The Stage 5A audit identified 37 deprecation warnings during the execution of `research_crew/tests/`. A deep-dive code inspection revealed the following:

- **Specific Warning Sources:**
  - `crewai/agent/core.py:425`: `function_calling_llm is deprecated and will be removed in a future release.` (12 occurrences)
  - `crewai/agent/core.py:433`: `allow_code_execution is deprecated and will be removed in v2.0.` (12 occurrences)
  - `crewai/agent/core.py:444`: `The 'reasoning' parameter is deprecated. Use 'planning_config=PlanningConfig()' instead.` (12 occurrences)
  - `crewai/crew.py:650`: `function_calling_llm is deprecated and will be removed in a future release.` (1 occurrence)
- **Root Cause:**
  - In `crewai` 1.15.x, the `Agent` and `Crew` classes inherit from Pydantic `BaseModel` and decorate these legacy fields with `typing.Annotated[..., deprecated="..."]`.
  - During standard instantiation of `Agent(...)` and `Crew(...)`, CrewAI's own internal initialization routines check `self.function_calling_llm`, `self.allow_code_execution`, and `self.reasoning` to set default internal executor state.
  - This internal attribute access immediately triggers Pydantic's deprecation hook.
- **Repository Code State:**
  - Neither `research_crew/agents/*.py`, `research_crew/main.py`, nor any tests in `research_crew/tests/` specify, pass, or rely on `function_calling_llm`, `allow_code_execution`, or `reasoning`.
- **Action & Recommendation:**
  - Per the user guidelines, warnings are not globally suppressed, and major dependencies are not bumped prematurely.
  - When CrewAI v2.0 is released, monitor their updated `Agent` API for changes in tool-calling mechanics.

---

## 6. Deferred Work

### Alembic Database Migrations (ARCH-01)
- In accordance with the Stage 5B scope boundary, Alembic was not introduced at this stage.
- The existing `CREATE TABLE IF NOT EXISTS` implementation in [`backend/db.py`](file:///home/arch/DEV/multi-agent-research-system-2/backend/db.py) remains lightweight, stable, idempotent, and verified by 6 database unit tests.
- Transitioning to Alembic migrations is deferred to future production deployment milestones.

---

## 7. Results Classification

- **Verified Results (Empirically Executed & Observed):**
  - `.env` file permissions set to `600`.
  - Backend tests: 98/98 passed.
  - Research engine tests: 391/391 passed.
  - Frontend unit tests: 27/27 passed.
  - Total automated tests: 516/516 passed.
  - TypeScript typecheck, ESLint, and Next.js build all passed with 0 errors.
  - Backend port 8001 and frontend port 3000 verified responding with HTTP 200 across all 6 run tabs.
  - PostgreSQL connectivity and schema health verified.
- **Agent-Reported Context:**
  - Network timeout encountered during the optional browser subagent cloud recording step did not affect local server health (both local servers verified independently via curl).
- **Unverified Assumptions:**
  - None. All reported assertions are backed by direct shell commands and logged outputs.

---

## 8. Final Verdict

### **PASS**

All targeted stabilization fixes from Stage 5A have been completed:
1. `.env` permissions hardened to `600`.
2. README dependency instructions corrected to match existing repository structure.
3. Complete Arch Linux full-stack setup guide documented in README.
4. Project tree typo (`plannsing_task.py`) corrected.
5. All outdated references to port 8000 updated to 8001.
6. CrewAI deprecation warnings investigated, pinpointed to upstream library code, and documented.
7. Alembic migration decision documented as deferred production hardening.
8. 100% of the 516 automated tests passed without regression.
