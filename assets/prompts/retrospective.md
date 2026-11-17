# Assignment: retrospective of change {{change_id}}

Goal of the change: {{goal}}  (tier {{tier}}; integration head `{{head_sha}}`)

The change has ended. Your material is in `../payload/retro/` (relative to your working
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
3. Decide what is worth fixing (cost × likelihood of recurring) and the smallest change that
   would fix it.

## Output

Write `retro/report.md` (your write scope: {{write_scope}}) with these sections:

- `## Summary` – how the change went in numbers (runs, rework cycles, review findings by severity,
  failures, wall time, cost) and the three most valuable improvements.
- `## Findings` – one `### ` heading per problem, ranked by value: what happened (with evidence:
  run ids, `legion_ts`, documents), the root cause, its cost, whether it recurs (here or in
  earlier retrospectives), and the proposed fix.
- `## Proposed changes` – per asset file (`souls/…`, `skills/…`, `prompts/…`, `roles.yaml`) a
  unified diff against `retro/assets/`, plus process, tooling or environment changes that are
  not asset edits (say who would make them).
- `## Not worth fixing` – what you looked at and decided against, briefly.

Then report `DONE` attesting `retro/report.md`.
