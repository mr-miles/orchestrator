# Assignment: plan change {{change_id}} (tier {{tier}}, attempt {{attempt}})

Goal: {{goal}}

Signed-off inputs (read all of them first): {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}`.

Write, in `tickets/{{ticket_id}}/`, following the plan format skill and the workflow skill
(`legion-workflow`: the skeleton your tickets go into and what tier {{tier}} requires):

* `spec.md` – the overview, and one delta spec `specs/<REQ-id>.md` per accepted requirement;
* `oracle.json` – the acceptance tests, commands, criteria (and at R2+ verification surfaces);
* `environment.json` – the sandboxes (sandbox environment skill);
* `tickets.json` – your plan as tickets: the builds `into: builds` (inside `delivery`), each with the criteria the
  tier requires (the integration is `delivery`'s own criterion, already there); and your
  decision about the skeleton's `scaffold` – its work, or cancelled.

Every build must be decision-complete: its `verification` commands must be able to exit 0 using
only its own `files_modified`, against the repository's real configuration and the oracle's
tests (check compiler/type-checker includes that pull in other builds' or protected files, and
per-file coverage thresholds that apply to files the build may not touch). Never leave an
instruction like "adjust until it passes".

Report `DONE` with `outputs: {"spec", "specs": [...], "oracle", "environment", "tickets"}`,
attesting every one of these documents and the `git:` files your design depends on. On a later
attempt every output you name must have been written by this run (G-R4): `docs_write` each
again – unchanged is fine, it creates no version – with `base_version`. Check with
`report(..., dry_run=true)` before reporting: it runs every plan guard and the tier policy.

Sandbox environment: {{environment}}

{{scaffold}}

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

The critic judges your plan, then a decision owner signs it off (H-E1). What to address from the
last attempt (the critique's findings, or the decision owner's reasons):
{{findings}}
