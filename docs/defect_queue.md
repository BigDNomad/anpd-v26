# Defect Queue

## Open

### DQ-001: Q5 (character_profile_fidelity) demoted to advisory
- **Date:** 2026-06-13
- **Severity:** Was Class A, now capped at Class B (advisory)
- **Reason:** Q5 returned [unstable: WEAK/FAIL/PASS] across 3 passes — rubric is too subjective for LLM to converge. A non-converging check must not gate at Class A (same principle as MA-001 demotion).
- **Evidence added:** Structured violations array (character, scene, profile_rule) now emitted when LLM provides them.
- **Restore condition:** Q5 can be promoted back to Class A when either (a) the rubric is made deterministic (e.g., pattern-matching against profile rules rather than LLM judgment), or (b) convergence is demonstrated across 10+ runs on a fixed fixture.
- **Commits:** `071962d`, `a58dcfd`

### DQ-003: character_role_enum_check — generator produces free-text roles
- **Date:** 2026-06-13
- **Severity:** Class A (blocks Phase 4)
- **Reason:** character_generator LLM outputs free-text `character_role` values (e.g., "Senior PJ, mentor to Archer") instead of Schema v1.1.0 §3.2 enum: `antagonist`, `protagonist`, `recurring`, `supporting`. 12/12 characters failed on attempt 8.
- **Fix options:** (a) constrain generator prompt to output only enum values, (b) add post-generation normalization, (c) both.
- **Commits:** (pending next dispatch)

## Closed

### DQ-002: R007 stale STOP_REPORT (RESOLVED)
- **Date opened:** 2026-06-13
- **Date closed:** 2026-06-13
- **Root cause:** TRANSIENT — stale STOP_REPORT from prior run survived into new run. R007 checks file existence not mtime.
- **Fix:** master_controller cleans stale STOP_REPORT at run start. Per-pass exceptions no longer write STOP_REPORTs.
- **Commits:** `7547746`
