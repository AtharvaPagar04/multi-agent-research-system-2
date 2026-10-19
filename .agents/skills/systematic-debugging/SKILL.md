---
name: systematic-debugging
description: >-
  Use when investigating bug reports, unexpected failures, pipeline anomalies, or test regressions. Enforces evidence-based root-cause identification, minimal single-variable hypothesis testing, regression tests, and verification without speculative patches.
---

# Systematic Debugging

## Core Principle

```
NO FIXES WITHOUT REPRODUCIBLE ROOT CAUSE INVESTIGATION FIRST
```

Random edits, speculative patches, and untested heuristics waste engineering time, introduce regressions, and obscure the real defects. Every bug fix must follow a systematic, evidence-backed workflow.

---

## The 5-Phase Debugging Protocol

### Phase 1: Defect Reproduction & Evidence Gathering

1. **Reproduce Deterministically Before Editing:**
   - Establish an exact, repeatable reproduction script or test command before making any code modifications.
   - If a failure is intermittent, isolate the environmental condition, seed, or input variation that triggers it.
   - Record the exact input payload, command line, environment, and error output.

2. **Inspect Upstream State & Data Flow:**
   - Trace the error backward through the execution stack to find where invalid, unexpected, or truncated data first originated.
   - Inspect active logs, intermediate variable states, return types, and recent `git diff` changes.
   - Do not fix the symptom where the exception was raised if the defect was caused by invalid data passed from an upstream caller.

3. **Separate Measured Facts from Assumptions:**
   - Document what is directly verified by logs/tests versus what is inferred or assumed.
   - Explicitly verify assumptions by inspecting runtime values rather than guessing.

### Phase 2: Pattern Analysis & Reference Comparison

1. **Locate Working Counterparts:**
   - Identify similar working code paths or passing test fixtures in the codebase.
   - Compare the failing execution against known-working patterns line by line.

2. **Identify Concrete Discrepancies:**
   - Detail every difference in inputs, arguments, regex patterns, character limits, encodings, or type signatures.
   - Do not dismiss small discrepancies as "unimportant" without evidence.

### Phase 3: Single Testable Hypothesis

1. **Formulate a Precise Hypothesis:**
   - State the cause clearly: *"The defect occurs because component X assumes Y, but under condition Z, input W violates this assumption by producing output V."*
   - Avoid vague statements like *"The regex is broken"* or *"The LLM hallucinated."*

2. **Test One Variable at a Time:**
   - Devise the minimal discriminating experiment that tests the hypothesis directly.
   - Change exactly one variable or code line at a time. Never combine multiple speculative changes into a single test run.

3. **Evaluate the Outcome:**
   - If the hypothesis is refuted, revert the diagnostic change immediately and formulate a new hypothesis based on the new evidence.
   - Do not stack fixes on top of an unverified hypothesis.

### Phase 4: Regression Test & Minimal Targeted Fix

1. **Write a Failing Regression Test First:**
   - Create a minimal, automated test reproducing the exact failure before modifying production logic.
   - Confirm the test fails with the expected failure signature, proving the test is sensitive to the actual defect.

2. **Implement the Minimal Targeted Fix:**
   - Fix the underlying root cause at the source.
   - Avoid speculative "defensive" hacks, bundled refactorings, or unrelated formatting changes.
   - Avoid introducing new heuristic filters, hardcoded regexes, or custom score adjustments unless strictly demonstrated by evidence.

3. **Verify the Fix:**
   - Run the regression test to confirm it now passes cleanly.
   - Confirm the original user-reported failure or reproduction script is fully resolved.

### Phase 5: Regression & Side-Effect Verification

1. **Run Full Test Suite:**
   - Run focused module tests first, followed by the repository's complete test suite:
     ```bash
     PYTHONPATH=research_crew:. .venv313/bin/python -m pytest research_crew/tests/
     ```
   - Verify zero regressions and zero broken downstream components.

2. **Review Working-Tree Changes:**
   - Run `git diff` to inspect every modified line before considering the task complete.
   - Verify no temporary print statements, debug logging, or accidental edits remain.

---

## Anti-Patterns and Red Flags

| Red Flag / Excuse | Reality & Required Action |
|---|---|
| *"Let me try this quick patch to see if it works"* | **STOP.** You are guessing. Return to Phase 1 and gather reproducible evidence. |
| *"I'll add several improvements together to save time"* | **STOP.** You cannot isolate which change resolved the issue or which introduced new bugs. Make one logical change at a time. |
| *"The bug is simple, I don't need a regression test"* | **STOP.** If it broke once, it will break again. Write an automated regression test. |
| *"Let me add a heuristic rule for this specific case"* | **STOP.** Speculative heuristics degrade generalization and lead to false negatives. Fix the underlying parsing/data model defect. |
| *"The test passed in my head"* | **STOP.** Run the test command in the terminal and inspect actual exit codes and output. |

---

## Escalation Rule (3-Failure Limit)

If two consecutive fix attempts fail to resolve the defect:
1. **STOP immediately.** Do not attempt a third ad-hoc patch.
2. Step back and question fundamental assumptions:
   - Is the problem architectural or related to incorrect contract expectations between components?
   - Is the test exercising the wrong layer or testing mock artifacts rather than real behavior?
3. Re-examine the data flow from first principles and document the exact point of divergence.
