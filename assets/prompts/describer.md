# Assignment: describe change {{change_id}} as a pull request (attempt {{attempt}})

Goal: {{goal}}

The change's head `{{head_sha}}` is checked out at `{{workdir}}`; the change is everything
since its base: `git diff {{change_base_sha}}..{{head_sha}}` (and `git log` for its history).
Inputs – the requirements, the spec and what else you are given: {{context_refs}}

Write `tickets/{{ticket_id}}/pr.md` (G-PR1):

1. First line: the PR's title, `# <title>` – imperative, at most 120 characters.
2. `## Motivation` – why: the goal and the problem it solves, citing every requirement id
   (`REQ-<n>`) the change implements.
3. `## Changes` – what changed, grouped by area, as the diff shows it; call out anything risky,
   surprising or breaking.
4. `## Verification` – how it was verified: the acceptance tests, the integration report, what
   was not verified.
5. Optionally `## Notes for reviewers` – where to look first, known limitations, follow-ups.

Report with `outputs: {"pr": …}`, attesting `git:{{repo_name}}@{{head_sha}}` and the
description. On a later attempt `pr.md` already exists: `docs_read` it and pass its version as
`base_version`. Check with `report(..., dry_run=true)` before reporting.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

A decision owner signs the change with your description (H-3). Findings to address on this
attempt:
{{findings}}
