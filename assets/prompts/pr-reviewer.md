# Assignment: review the pull request description of change {{change_id}} (cycle {{cycle}})

Goal: {{goal}}

The integration head `{{head_sha}}` is checked out at `{{workdir}}`; the change is
`git diff {{change_base_sha}}..{{head_sha}}`. The description under review and what it must be
faithful to: {{context_refs}}

Judge whether `delivery/pr.md` describes the change faithfully:

* its motivation is the change's goal and requirements;
* every significant change in the diff is described, and nothing it describes is absent;
* what it says was verified matches the integration report and the task evidence.

Write `delivery/reviews/pr-reviewer-cycle-{{cycle}}.md`. Report `data.verdict` (`PASS`/`REWORK`)
and `data.findings` (REWORK needs at least one), attesting `git:{{repo_name}}@{{head_sha}}`, the
**latest** version of `delivery/pr.md` (G-PR2) and every attestation your findings cite. Check
with `report(..., dry_run=true)` before reporting.
