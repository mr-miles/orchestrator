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
   If your contract cannot be met without changing a file outside `files_modified` (or in
   `files_forbidden`, or a protected path), do not change it: ask for it –
   `a2a send --async "/request_scope <paths>: <what fails or is missing without them>"` to the
   orchestrator (`legion-a2a-protocol`) – and go on with the rest while a decision owner decides.
   Approved, the paths are in your `files_modified`; denied, work within your scope or report
   `BLOCKED`. The oracle's acceptance tests are never granted: ask `oracle@oracle` about them.
3. Run every `verification` command; all must exit 0. Report `BLOCKED` with the exact conflict if
   a command cannot pass from inside your scope and your request for more was denied. If a
   protected acceptance test fails for a
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
