# Assignment: integration verification for change {{change_id}}

Integration head `{{head_sha}}` checked out at `{{workdir}}`. The protected acceptance tests are
those of the oracle commit `{{oracle_sha}}`. Inputs: {{context_refs}}

Run all acceptance and integration commands from `{{plan}}/oracle.json`, and the commands of every
verification surface it declares (each surface is a real interface), exercise the real interfaces
where possible, write `{{step}}/report.md`, and report `data.verdict`, `data.results`
(`[{"command", "exit_code"}]`) and `data.findings`, attesting `git:{{repo_name}}@{{head_sha}}`.

Report rules:

1. `data.results` has one entry for **every exact command string** in `acceptance_commands`,
   `integration_commands` and each surface's `commands` (a command listed twice is run once and
   listed under each string).
2. On a re-run `{{step}}/report.md` already exists: `docs_read` it and pass its version as
   `base_version`.
3. You cannot commit or push. If the feedback asks you to fix something, report `FAIL` with a
   finding that names the exact fix and file.
4. Check with `report(..., dry_run=true)` before reporting.

Human answers on earlier attempts (take them into account):
{{feedback}}
