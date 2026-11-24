# Assignment: intake for change {{change_id}} (attempt {{attempt}})

Goal (from the human operator):

> {{goal}}

Repository `{{repo_name}}` is cloned at `{{workdir}}` (commit `{{base_sha}}`).

1. Call `get_assignment()`. Map the repository: structure, entry points, test setup, conventions.
2. Write `tickets/{{ticket_id}}/requirements.md` with numbered `REQ-<n>` requirements for the
   goal.
3. Score the twelve risk signals in `tickets/{{ticket_id}}/risk.json` (risk signals skill).
4. Put anything a human must decide in `tickets/{{ticket_id}}/questions.md` (call `docs_schema`
   first). A question with a safe default is written resolved: `- [x] Q1: … — assumed:
   <default>`. Leave `- [ ]` only for questions that block the requirements, and then report
   `BLOCKED` with the question as `missing_decision`: any open `- [ ]` fails a `DONE` report
   (G-I3).
5. Report `DONE` with `outputs: {"requirements": …, "risk": …}`, attesting both documents and the
   `git:` files you relied on (an empty repository is fine: attest `git:{{repo_name}}@{{base_sha}}`).
   Check with `report(..., dry_run=true)` first.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}). On a later attempt they
exist: `docs_read` each and pass its version as `base_version`; a document that is still right
may be written again unchanged (it counts as yours).

A decision owner signs off your requirements and risk (H-1). What to address from the last
attempt (their reasons, or the answer to your question):
{{findings}}

If your assignment lists a `selection` input (a design review), write exactly one `REQ-<n>` per selected finding, each line naming its finding id (`F-<n>`) and what to build from the selection's `scope`; no requirement for dropped findings (G-DR4).
