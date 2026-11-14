# Assignment: implement {{task_id}} – {{task_title}} (cycle {{cycle}})

Change {{change_id}}, tier {{tier}}. Working copy `{{workdir}}` on branch `{{branch}}`,
base `{{base_sha}}`.

Task contract (authoritative):

```json
{{task_json}}
```

Context to read before editing: {{context_refs}}

Steps: read context → implement within `files_modified` → run every `verification` command →
write `tasks/{{task_id}}/evidence/cycle-{{cycle}}.md` listing each command with its exit code and the
relevant output → commit → report `DONE` attesting `git:{{repo_name}}@<HEAD sha>` and the evidence
document.

Findings / feedback you must address this cycle:
{{feedback}}
