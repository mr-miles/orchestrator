# Assignment: review {{ticket_id}} (attempt {{attempt}})

Commit under review: `{{judged_sha}}` (base `{{base_sha}}`), checked out at `{{workdir}}`.
Review the diff `git diff {{base_sha}}..{{judged_sha}}`.

The contract of the work you judge:

```json
{{work_json}}
```

Its evidence and context: {{context_refs}}

Write `tickets/{{ticket_id}}/review.md` (on a later attempt it exists – your predecessor's
review, for an earlier commit: read it, then `docs_write` with its version as `base_version`).
Report with `data.verdict`, `data.findings`, `data.checked` and `outputs: {"review": …}`,
attesting `git:{{repo_name}}@{{judged_sha}}`.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).
