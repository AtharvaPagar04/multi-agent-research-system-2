---
name: test-driven-development
description: >-
  Use when implementing behavior changes, new research features, or bug fixes. Enforces the Red-Green-Refactor discipline, test-type hierarchy (unit, mocked integration, live API, end-to-end), and mandatory test suite verification.
---

# Test-Driven Development & Rigorous Testing

## Core Principle

```
TEST FIRST: RED -> GREEN -> REFACTOR
```

If you did not observe the test fail before implementing the change, you cannot be certain the test is sensitive to the defect or validating the intended requirement.

---

## The Testing Hierarchy

To maintain testing integrity, always categorize tests explicitly. Never conflate levels:

| Test Level | Scope & Boundaries | Typical Execution Time | Proof Value |
|---|---|---|---|
| **Unit Tests** | Pure logic, single class or function. No I/O, no network calls. | < 0.1s | Proves isolated algorithmic correctness. |
| **Mocked Integration Tests** | Multi-component interactions using deterministic mocked responses (e.g., `MagicMock` for Mesh LLM or search tools). | < 2.0s | Proves wiring, protocol adherence, schema parsing, and fallback logic under synthetic inputs. |
| **Live API Tests** | Direct interaction with real external services (Mesh API, arXiv API, DuckDuckGo) with real network latency. | 5s – 60s | Validates real-world provider contracts, authentication, and response schemas. |
| **Full End-to-End Tests** | Complete execution of multi-agent research crews, feedback loops, evidence stores, and final report verification. | 1m – 5m | Proves end-to-end system orchestration and output quality. |

### The Golden Rule of Test Reporting
> **NEVER describe mocked or helper-level tests as proof of end-to-end correctness.**  
> A unit test or mock test only verifies that the code satisfies the mock's assumptions. It does not prove that external APIs, live LLMs, or multi-agent pipelines behave correctly end-to-end.

---

## The TDD Cycle

### 1. RED — Write the Failing Test First
- Write the smallest possible automated test that demonstrates the desired feature or reproduces the reported defect.
- Choose a descriptive test function name that states the scenario and expected outcome (e.g., `test_phase2u_entity_mismatch_false_rejection_resolved`).
- Use real production classes and domain models (`AtomicClaim`, `EvidenceRecord`, `EvidenceStore`) rather than synthetic mock objects whenever feasible.
- **Execute the test to verify it fails:**
  ```bash
  PYTHONPATH=research_crew:. .venv313/bin/python -m pytest path/to/test_file.py -k "test_name"
  ```
- **Confirm the failure reason:** Verify that it fails due to the missing feature or known defect—not due to an import error, syntax typo, or missing fixture.

### 2. GREEN — Implement Minimal Code
- Write the minimal code necessary to make the failing test pass.
- Do not implement speculative extras, unrequested configuration options, or unrelated refactorings.
- Maintain existing codebase conventions and docstrings.
- **Execute the test to verify it passes:**
  ```bash
  PYTHONPATH=research_crew:. .venv313/bin/python -m pytest path/to/test_file.py -k "test_name"
  ```

### 3. REFACTOR — Clean Up and Optimize
- Clean up duplication, clarify variable names, and ensure clean error handling.
- Keep the test suite passing throughout the refactoring stage.
- Do not add new behavior during the refactor phase.

---

## Test Execution Protocol

Always execute tests in progressive tiers to catch regressions efficiently:

1. **Step 1: Focused Unit / Regression Test:**
   ```bash
   PYTHONPATH=research_crew:. .venv313/bin/python -m pytest research_crew/tests/test_claim_verification.py -k "<test_name>"
   ```
2. **Step 2: Entire Targeted Test Module:**
   ```bash
   PYTHONPATH=research_crew:. .venv313/bin/python -m pytest research_crew/tests/test_claim_verification.py
   ```
3. **Step 3: Full Repository Test Suite (Mandatory before completion):**
   ```bash
   PYTHONPATH=research_crew:. .venv313/bin/python -m pytest research_crew/tests/
   ```

---

## Test Quality & Edge Cases

When evaluating or authoring tests, systematically inspect:
- **False Positives:** Could this test pass even if the implementation is completely broken? (e.g., asserts on `is not None` without checking contents).
- **False Negatives:** Is the test brittle due to unordered lists, whitespace differences, non-deterministic timestamps, or transient network availability?
- **Boundary Conditions:** Test empty strings, zero values, extreme thresholds, missing keys, malformed markdown, and unicode/escape sequences.
- **Negative & Adversarial Cases:** Always test that invalid, contradictory, or ungrounded claims are rejected or flagged correctly (`ClaimVerdict.INSUFFICIENT_EVIDENCE` or `ClaimVerdict.CONTRADICTED`).

---

## Reporting Standard

When reporting test results to users or documentation:
- **Never claim a test passed unless it was actually executed and passed in the current session.**
- Report the **exact command** executed.
- Report the **exact pass/fail counts** and total execution time (e.g., `101 passed in 1.58s`).
- Report any warnings (deprecation, resource leaks) and known limitations.
