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
   `files_modified` is the scope the plan expects. If your contract cannot be met without
   changing a file outside it (or in `files_forbidden`), change it – minimally – and list it in
   your report's `data.beyond_scope` as `{"path": …, "reason": …}`: what fails or is missing
   without the change. A reviewer judges every reason, and sends the work back if one does not
   hold. A protected path (the operator's, or a pinned `mise.toml`) is judged strictly: change
   one only when there is no other way, and know that the setup always runs the pinned
   `mise.toml`, so editing it does not change the setup. Never change the oracle's acceptance
   tests: ask `oracle@oracle` if one looks wrong.
3. Run every `verification` command; all must exit 0 without any test or feature the builds
   will add.
4. Commit and push (git protocol).
5. Write `tickets/{{ticket_id}}/evidence.md` with `docs_write` (on a later attempt it exists:
   `docs_read` it and pass its version as `base_version`): one line per verification command,
   the command verbatim in backticks followed by `exit <code>`, then any relevant output.
6. Check with `report(..., dry_run=true)`, then report `DONE` attesting
   `git:{{repo_name}}@<pushed HEAD sha>` and the evidence, with `outputs: {"evidence": …}`.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}).

Findings you must address on this attempt:
{{findings}}
