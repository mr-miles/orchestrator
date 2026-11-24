# Assignment: select the design findings to build for change {{change_id}} (attempt {{attempt}})

The reviewed findings and their review: {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}`.

Write `tickets/{{ticket_id}}/selection.json` (call `docs_schema` first; on a later attempt it
exists: `docs_read` it and pass its version as `base_version`). Every finding appears exactly
once:

- `selected`: only findings the review rated `valid`; a reason, a `scope` (what to build) and
  the finding's evidence copied unchanged;
- `dropped`: a reason.

Choose by value, effort and risk. Report `DONE` with `outputs: {"selection": …}`, attesting the
document. Check with `report(..., dry_run=true)` first.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

A decision owner signs off your selection (H-S). What to address from the last attempt (their
reasons):
{{findings}}
