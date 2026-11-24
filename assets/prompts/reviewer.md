# Assignment: review {{ticket_id}} (attempt {{attempt}})

Commit under review: `{{judged_sha}}` (base `{{base_sha}}`), checked out at `{{workdir}}`.
Review the diff `git diff {{base_sha}}..{{judged_sha}}`.

The contract of the work you judge:

```json
{{work_json}}
```

Its evidence and context: {{context_refs}}

What it changed beyond its `files_modified`, with its reasons:

{{beyond_scope}}

Review {{review_scope}}. For each change beyond its scope, judge the reason: does the contract
(its requirements, acceptance, verification) really need this change, is it the smallest change
that does, and is it not another build's work (a file a build alongside it owns is not, unless
there was no other way)? A change to a **protected** path (the operator's protected paths, a
pinned `mise.toml`) is held to a much higher bar: pass it only if the contract cannot be met any
other way and the change is minimal – the setup always runs the pinned `mise.toml`, so a
`mise.toml` edit never changes the setup the checks run. A reason that does not hold is a
BLOCKER, HIGH confidence, whose fix is to revert the change (or to make it smaller).

Write `tickets/{{ticket_id}}/review.md` (on a later attempt it exists – your predecessor's
review, for an earlier commit: read it, then `docs_write` with its version as `base_version`).
Report with `data.verdict`, `data.findings`, `data.checked` and `outputs: {"review": …}`,
attesting `git:{{repo_name}}@{{judged_sha}}`.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).
