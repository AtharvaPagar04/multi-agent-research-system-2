# Stage 6D — Fresh-Clone Test Reliability Report

**Harness:** Antigravity  
**Model:** Gemini 3.8 Flash  
**Repository:** `~/DEV/multi-agent-research-system-2`  
**Branch:** `release/stage-6-remediation`  
**Execution Date:** 2026-11-02  
**Final Status:** **PASS**

---

## 1. Executive Summary

Stage 6D resolved the single failing test identified during Stage 6C fresh-clone verification (`research_crew/tests/test_feedback_loop.py::test_task14_resolution_audit_serialization_backward_compatible`).

By replacing an unmocked dependency on an untracked local disk artifact with a deterministic, self-contained test fixture and optional disk validation, the test now executes reliably across both fresh clones and active development repositories.

### Key Results
- **Python Test Suite (Repository Root):** **489/489 passed** (100% pass rate in 22.56s).
- **Python Test Suite (Fresh Clone Workspace):** **488 passed, 1 skipped, 0 failed** (100% pass rate, zero failures).
- **Frontend Regression Suite:** **27/27 passed**; TypeScript, ESLint, and Next.js 15.5.28 production build passed with zero errors.
- **Git State:** 1 focused commit on `release/stage-6-remediation`; `main` untouched; working tree clean.

---

## 2. Root Cause Analysis

### The Failure
```text
FAILED research_crew/tests/test_feedback_loop.py::test_task14_resolution_audit_serialization_backward_compatible
FileNotFoundError: [Errno 2] No such file or directory: 'evidence_runs/feedback_loop_de3572bb68b442fd92e4ca8086cbea18.json'
```

### Technical Root Cause
The test `test_task14_resolution_audit_serialization_backward_compatible` was written to verify that serialization of `ClaimResolutionAudit` preserves the 7 historical dictionary keys while adding the new nullable `recheck_claim_id` key (introduced in Task 14).

However, the test attempted to directly load a historical run file from the filesystem:
```python
historical = json.loads(
    (Path("evidence_runs") / "feedback_loop_de3572bb68b442fd92e4ca8086cbea18.json").read_text()
)
```

Because `evidence_runs/` is intentionally excluded from version control via `.gitignore`, this directory and its contents are absent in a clean git clone. Other tests in `backend/tests/` that reference `evidence_runs/` protect against this via `if not path.exists(): pytest.skip(...)`. Furthermore, inspection of the historical artifact revealed that its `targeted_claim_resolutions` list was actually empty (`[]`), causing the original test to fall back to an internal key calculation anyway.

---

## 3. Test Strategy & Files Changed

### Files Changed
* `research_crew/tests/test_feedback_loop.py` (lines 1680–1706)

### Strategy
1. **Self-Contained Fixture:** Defined a deterministic schema constant representing the 7 historical pre-Task-14 keys of `ClaimResolutionAudit`:
   ```python
   historical_shape_keys = {
       "claim_id",
       "claim_text",
       "gained_relevant_evidence",
       "evidence_id",
       "source_url",
       "verdict_after_recheck",
       "notes",
   }
   assert historical_shape_keys == set(dumped.keys()) - {"recheck_claim_id"}
   ```
2. **Backward-Compatibility Preservation:** Preserved all original test assertions, including:
   - Full 8-key dictionary structure verification.
   - Exact value assertions for `recheck_claim_id` and `claim_id`.
   - Nullable handling for unmatched claims (`None` values).
3. **Optional Disk Validation:** Maintained the historical disk file inspection guarded by `if historical_path.exists():`, ensuring that developer environments with populated evidence runs still execute the live schema cross-check without failing clean environments.
4. **Zero Production Changes:** No production code or runtime behaviors in `feedback_loop.py` were modified.

---

## 4. Test Results: Before vs. After

### Python Suite Comparison

| Test Environment | Metric | Before Fix (Stage 6C) | After Fix (Stage 6D) | Status |
|:---|:---|:---:|:---:|:---:|
| **Main Repository** (`.venv313`) | Collected | 489 | 489 | — |
| | Passed | 489 | **489** | **PASS** |
| | Failed | 0 | **0** | **PASS** |
| | Skipped | 0 | **0** | — |
| | Runtime | 22.50s | 22.56s | — |
| **Fresh Clone** (`.venv`) | Collected | 489 | 489 | — |
| | Passed | 485 | **488** | **+3 tests** |
| | Failed | 1 | **0** | **FIXED** |
| | Skipped | 3 | **1** | Legitimate skip |
| | Runtime | 22.65s | 22.38s | — |

*Note on Skipped Test in Fresh Clone:* The single remaining skip is in `test_artifact_endpoints.py:229` (`Historical artifact de3572bb68b442fd92e4ca8086cbea18 not found on disk`), which explicitly and cleanly skips retrieval of uncommitted historical evidence in clean environments.

---

## 5. Frontend Regression Verification

All frontend validation suites were executed from `frontend/` to confirm zero regression:

| Check | Command | Result | Notes |
|:---|:---|:---:|:---|
| **Frontend Tests** | `npm test` | **PASS (27/27)** | Completed in 79.2ms |
| **Typecheck** | `npx tsc --noEmit` | **PASS** | 0 errors |
| **Linter** | `npm run lint` | **PASS** | 0 errors |
| **Production Build** | `npm run build` | **PASS** | Turbopack compiled in 1.71s (8/8 routes generated) |

---

## 6. Commit Summary

- **Branch:** `release/stage-6-remediation`
- **Commit Hash:** `49329b3`
- **Commit Subject:** `test(feedback_loop): use self-contained fixture for resolution audit serialization`
- **Scope:** Single focused commit containing the test fix and documentation.
- **Main Branch:** Untouched at `08fe813` (`origin/main`).
- **Working Tree:** Clean.

---

## 7. Remaining Limitations

1. **Remote Synchronization:** `release/stage-6-remediation` has not been pushed to GitHub origin, pending explicit user push authorization.
2. **Upstream ChromaDB Dependency:** `chromadb==1.5.9` retains 5 CVEs on PyPI with no upstream fix available; accepted risk as `memory=False` isolates the vector store from network execution.
3. **ESLint DevDependencies:** 14 moderate build-time advisories in `minimatch` used by offline ESLint plugins.
