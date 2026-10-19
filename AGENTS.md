# Project Agent Instructions & Rules

These project-wide instructions govern development, testing, and delivery workflows across the repository.

## 1. Delivery Phase & Engineering Standards

- **Project Priorities:** Prioritize the currently authorized project milestone. During the current 10-day delivery phase, finish and verify backend-to-frontend integration before optional research-engine improvements.
- **Simplest Implementation:** Prefer the simplest correct implementation; avoid speculative abstractions and unrelated refactors.
- **Simplification with Caution:** Use Ponytail Lite for simplification when available, but do not remove necessary validation, security controls, or tests merely to reduce complexity.
- **Engine Scope Constraint:** Do not modify the research engine during frontend integration unless a concrete integration blocker is identified and explicitly authorized.

## 2. Git & Workspace Hygiene

- **Preserve Existing Work:** Preserve existing staged, unstaged, and untracked work and historical research artifacts (e.g., `evidence_runs/`).
- **No Unauthorized Git Modifications:** Never stage, commit, reset, clean, stash, or discard changes without explicit authorization.

## 3. Testing & Execution Discipline

- **Run & Report Tests:** Run relevant tests and report exact commands and actual results.
- **Testing Hierarchy:** Clearly distinguish unit tests, mocked integration tests, and real end-to-end research runs.
- **Mesh API Safety:** Do not launch live Mesh-backed research during implementation or testing unless explicitly authorized.

## 4. Specialized Skills Reference

For domain-specific workflows, refer to the skills defined in `.agents/skills/`:
- `research-evidence-verification`: Strict factual entailment, citation tracing, and zero-threshold dilution.
- `systematic-debugging`: 5-phase root-cause investigation, minimal single-variable hypothesis testing, and regression verification.
- `test-driven-development`: Red-Green-Refactor discipline and test-tier execution protocol.
