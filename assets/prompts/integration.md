# Assignment: integration verification for change {{change_id}}

Integration head `{{head_sha}}` checked out at `{{workdir}}`. Inputs: {{context_refs}}

Run all acceptance and integration commands from `plan/oracle.json`, and the commands of every
verification surface it declares (each surface is a real interface), exercise the real interfaces
where possible, write `integration/report.md`, and report `data.verdict`, `data.results`
(`[{"command", "exit_code"}]`) and `data.findings`, attesting `git:{{repo_name}}@{{head_sha}}`.

Human answers on earlier attempts (take them into account):
{{feedback}}
