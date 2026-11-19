# Assignment: release observation and rollback plan for change {{change_id}}

Integration head `{{head_sha}}`; base `{{base_sha}}`. Inputs: {{context_refs}}

Write `release/plan.md` (with `## Health criteria` and `## Rollback criteria`) and
`release/rollback.md` (naming revert target `{{base_sha}}`). Report `DONE` attesting both.

`release/rollback.md` starts with your recommendation for recovering if the release goes wrong,
each on its own line:

    Strategy: rollback        (or: Strategy: forward-fix)
    Rationale: <one sentence: why this one – e.g. data changes that cannot be reverted>

A human confirms it at H-3 by attesting the plan, so make the procedure for the recommended
strategy concrete: for rollback, the steps and what happens to data written since; for a forward
fix, how a fix is shipped fast (flag, kill switch, pipeline) and what limits the damage meanwhile.

Human answers on earlier attempts (take them into account):
{{feedback}}
