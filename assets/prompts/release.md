# Assignment: release observation and rollback plan for change {{change_id}} (attempt {{attempt}})

The change's head `{{head_sha}}`; the change's base `{{change_base_sha}}`. Inputs: {{context_refs}}

Write `tickets/{{ticket_id}}/release-plan.md` (with `## Health criteria` and
`## Rollback criteria`) and `tickets/{{ticket_id}}/rollback.md` (naming the revert target
`{{change_base_sha}}`). Report `DONE` with your outputs named, attesting both.

`rollback.md` starts with your recommendation for recovering if the release goes wrong, each on
its own line:

    Strategy: rollback        (or: Strategy: forward-fix)
    Rationale: <one sentence: why this one – e.g. data changes that cannot be reverted>

Make the procedure for the recommended strategy concrete: for rollback, the steps and what
happens to data written since; for a forward fix, how a fix is shipped fast (flag, kill switch,
pipeline) and what limits the damage meanwhile.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}). On a later attempt they
exist: `docs_read` each and pass its version as `base_version`.

What to address from the last attempt:
{{findings}}
