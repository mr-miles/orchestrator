# Assignment: independent baseline for change {{change_id}}

Inputs: {{context_refs}}. Working copy `{{workdir}}` at integration head `{{head_sha}}`.

Run the `setup_commands` from `plan/oracle.json` first, then every acceptance and integration
command, in that order (as the controller's verifier does). Report the acceptance and integration
commands. Write `baseline/report.md`.
Report `DONE` with `data.results = [{"command": …, "exit_code": …}]`, attesting
`git:{{repo_name}}@{{head_sha}}` and the report.

Human answers on earlier attempts (take them into account):
{{feedback}}
