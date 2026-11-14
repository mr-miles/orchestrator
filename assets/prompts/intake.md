# Assignment: intake for change {{change_id}}

Goal (from the human operator):

> {{goal}}

Repository `{{repo_name}}` is cloned at `{{workdir}}` (commit `{{base_sha}}`).

1. Call `get_assignment()`. Map the repository: structure, entry points, test setup, conventions.
2. Write `intake/requirements.md` with numbered `REQ-<n>` requirements for the goal.
3. Score the twelve risk signals in `intake/risk.json`.
4. Put anything a human must decide in `intake/questions.md`; if it blocks the requirements,
   report `BLOCKED`.
5. Report `DONE` attesting both required documents and the `git:` files you relied on.

Write scope: {{write_scope}}. Required documents: {{required_docs}}.

Feedback from previous attempts (address every point):
{{feedback}}
