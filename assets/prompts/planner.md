# Assignment: plan change {{change_id}} (tier {{tier}})

Goal: {{goal}}

Accepted inputs (read all of them first): {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}`.

Produce `{{plan}}/spec.md`, one delta spec `{{plan}}/specs/<REQ-id>.md` per accepted requirement,
`{{plan}}/oracle.json` (with verification surfaces) and `{{plan}}/taskgraph.json` following the task graph
schema skill. Every task must be decision-complete. Report `DONE` attesting all of these documents
and the `git:` files your design depends on.

Every task's `verification` commands must be able to exit 0 using only that task's
`files_modified`, against the repository's real configuration and the oracle's tests (check
compiler/type-checker includes that pull in other tasks' or protected files, and per-file coverage
thresholds that apply to files the task may not touch). Never leave an instruction like "adjust
until it passes".

On a rework (cycle 2 and later) every document you attest must have been written by this run
(G-R4, G-P9): `docs_write` each required document and **every** delta spec again, even if
unchanged (an identical write creates no new version and counts as yours). Update task `context`
to the latest versions. Check with `report(..., dry_run=true)` before reporting.

Sandbox environment: {{environment}}

{{scaffold}}

{{fix_round}}

Write scope: {{write_scope}}. Required documents: {{required_docs}}.

Critique / human feedback to address (cycle {{cycle}}):
{{feedback}}
