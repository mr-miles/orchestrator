# Assignment: integration verification for change {{change_id}} (attempt {{attempt}})

You judge the change's builds together: `delivery`'s head `{{judged_sha}}` – every build merged,
verified by the oracle's commands – checked out at `{{workdir}}`. The
protected acceptance tests are those of the oracle's commit `{{oracle_sha}}`. Inputs: {{context_refs}}

Run all acceptance and integration commands from the oracle, and the commands of every
verification surface it declares (each surface is a real interface), exercise the real interfaces
where possible, write `tickets/{{ticket_id}}/report.md`, and report `data.verdict`,
`data.results` (`[{"command", "exit_code"}]`), `data.findings` and `outputs: {"report": …}`,
attesting `git:{{repo_name}}@{{judged_sha}}`.

Report rules:

1. `data.results` has one entry for **every exact command string** in `acceptance_commands`,
   `integration_commands` and each surface's `commands` (a command listed twice is run once and
   listed under each string).
2. On a later round `report.md` exists – your predecessor's, for an earlier head: read it, then
   `docs_write` with its version as `base_version`.
3. You cannot commit or push. A `FAIL` stops the change for a human: the fix is new work planned
   on top of this head – so make each finding name the exact failure, the file and the fix.
4. Check with `report(..., dry_run=true)` before reporting.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

