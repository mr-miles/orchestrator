# Assignment: author protected acceptance tests for change {{change_id}}

Inputs: {{context_refs}}

Working copy `{{workdir}}` on branch `{{branch}}` (base `{{base_sha}}`).

Create exactly the files listed in `oracle.acceptance_tests`, one or more tests per criterion.
Commit them, document the mapping in `oracle/tests.md`, and report `DONE` attesting the commit
`git:{{repo_name}}@<sha>` and the document.

The controller's verifiers run your acceptance commands in a **fresh clone** where only the
repository's setup has run (`mise run setup`, when `mise.toml` defines it; it also ran in your
working copy): anything else you install or build by hand is not there. If the tests need a step
the setup lacks, report `BLOCKED` with it as `missing_decision` (the setup is part of the plan's
environment, not your files).

Before you commit, run the acceptance commands under the repository's real test configuration
(its config files, setup and preload files): every failure must mean "the implementation is
missing", never a harness or configuration error. Do not rely on runtime globals a setup file may
replace. Put fixtures several tests share (known game states, sample data) in a
`protected_support` file instead of rebuilding them in each test, and prefer named results over
positional arrays.

{{scaffold}}

Feedback: {{feedback}}
