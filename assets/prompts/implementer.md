# Assignment: build {{ticket_id}} – {{ticket_goal}} (attempt {{attempt}})

Change {{change_id}}, tier {{tier}}. Working copy `{{workdir}}` on branch `{{branch}}`,
base `{{base_sha}}`.

Your contract (authoritative):

```json
{{work_json}}
```

Context to read before editing: {{context_refs}}

Steps, in this order:

1. Read the context above: `file:` entries are documents, read with `docs_read` (they are not in
   the working copy). When the repository's `mise.toml` has a setup task it has already run in
   your working copy; otherwise install the project's dependencies before running any of its
   tools (see language preferences). Your sandbox's tools: {{sandbox_tools}}.
2. Implement within `files_modified`. Run the formatter and linter early, not only at the end.
3. Run every `verification` command; all must exit 0. Report `BLOCKED` with the exact conflict if
   a command cannot pass from inside `files_modified`, or if protected acceptance tests fail for a
   reason other than the missing implementation (for example a harness or configuration error).
4. Commit and push (git protocol). The evidence is a document, never a file in the repository.
5. Write `tickets/{{ticket_id}}/evidence.md` with `docs_write` (on a later attempt it exists:
   `docs_read` it and pass its version as `base_version`): one line per verification command,
   the command verbatim in backticks followed by `exit <code>` (lowercase `exit`), then any
   relevant output. Not a table.
6. Check with `report(..., dry_run=true)`, then report `DONE` with `outputs: {"evidence": …}`,
   attesting `git:{{repo_name}}@<pushed HEAD sha>` and the evidence document.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

After you report, the controller verifies your commit, your judges review it and it is merged –
in that order. Findings you must address on this attempt:
{{findings}}
