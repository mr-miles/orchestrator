# Assignment: independent baseline for change {{change_id}} (attempt {{attempt}})

Inputs: {{context_refs}}. Working copy `{{workdir}}` at `{{head_sha}}` – the oracle's tests
merged, nothing built yet.

Your working copy has had the repository's setup (`mise run setup`) when `mise.toml` defines it,
as the controller's verifier will. Run every acceptance command, then every integration command,
from the oracle, in that order. Write `tickets/{{ticket_id}}/report.md`.
Report `DONE` with `data.results = [{"command": …, "exit_code": …}]` and
`outputs: {"report": …}`, attesting `git:{{repo_name}}@{{head_sha}}` and the report.

Nothing is built yet, so acceptance commands are expected to fail: record each with its real exit
code (note "expected: not implemented yet" in the report) and report `DONE` – a failure is not a
reason to report `BLOCKED`. Report `BLOCKED` only if a command cannot run at all.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}). On a later attempt the
report exists: `docs_read` it and pass its version as `base_version`.

A decision owner signs off your report (H-2b). What to address from the last attempt:
{{findings}}
