# Assignment: implement {{task_id}} – {{task_title}} (cycle {{cycle}})

Change {{change_id}}, tier {{tier}}. Working copy `{{workdir}}` on branch `{{branch}}`,
base `{{base_sha}}`.

Task contract (authoritative):

```json
{{task_json}}
```

Context to read before editing: {{context_refs}}

Steps, in this order:

1. Read the context above. Install the project's dependencies before running any of its tools
   (see language preferences), and use only tools your sandbox has (`command -v` before relying on
   `python3`, `node` or similar).
2. Implement within `files_modified`. Run the formatter and linter early, not only at the end.
3. Run every `verification` command; all must exit 0. Report `BLOCKED` with the exact conflict if
   a command cannot pass from inside `files_modified`, or if protected acceptance tests fail for a
   reason other than the missing implementation (for example a harness or configuration error).
4. Commit and push (git protocol). The evidence is a document, never a file in the repository.
5. Write `tasks/{{task_id}}/evidence/cycle-{{cycle}}.md` with `docs_write`: one line per
   verification command, the command verbatim in backticks followed by `exit <code>` (lowercase
   `exit`), then any relevant output. Not a table.
6. Check with `report(..., dry_run=true)`, then report `DONE` attesting
   `git:{{repo_name}}@<pushed HEAD sha>` and the evidence document.

Findings / feedback you must address this cycle:
{{feedback}}
