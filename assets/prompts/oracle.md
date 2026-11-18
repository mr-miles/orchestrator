# Assignment: author protected acceptance tests for change {{change_id}}

Inputs: {{context_refs}}

Working copy `{{workdir}}` on branch `{{branch}}` (base `{{base_sha}}`).

Create exactly the files listed in `oracle.acceptance_tests`, one or more tests per criterion.
Commit them, document the mapping in `oracle/tests.md`, and report `DONE` attesting the commit
`git:{{repo_name}}@<sha>` and the document.

Before you commit, run the acceptance commands under the repository's real test configuration
(its config files, setup and preload files): every failure must mean "the implementation is
missing", never a harness or configuration error. Do not rely on runtime globals a setup file may
replace. Put fixtures several tests share (known game states, sample data) in a
`protected_support` file instead of rebuilding them in each test, and prefer named results over
positional arrays.

{{scaffold}}

Feedback: {{feedback}}
