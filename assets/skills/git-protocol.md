---
description: "Working copy, commits, pushing your branch, rebasing, and what judges do instead. Load before you commit, push or attest a git sha."
---

## Skill: Git protocol

- Your working copy is the `workdir` in your assignment (your current directory), cloned from the
  controller's remote (`origin`). A role that commits is on branch `{{branch}}` at base
  `{{base_sha}}`. Every other role (judges, and roles that only read the tree) has a **detached**
  checkout of the commit it works at, in a partial clone: history is all there, and older file
  contents are fetched when a command needs them (`git diff`, `git log -p`, `git show`). Do not
  create branches or commits there. Credentials are already configured.
- Commit with clear messages: `<ticket id>: <imperative summary>`. Do not rewrite `{{base_sha}}` or
  anything before it, and do not create merge commits.
- When you finish: leave the tree clean (`git status --porcelain` empty), then
  `git push origin HEAD:refs/heads/{{branch}}`. You may only push to your own branch; any other
  ref is refused. Judge roles (reviewers, verifiers) cannot push at all: they skip the push and
  attest the checked-out sha from their assignment.
- Evidence and reports are documents (`docs_write`), never files committed to the repository: a
  committed path outside `files_modified` without a reason in `data.beyond_scope` fails G-B3.
- **Every push is verified.** The controller runs your ticket's `verification` commands at each
  tip you push, in a fresh verifier sandbox, once per commit: check the result with
  `verification_status()` (MCP) and read a failure with `verification_evidence(id)` before you
  report. The commit you report is then usually verified already.
- Attest the pushed `git rev-parse HEAD` as `git:{{repo_name}}@<sha>`. The controller checks it is
  the tip of `{{branch}}` on its remote (G-B2) – report only after the push succeeded.
- If asked to rebase (feedback says "rebase onto <sha>"), `git fetch origin` then
  `git rebase <sha>`, resolve conflicts within your `files_modified` (and the paths you gave a
  reason for), and
  `git push --force origin HEAD:refs/heads/{{branch}}` (force is allowed on your own branch only).
