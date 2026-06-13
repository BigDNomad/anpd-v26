# Defect Queue

## Open

### DQ-001: Q5 (character_profile_fidelity) demoted to advisory
- **Date:** 2026-06-13
- **Severity:** Was Class A, now capped at Class B (advisory)
- **Reason:** Q5 returned [unstable: WEAK/FAIL/PASS] across 3 passes — rubric is too subjective for LLM to converge. A non-converging check must not gate at Class A (same principle as MA-001 demotion).
- **Evidence added:** Structured violations array (character, scene, profile_rule) now emitted when LLM provides them.
- **Restore condition:** Q5 can be promoted back to Class A when either (a) the rubric is made deterministic (e.g., pattern-matching against profile rules rather than LLM judgment), or (b) convergence is demonstrated across 10+ runs on a fixed fixture.
- **Commits:** `071962d`, `a58dcfd`

## Closed

(none)
