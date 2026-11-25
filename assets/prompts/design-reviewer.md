# Assignment: design review of {{repo_name}} for change {{change_id}} (attempt {{attempt}})

Goal of the change:

> {{goal}}

Repository at `{{workdir}}`, commit `{{base_sha}}`. Context: {{context_refs}}

Load the codebase-design skill and apply it to the whole repository. Write
`tickets/{{ticket_id}}/findings.json` (call `docs_schema` first; on a later attempt it exists:
`docs_read` it and pass its version as `base_version`). Number the findings `F-1`, `F-2` …. Each
finding's `evidence` are `git:` files at `{{base_sha}}` that show the problem.

Report `DONE` with `outputs: {"findings": …}`, attesting the document and every evidence file
of every finding (G-DR1). Check with `report(..., dry_run=true)` first.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

What to address from the last attempt:
{{findings}}
