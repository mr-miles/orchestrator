# Assignment: critique the plan for change {{change_id}} (cycle {{cycle}})

Plan under review: {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}` – verify assumptions against it.

Run the pre-mortem skill. Write `critique/cycle-{{cycle}}.md`. Report with
`data.verdict` (`PASS`/`REWORK`) and `data.findings`, attesting the **latest** versions of
`plan/spec.md`, `plan/oracle.json`, `plan/taskgraph.json` (and `plan/environment.json` when it is
among your inputs) – G-C1 – and every attestation your findings cite. Check with
`report(..., dry_run=true)` before reporting.

Previous critique cycles: {{feedback}}
