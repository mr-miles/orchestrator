# Assignment: integration verification for change {{change_id}} (attempt {{attempt}})

The change's head `{{head_sha}}` – every build merged – checked out at `{{workdir}}`. The
protected acceptance tests are those of the oracle's commit `{{oracle_sha}}`. Inputs: {{context_refs}}

Run all acceptance and integration commands from the oracle, and the commands of every
verification surface it declares (each surface is a real interface), exercise the real interfaces
where possible, write `tickets/{{ticket_id}}/report.md`, and report `data.verdict`,
`data.results` (`[{"command", "exit_code"}]`), `data.findings` and `outputs: {"report": …}`,
attesting `git:{{repo_name}}@{{head_sha}}`.

Report rules:

1. `data.results` has one entry for **every exact command string** in `acceptance_commands`,
   `integration_commands` and each surface's `commands` (a command listed twice is run once and
   listed under each string).
2. On a later attempt `report.md` exists: `docs_read` it and pass its version as `base_version`.
3. You cannot commit or push. A `FAIL` is not the end: your ticket waits, and the fix is new work
   planned on top of this head – so make each finding name the exact failure, the file and the
   fix.
4. Check with `report(..., dry_run=true)` before reporting.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

What to address from the last attempt:
{{findings}}
