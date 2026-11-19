---
description: "MANDATORY for every legion run: load it before your first action. The rules every agent follows: read before write, scope, verification, how to report."
---

## Skill: Harness contract (mandatory)

You follow `read-before-write -> evidence-before-action -> minimal diff -> verify-before-report`.

- Call `get_assignment()` first. Read every attestation listed under `context` before acting.
  A `file:<path>@<version>` attestation is a **document in the document store**, not a file in
  your working copy: read it with `docs_read` (searching the checkout finds nothing). `git:`
  attestations are commits in the repository.
- Do not invent architecture, paths, APIs, helpers, tests or verification commands that the
  assignment and its context do not establish. If a high-impact decision is missing, stop and
  report `BLOCKED` with the exact missing decision (see blocked-protocol).
- Keep changes strictly inside your scope. Never edit files outside `files_modified`, never touch
  protected paths, never write documents outside your write scope.
- Verification is part of the work. Run the commands you were given and record their exit codes
  before you report.
- If you run a command in the background (`Monitor`, or `Bash` with `run_in_background`), its
  exit code is unknown until its completion notice arrives. Wait for that notice before you
  record the result or report; never report while a task you started is still running. Stop
  any background process you no longer need before you report.
- Finish with exactly one `report(...)` call whose status is `DONE` or `BLOCKED`. If the
  controller rejects the report it returns the violated guard ids – fix the cause and report
  again. You have a limited number of attempts. Check first with the same call and
  `dry_run=true`: it runs every guard, returns `would_accept` and the violations, and uses no
  attempt.
- Call `progress(message)` at meaningful milestones so the operator can follow your work.
