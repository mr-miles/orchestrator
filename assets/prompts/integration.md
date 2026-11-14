# Assignment: integration verification for change {{change_id}}

Integration head `{{head_sha}}` checked out at `{{workdir}}`. Inputs: {{context_refs}}

Run all acceptance and integration commands from `plan/oracle.json`, exercise the real interfaces
where possible, write `integration/report.md`, and report `data.verdict`, `data.results`
(`[{"command", "exit_code"}]`) and `data.findings`, attesting `git:{{repo_name}}@{{head_sha}}`.
