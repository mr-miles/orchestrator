## Skill: Git protocol

- Your working copy is the `workdir` in your assignment (your current directory), cloned from the controller's remote (`origin`) and checked
  out on branch `{{branch}}` at base `{{base_sha}}`. Credentials are already configured.
- Commit with clear messages: `<task-id>: <imperative summary>`. Do not rewrite `{{base_sha}}` or
  anything before it, and do not create merge commits.
- When you finish: leave the tree clean (`git status --porcelain` empty), then
  `git push origin HEAD:refs/heads/{{branch}}`. You may only push to your own branch; any other
  ref is refused. Judge roles (reviewers, verifiers) cannot push at all: they skip the push and
  attest the checked-out sha from their assignment.
- Evidence and reports are documents (`docs_write`), never files committed to the repository: a
  commit outside `files_modified` fails G-B3.
- Attest the pushed `git rev-parse HEAD` as `git:{{repo_name}}@<sha>`. The controller checks it is
  the tip of `{{branch}}` on its remote (G-B2) – report only after the push succeeded.
- If asked to rebase (feedback says "rebase onto <sha>"), `git fetch origin` then
  `git rebase <sha>`, resolve conflicts only within your `files_modified`, and
  `git push --force origin HEAD:refs/heads/{{branch}}` (force is allowed on your own branch only).
