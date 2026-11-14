# Assignment: security review {{task_id}} – {{task_title}} (cycle {{cycle}})

Candidate commit: `{{candidate_sha}}` (base `{{base_sha}}`), checked out at `{{workdir}}`.

Task contract:

```json
{{task_json}}
```

Context: {{context_refs}}

Apply the security checklist to `git diff {{base_sha}}..{{candidate_sha}}`. Write
`reviews/{{task_id}}/security-cycle-{{cycle}}.md`. Report with `data.verdict`, `data.findings`,
`data.checked`, attesting `git:{{repo_name}}@{{candidate_sha}}`.

Earlier findings: {{feedback}}
