# Assignment: the scaffold of change {{change_id}} – {{ticket_goal}} (attempt {{attempt}})

Change {{change_id}}, tier {{tier}}. Working copy `{{workdir}}` on branch `{{branch}}`,
base `{{base_sha}}`. Your sandbox's tools: {{sandbox_tools}}.

You set the project up – its layout, manifest, build, lint and test tooling and their
configuration – or reshape what is there, exactly as the plan says, **before** the acceptance
tests and the builds are written on top of it. Build none of the requirements' behaviour: a
placeholder entry point at most. Every choice the tests and builds depend on (paths, test
runner, configuration) is fixed in the contract below; do not make others.

Your contract (authoritative):

```json
{{work_json}}
```

Context to read before editing: {{context_refs}}

1. Read the context (`file:` entries with `docs_read`; they are not in the working copy).
2. Work within `files_modified`. Run the formatter and linter early.
   If your contract cannot be met without changing a file outside `files_modified` (or in
   `files_forbidden`, or a protected path), do not change it: ask for it –
   `a2a send --async "/request_scope <paths>: <what fails or is missing without them>"` to the
   orchestrator (`legion-a2a-protocol`) – and go on with the rest while a decision owner decides.
   Approved, the paths are in your `files_modified`; denied, work within your scope or report
   `BLOCKED`. The oracle's acceptance tests are never granted: ask `oracle@oracle` about them.
3. Run every `verification` command; all must exit 0 without any test or feature the builds
   will add. Report `BLOCKED` with the exact conflict if one cannot pass from inside your scope
   and your request for more was denied.
4. Commit and push (git protocol).
5. Write `tickets/{{ticket_id}}/evidence.md` with `docs_write` (on a later attempt it exists:
   `docs_read` it and pass its version as `base_version`): one line per verification command,
   the command verbatim in backticks followed by `exit <code>`, then any relevant output.
6. Check with `report(..., dry_run=true)`, then report `DONE` attesting
   `git:{{repo_name}}@<pushed HEAD sha>` and the evidence, with `outputs: {"evidence": …}`.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

Findings you must address on this attempt:
{{findings}}
