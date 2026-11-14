# Assignment: plan change {{change_id}} (tier {{tier}})

Goal: {{goal}}

Accepted inputs (read all of them first): {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}`.

Produce `plan/spec.md`, one delta spec `plan/specs/<REQ-id>.md` per accepted requirement,
`plan/oracle.json` (with verification surfaces) and `plan/taskgraph.json` following the task graph
schema skill. Every task must be decision-complete. Report `DONE` attesting all of these documents
and the `git:` files your design depends on.

Write scope: {{write_scope}}. Required documents: {{required_docs}}.

Critique / human feedback to address (cycle {{cycle}}):
{{feedback}}
