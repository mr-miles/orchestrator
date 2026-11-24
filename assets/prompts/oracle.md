# Assignment: author protected acceptance tests for change {{change_id}} (attempt {{attempt}})

Inputs – the signed spec, delta specs and oracle: {{context_refs}}

Working copy `{{workdir}}` on branch `{{branch}}` (base `{{base_sha}}`). Your sandbox's tools:
{{sandbox_tools}}.

Create exactly the files listed in the oracle's `acceptance_tests` (and its
`protected_support`): a test per criterion (a few when it names several cases), each asserting
what the criterion states and nothing it leaves open – no exact-output or snapshot matches unless
the criterion fixes the format. Commit and push them, document the
mapping in `tickets/{{ticket_id}}/tests.md`, and report `DONE` with `outputs: {"tests": …}`,
attesting the commit `git:{{repo_name}}@<sha>` and the document.

Right after you report, the controller runs the repository's setup (`mise run setup`, when
`mise.toml` defines it; it also ran in your working copy) and then the acceptance commands at
your commit in a **fresh clone** (preflight): anything else you installed or built by hand is not
there. Check your tests the same way. If they cannot run that way and the fix is outside your
files (the setup is part of the plan's environment; the commands are in the oracle), report
`BLOCKED` with it as `missing_decision`.

Before you commit, run the acceptance commands under the repository's real test configuration
(its config files, setup and preload files): every failure must mean "the implementation is
missing", never a harness or configuration error. Do not rely on runtime globals a setup file may
replace. Put fixtures several tests share (known game states, sample data) in a
`protected_support` file instead of rebuilding them in each test, and prefer named results over
positional arrays.

Your outputs: {{outputs}}, in your ticket's directory ({{write_scope}}). On a later attempt
`tests.md` exists: `docs_read` it and pass its version as `base_version`.

A decision owner signs off your tests (H-2). What to address from the last attempt (preflight's
findings, or the decision owner's reasons):
{{findings}}
