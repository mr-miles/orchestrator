---
description: "MANDATORY for every legion run: load it before your first action. The rules every agent follows: read before write, scope, verification, how to report."
---

## Skill: Harness contract (mandatory)

You follow `read-before-write -> evidence-before-action -> minimal diff -> verify-before-report`.

- Call `inbox()` first. Your work is the task from the orchestrator (its `task_id` is your run
  id): its first message is your assignment (`assignment` holds it structured, as
  `get_assignment()` does). Messages from other agents of the change arrive in the same inbox.
  Read every attestation listed under `context` before acting. `workflow_context()` shows how
  your work fits the change (what it builds on, what builds on it, nearby tasks, the criteria it
  serves, who judges it, whom to ask); `step_status()` shows your step as it stands now (cycle,
  budget left, verification, the panel's verdicts so far).
  A `file:<path>@<version>` attestation is a **document in the document store**, not a file in
  your working copy: read it with `docs_read` (searching the checkout finds nothing). `git:`
  attestations are commits in the repository.
- Do not invent architecture, paths, APIs, helpers, tests or verification commands that the
  assignment and its context do not establish. If a high-impact decision is missing, stop and
  report `BLOCKED` with the exact missing decision (see blocked-protocol).
- Keep changes strictly inside your scope. Never edit files outside `files_modified`, never touch
  protected paths, never write documents outside your write scope.
- Verification is part of the work. Run the commands you were given and record their exit codes
  before you report. The controller also verifies every tip you push (`verification_status`
  over MCP): a failure there is what review will start from, so fix it first.
- If you run a command in the background (`Monitor`, or `Bash` with `run_in_background`), its
  exit code is unknown until its completion notice arrives. Wait for that notice before you
  record the result or report; never report while a task you started is still running. Stop
  any background process you no longer need before you report.
- **File your results with `report(...)`** (status `DONE` or `BLOCKED`): it records them – the
  documents, attestations and data – and ends nothing. If the controller rejects the report it
  returns the violated guard ids – fix the cause and report again. You have a limited number of
  attempts. Check first with the same call and `dry_run=true`: it runs every guard, returns
  `would_accept` and the violations, and uses no attempt.
- **Then end your task with `a2a_reply(task_id=<your run id>, text=<one line>, state=...)`**:
  `completed` after an accepted DONE report, `input-required` after an accepted BLOCKED one
  (blocked-protocol), `failed` only to give up. Answer the messages in your inbox first. Then
  **end your turn**: that hands your work back to the controller, and nothing you do after it
  counts. If the reply does not match your record, or your turn ends with the task still open,
  the orchestrator tells you what is missing – each time uses one of your attempts.
- Call `progress(message)` at meaningful milestones so the operator can follow your work.
