# Multi-Agent Research Citation Engine

A production-grade AI research assistant built with **FastAPI**, **Next.js 15**, and **CrewAI** in Python.  
Enter any research topic and receive a structured report with accurate citations, extracted evidence, and verified references — similar to Perplexity Deep Research or Elicit, but fully open, auditable, and locally verifiable.

---

## What It Does

1. **Planner Agent** decomposes your research topic into 4–6 targeted search queries.
2. **Search Agent** retrieves up to 8 sources per query from arXiv, IEEE, ACL, GitHub, and official documentation via Exa and Tavily APIs.
3. **Validator Agent** scores every source (1–10) on credibility, recency, and technical depth — keeping only the top 5 highest-quality sources.
4. **Extractor Agent** fetches each source (PDF or webpage), chunks the text, and extracts metrics, datasets, findings, and verbatim quotes.
5. **Synthesizer Agent** merges all evidence into a structured Markdown report with inline citations grounded in the local evidence store.
6. **Claim Verification & Feedback Loop** extracts factual assertions and verifies entailment against captured source text, triggering bounded supplemental research rounds if evidence is lacking.
7. **Deterministic Evidence Citation Audit** verifies that 100% of cited source URLs exist in the run's evidence store, flagging thin evidence or unverified assertions.

---

## Full-Stack Architecture

```
┌────────────────────────────────────────────────────────┐
│                   Next.js 15 Frontend                  │
│                (http://127.0.0.1:3000)                 │
└───────────────────────────┬────────────────────────────┘
                            │ REST API
┌───────────────────────────▼────────────────────────────┐
│                  FastAPI Backend Service               │
│                (http://127.0.0.1:8001)                 │
│  - Job lifecycle & queueing (JobManager)               │
│  - Child process runner (backend/engine_runner.py)     │
│  - PostgreSQL persistence (research_jobs & runs)       │
└───────────────────────────┬────────────────────────────┘
                            │ Subprocess Execution
┌───────────────────────────▼────────────────────────────┐
│                 CrewAI Research Engine                 │
│  User Topic                                            │
│      ↓                                                 │
│  Planner Agent      → { "queries": [...] }             │
│      ↓                                                 │
│  Search Agent       → [ { title, url, snippet, ... } ] │
│      ↓                                                 │
│  Validator Agent    → { "validated_sources": [...] }   │
│      ↓                                                 │
│  Extractor Agent    → [ { metrics, datasets, ... } ]   │
│      ↓                                                 │
│  Synthesizer Agent  → Final Markdown Report            │
│      ↓                                                 │
│  Feedback Loop      → Bounded Verification Rounds      │
│      ↓                                                 │
│  Evidence Store     → evidence_runs/<run_id>.*.json    │
└────────────────────────────────────────────────────────┘
```

All inter-agent communication uses **structured JSON**; artifacts are stored deterministically in `evidence_runs/` and tracked in PostgreSQL.

---

## Local Setup & Quick Start (Arch Linux)

Follow these step-by-step instructions for a clean, reproducible setup on Arch Linux (or other Linux distributions).

### 1. Prerequisites

Ensure system packages are installed:

```bash
sudo pacman -S --needed postgresql nodejs npm git uv
```

- **Python:** **Python 3.13 is strictly required** (see note below on Python 3.14 incompatibility).
- **Node.js:** ≥ 20.x
- **PostgreSQL:** Active local instance (≥ 15)

> [!IMPORTANT]
> **Python 3.14 Incompatibility Notice:**
> Modern rolling-release distributions like Arch Linux package Python 3.14 as the default system interpreter (`/usr/bin/python`). However, Python 3.14 is **not currently supported** by this project because core research dependencies—including `crewai`, `chromadb`, `pymupdf` (fitz), `pydantic-core`, `lxml`, and `tiktoken`—rely on C-extensions and pre-compiled binary wheels that are not yet built for Python 3.14. Attempting to install dependencies under Python 3.14 will result in compilation and wheel build errors.
>
> Always use **Python 3.13** to create your virtual environment.

### 2. Python 3.13 Virtual Environment Setup

Clone the repository and create a Python 3.13 virtual environment using `uv` (recommended on Arch Linux) or standalone `python3.13`:

```bash
git clone https://github.com/AtharvaPagar04/multi-agent-research-system-2.git
cd multi-agent-research-system-2

# Option A: Fast setup via uv (recommended on Arch Linux)
uv venv --python 3.13 .venv
source .venv/bin/activate

# Option B: Using AUR package python313 (yay -S python313)
# python3.13 -m venv .venv
# source .venv/bin/activate
```

Verify your active environment is running Python 3.13:

```bash
python --version
# Output MUST be Python 3.13.x (e.g., Python 3.13.16)
```

### 3. Dependency Installation

The application partitions dependencies into the research pipeline and backend API tiers. Install both using the existing requirement specifications:

```bash
# Research engine dependencies (CrewAI, LangChain, Exa, PyMuPDF, etc.)
pip install -r research_crew/requirements.txt

# Backend API dependencies (FastAPI, Uvicorn, psycopg, python-dotenv)
pip install -r backend/requirements.txt
```

### 4. PostgreSQL Setup & Database Configuration

Initialize and start the local PostgreSQL service:

```bash
# Start and enable PostgreSQL service
sudo systemctl enable --now postgresql

# Create the application database (if not already created)
sudo -u postgres psql -c "CREATE DATABASE multi_agent_research;"
```

*Note: If this is a fresh PostgreSQL installation on Arch Linux, run `sudo -u postgres initdb --locale=C.UTF-8 -E UTF8 -D /var/lib/postgres/data` before starting the service.*

### 5. Environment Variable Configuration

Copy the template configuration to `.env` and set file permissions to `600` (owner read/write only):

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` to configure your API keys and database connection:

```ini
# LLM Gateway
MESH_API_KEY=your_mesh_api_key_here
MESH_MODEL=gemini-3.5-flash-lite
LLM_TEMPERATURE=0.4

# Search APIs
EXA_API_KEY=your_exa_api_key_here
TAVILY_API_KEY=your_optional_tavily_key_here

# PostgreSQL Persistence
DATABASE_URL=postgresql://postgres@127.0.0.1:5432/multi_agent_research

# Ports & Origins
BACKEND_PORT=8001
PORT=8001
FRONTEND_ORIGIN=http://localhost:3000,http://127.0.0.1:3000

# Research Artifacts & Feedback Loop
RESEARCH_ARTIFACTS_DIR=./evidence_runs
EVIDENCE_DIR=./evidence_runs
CLAIM_VERIFICATION_POLICY=label
MAX_RESEARCH_ROUNDS=2
```

### 6. Start Backend Service (Port 8001)

Start the FastAPI application in a dedicated terminal:

```bash
source .venv/bin/activate
uvicorn backend.app:app --host 127.0.0.1 --port 8001
```

The backend automatically initializes required PostgreSQL tables (`research_jobs`, `research_runs`) and recovers interrupted runs on startup.

### 7. Frontend Dependency Installation & Startup (Port 3000)

In a separate terminal, install frontend dependencies and launch the Next.js development server:

```bash
cd frontend
npm install
npm run dev -- -p 3000
```

Open your browser at `http://127.0.0.1:3000` to access the research workspace.

### 8. Health Check Command

Verify that the backend is fully configured and ready:

```bash
curl -s http://127.0.0.1:8001/api/health | jq .
```

Expected output:
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

### 9. Stopping the Services

- **Foreground terminals:** Press `Ctrl + C` in each terminal window.
- **Background processes:** Gracefully terminate using `pkill`:
  ```bash
  pkill -f "uvicorn backend.app:app"
  pkill -f "next dev"
  ```

---

## Running Tests

A root-level `pytest.ini` is pre-configured so that backend and research crew test suites can be executed directly without manual `PYTHONPATH` exports:

```bash
# Activate your Python 3.13 environment
source .venv/bin/activate

# Run all Python tests (489 tests across backend and research_crew)
pytest

# Or run suites individually
pytest backend/tests
pytest research_crew/tests

# Run frontend test suite (27 tests)
npm --prefix frontend test
```

---

## Standalone CLI Mode (Optional)

You can also run the research engine directly from the command line without the web server:

```bash
# Interactive mode
python -m research_crew.main

# Specific topic
python -m research_crew.main --topic "attention mechanisms in transformers"

# Save custom output
python -m research_crew.main --topic "diffusion models" --output diffusion.md
```

---

## Common Setup Errors & Solutions

| Error | Root Cause | Solution |
|---|---|---|
| `Building wheel for ... failed` or `ModuleNotFoundError` on install | Running on Python 3.14 where binary wheels do not yet exist. | Install and use Python 3.13: `uv venv --python 3.13 .venv` and activate it. |
| `[Errno 98] address already in use: ('127.0.0.1', 8000)` | A host daemon is occupying port 8000. | The backend is configured to use port **8001** by default. Ensure you run `--port 8001`. |
| `psycopg.OperationalError: connection failed` | PostgreSQL service is inactive or database does not exist. | Run `sudo systemctl start postgresql` and verify `DATABASE_URL` in `.env`. |
| `RuntimeError: MESH_API_KEY is required` | `MESH_API_KEY` is missing from the environment. | Ensure `.env` exists in the repository root and contains a valid key from [Mesh API](https://api.meshapi.ai). |
| `Missing required environment variables: EXA_API_KEY` | `EXA_API_KEY` is unset. | Set `EXA_API_KEY` in `.env` (obtainable from [Exa](https://exa.ai)). |
| Frontend displays `Backend offline` | Backend is not running on port 8001. | Verify backend service with `curl http://127.0.0.1:8001/api/health`. |

---

## Project Structure

```
multi-agent-research-system-2/
├── backend/
│   ├── app.py                 # FastAPI REST API endpoints
│   ├── config.py              # Centralized backend configuration & ports
│   ├── db.py                  # PostgreSQL persistence layer (psycopg)
│   ├── engine_runner.py       # Isolated child subprocess wrapper
│   ├── jobs.py                # JobManager, FIFO queue, and process supervisor
│   ├── requirements.txt       # Backend dependencies (FastAPI, psycopg, etc.)
│   └── tests/                 # Backend test suite (98 tests)
├── frontend/
│   ├── src/
│   │   ├── app/               # Next.js 15 App Router pages & tabs
│   │   ├── components/        # UI components (evidence, claims, audit)
│   │   └── lib/
│   │       ├── api.ts         # Backend REST API client (port 8001 default)
│   │       ├── runs.ts        # Run state normalization
│   │       └── types.ts       # Shared TypeScript type definitions
│   ├── package.json           # Next.js, React 19, Tailwind CSS
│   └── tests/                 # Frontend tests (node --test)
├── research_crew/
│   ├── agents/
│   │   ├── planner_agent.py   # Research Strategist
│   │   ├── search_agent.py    # Academic Source Finder
│   │   ├── validator_agent.py # Source Quality Evaluator
│   │   ├── extractor_agent.py # Technical Evidence Extractor
│   │   └── synthesizer_agent.py # Research Writer
│   ├── tasks/
│   │   ├── planning_task.py   # Query decomposition task
│   │   ├── search_task.py     # Source retrieval task
│   │   ├── validation_task.py # Source scoring & filtering task
│   │   ├── extraction_task.py # Evidence extraction task
│   │   └── summary_task.py    # Final report generation task
│   ├── tools/
│   │   ├── search_tool.py     # Exa + Tavily search tools
│   │   ├── pdf_extractor.py   # PyMuPDF PDF text extractor
│   │   └── web_parser.py      # BeautifulSoup webpage parser
│   ├── feedback_loop.py       # Bounded verification-to-research feedback loop
│   ├── pipeline_flow.py       # Flow coordinator & tool interceptor
│   ├── requirements.txt       # Research engine dependencies
│   ├── main.py                # Pipeline runner & Mesh API LLM factory
│   └── tests/                 # Research crew test suite (391 tests)
├── evidence_runs/             # Deterministic run artifacts (*.evidence.json, *.md)
├── .env.example               # Configuration template without real secrets
├── .gitignore                 # Exclusion rules (.env, evidence_runs, venv)
└── README.md                  # This file
```

---

## Token Safety & Rate Limits

The system enforces strict safeguards at every layer:

| Layer | Limit | Mechanism |
|---|---|---|
| Document download | 10 MB | Streaming cap in `pdf_extractor.py` |
| Extracted text per source | 3 000 chars | Hard truncation in tools |
| Text chunks | 800 tokens | `chunk_text()` in `text_chunker.py` |
| Evidence per source | 300 tokens | Agent instruction + task constraint |
| LLM calls | Retried on 429 | `tenacity` exponential backoff + MeshDiagnosticInterceptor |
| Process output buffers | 64 KB | `_BoundedBuffer` prevents memory exhaustion |

---

## Requirements Summary

- **OS:** Linux (Arch Linux, Ubuntu, Debian, etc.)
- **Python:** 3.13 strictly (Python 3.14 is currently unsupported by upstream CrewAI/wheel ecosystem)
- **Node.js:** ≥ 20.x
- **PostgreSQL:** ≥ 15
- **Mesh API Key:** Required for LLM completions ([api.meshapi.ai](https://api.meshapi.ai))
- **Exa API Key:** Required for neural search ([exa.ai](https://exa.ai))
- **Tavily API Key:** Optional fallback search ([tavily.com](https://tavily.com))
