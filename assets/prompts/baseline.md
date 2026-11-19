# Assignment: independent baseline for change {{change_id}}

Inputs: {{context_refs}}. Working copy `{{workdir}}` at integration head `{{head_sha}}`.

Your working copy has had the repository's setup (`mise run setup`) when `mise.toml` defines it,
as the controller's verifier will. Run every acceptance command, then every integration command,
from `plan/oracle.json`, in that order. Write `baseline/report.md`.
Report `DONE` with `data.results = [{"command": …, "exit_code": …}]`, attesting
`git:{{repo_name}}@{{head_sha}}` and the report.

Nothing is built yet, so acceptance commands are expected to fail: record each with its real exit
code (note "expected: not implemented yet" in the report) and report `DONE` – a failure is not a
reason to report `BLOCKED`. Report `BLOCKED` only if a command cannot run at all.

Human answers on earlier attempts (take them into account):
{{feedback}}
