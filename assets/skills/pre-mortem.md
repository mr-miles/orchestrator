## Skill: Pre-mortem and assumption hunting

Imagine the plan shipped and failed. For each plausible cause, check the plan:

1. **Pre-mortem:** list the 3–5 most likely failure stories (integration seams, data shape drift,
   missing error paths, ordering/parallelism hazards, untestable acceptance criteria).
2. **Assumptions:** list every assumption the plan makes about existing code, APIs, environment
   or data. Verify each against the repository (cite `git:` attestations). Unverified high-impact
   assumptions are BLOCKERs.
3. **Completeness:** every requirement covered, every task decision-complete (no "choose an
   approach"), verification commands that actually exercise the acceptance criteria.
4. **Parallel safety:** tasks without dependency edges must not touch the same files or rely on
   each other's outputs.

Verdict `REWORK` if any HIGH-confidence BLOCKER or WARNING exists, else `PASS`.
