# Assignment: review the pull request description of change {{change_id}} (attempt {{attempt}})

Goal: {{goal}}

The change's head `{{head_sha}}` is checked out at `{{workdir}}`; the change is
`git diff {{change_base_sha}}..{{head_sha}}`. The description under review and what it must be
faithful to: {{context_refs}}

Judge whether the description (`pr`) describes the change faithfully:

* its motivation is the change's goal and requirements;
* every significant change in the diff is described, and nothing it describes is absent;
* what it says was verified matches the integration report and the builds' evidence.

Write `tickets/{{ticket_id}}/review.md` (on a later attempt it exists: read it, then `docs_write`
with its version as `base_version`). Report `data.verdict` (`PASS`/`REWORK`), `data.findings`
(REWORK needs at least one) and `outputs: {"review": …}`, attesting `git:{{repo_name}}@{{head_sha}}`,
the **latest** version of the description (G-PR2) and every attestation your findings cite. Check
with `report(..., dry_run=true)` before reporting.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).
