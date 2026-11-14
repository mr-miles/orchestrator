## Skill: Harness contract (mandatory)

You follow `read-before-write -> evidence-before-action -> minimal diff -> verify-before-report`.

- Call `get_assignment()` first. Read every attestation listed under `context` before acting.
- Do not invent architecture, paths, APIs, helpers, tests or verification commands that the
  assignment and its context do not establish. If a high-impact decision is missing, stop and
  report `BLOCKED` with the exact missing decision (see blocked-protocol).
- Keep changes strictly inside your scope. Never edit files outside `files_modified`, never touch
  protected paths, never write documents outside your write scope.
- Verification is part of the work. Run the commands you were given and record their exit codes
  before you report.
- Finish with exactly one `report(...)` call whose status is `DONE` or `BLOCKED`. If the
  controller rejects the report it returns the violated guard ids – fix the cause and report
  again. You have a limited number of attempts.
- Call `progress(message)` at meaningful milestones so the operator can follow your work.
