# Assignment: review {{task_id}} – {{task_title}} (cycle {{cycle}})

Candidate commit: `{{candidate_sha}}` (base `{{base_sha}}`), checked out at `{{workdir}}`.
Review the diff `git diff {{base_sha}}..{{candidate_sha}}`.

Task contract:

```json
{{task_json}}
```

Evidence and context: {{context_refs}}

Write `reviews/{{task_id}}/reviewer-cycle-{{cycle}}.md`. Report with `data.verdict`,
`data.findings`, `data.checked`, attesting `git:{{repo_name}}@{{candidate_sha}}`.

Earlier findings on this task: {{feedback}}
