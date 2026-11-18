# Assignment: retrospective of change {{change_id}}

Goal of the change: {{goal}}  (tier {{tier}}; integration head `{{head_sha}}`)

The change has ended. If it was abandoned or rejected, `change.json` `ended` says who ended it,
at which node and why: find out what led there. A change can end early, so some material may be
missing: work with what there is. Your material is in `../payload/retro/` (relative to your working
directory, `{{workdir}}`): read its `README.md` first. Earlier changes' retrospectives, if any,
are in your context (read them through `docs_read`): note lessons that keep coming back.

Focus asked for by the operator (may be empty):
{{feedback}}

## Method

1. Read every reflection in `retro/reflections/`, then `nodes.json` (feedback and finding
   history per task) and the digests. List candidate problems: rework cycles and what caused
   them, review findings, rejected reports, failures and escalations, time sinks, tool errors,
   repeated commands, slow tool calls, long model waits, cost.
2. For each candidate, look in the logs (grep/`jq`; do not read whole logs) for what happened and
   why, and in `retro/assets/` for the instruction that caused it or should have prevented it.
   `prompt-check.json` tells you whether a run's instructions have changed since it ran.
3. Judge the judges with `retro/findings.json` (reviewers and the critic do not reflect: what
   they missed shows in what happened later). Look for:
   - **escapes** – a finding first raised in a later cycle (or by verification, integration or
     a human) whose code was already in an earlier candidate that a reviewer passed or reviewed
     without raising it: diff the candidates (`refs/legion/evidence/<run>`) to check;
   - **noise** – findings that came back unchanged, were dropped without a fix, or sent work
     back for little value;
   - **critique escapes** – problems found during build or review that the plan's critique
     should have caught.
   Map each to the review criteria that should have caught or prevented it (the reviewer souls,
   `security-checklist`, `architecture-checklist`, `finding-format`, `pre-mortem`).
4. Decide what is worth fixing (cost × likelihood of recurring) and the smallest change that
   would fix it.

## Output

Write `retro/report.md` (your write scope: {{write_scope}}) with these sections:

- `## Summary` – how the change went in numbers (runs, rework cycles, review findings by severity,
  failures, wall time, cost) and the three most valuable improvements.
- `## Review effectiveness` – escapes and noise per review role and the critique, with the
  criteria change for each.
- `## Findings` – one `### ` heading per problem, ranked by value: what happened (with evidence:
  run ids, `legion_ts`, documents), the root cause, its cost, whether it recurs (here or in
  earlier retrospectives), and the proposed fix.
- `## Proposed changes` – per asset file (`souls/…`, `skills/…`, `prompts/…`, `roles.yaml`) a
  unified diff against `retro/assets/`, plus process, tooling or environment changes that are
  not asset edits (say who would make them).
- `## Not worth fixing` – what you looked at and decided against, briefly.

Then report `DONE` attesting `retro/report.md`.
