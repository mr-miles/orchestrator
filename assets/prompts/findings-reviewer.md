# Assignment: review the design findings of change {{change_id}} (attempt {{attempt}})

The findings under review, and what they are based on: {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}` – check each finding's evidence against it.

Write `tickets/{{ticket_id}}/findings-review.json` (call `docs_schema` first; on a later attempt
it exists: `docs_read` it and pass its version as `base_version`): exactly one review per
finding, each `valid`, `invalid` or `unclear`, with a severity and a note.

Report with `data.verdict` (`PASS`/`REWORK`) and `data.findings` (finding-format skill),
`outputs: {"findings-review": …}`, attesting the **latest** version of `findings` (G-C1) and the
evidence files you checked. REWORK when a finding is unevidenced or wrong. Check with
`report(..., dry_run=true)` first.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).
