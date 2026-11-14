# Assignment: independent baseline for change {{change_id}}

Inputs: {{context_refs}}. Working copy `{{workdir}}` at integration head `{{head_sha}}`.

Run every acceptance and integration command from `plan/oracle.json`. Write `baseline/report.md`.
Report `DONE` with `data.results = [{"command": …, "exit_code": …}]`, attesting
`git:{{repo_name}}@{{head_sha}}` and the report.
