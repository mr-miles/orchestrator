# Assignment: independent baseline for change {{change_id}}

Inputs: {{context_refs}}. Working copy `{{workdir}}` at integration head `{{head_sha}}`.

Run every acceptance and integration command from `plan/oracle.json`. Write `baseline/report.md`.
Report `DONE` with `data.results = [{"command": …, "exit_code": …}]`, attesting
`git:{{repo_name}}@{{head_sha}}` and the report.

Nothing is built yet, so acceptance commands are expected to fail: record each with its real exit
code (note "expected: not implemented yet" in the report) and report `DONE` – a failure is not a
reason to report `BLOCKED`. Report `BLOCKED` only if a command cannot run at all.

Human answers on earlier attempts (take them into account):
{{feedback}}
