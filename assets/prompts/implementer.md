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
   `files_modified` is the scope the plan expects. If your contract cannot be met without
   changing a file outside it (or in `files_forbidden`), change it – minimally – and list it in
   your report's `data.beyond_scope` as `{"path": …, "reason": …}`: what fails or is missing
   without the change. A reviewer judges every reason, and sends the work back if one does not
   hold. A protected path (the operator's, or a pinned `mise.toml`) is judged strictly: change
   one only when there is no other way, and know that the setup always runs the pinned
   `mise.toml`, so editing it does not change the setup. Never change the oracle's acceptance
   tests: ask `oracle@oracle` if one looks wrong.
3. Run every `verification` command; all must exit 0 (go beyond `files_modified`, with a reason,
   when only that makes one pass). If a protected acceptance test fails for a
   reason other than the missing implementation, ask the oracle (`oracle@oracle`,
   `legion-a2a-protocol`) before you block: if it explains the test, fix your code; if it
   confirms the test is wrong, a fix follows – build to the spec and go on; only what it cannot
   resolve (a harness or configuration error, a criterion it says is wrong) is `BLOCKED`.
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
