---
description: "The plan/taskgraph.json and plan/oracle.json formats and their rules. Load when you write or critique a task graph or oracle."
---

## Skill: Task graph and oracle schema

`plan/taskgraph.json`:

```json
{"tasks": [{
  "id": "T1",                        // ^T[0-9]+$ unique
  "title": "Add user lookup endpoint",
  "soul": "backend-engineer",        // senior-developer | backend-engineer | frontend-engineer
  "requirements": ["REQ-1"],
  "depends_on": [],
  "files_modified": ["src/api/users.py", "tests/unit/test_users.py"],   // exact paths or dir/ prefixes
  "files_forbidden": ["src/auth/"],
  "context": ["git:app/src/api/__init__.py@<sha>"],   // only what this task needs beyond the defaults
  "instructions": "decision-complete instructions; no choices left to the implementer",
  "acceptance": ["GET /users/{id} returns 404 for unknown ids"],
  "verification": ["pytest -q tests/unit/test_users.py"],
  "scaffold": false                  // true only for a blank repository's scaffold task (G-P11)
}]}
```

A task's runs are always given the accepted requirements, the approved spec, oracle and task graph,
the delta specs of the task's own `requirements`, and its dependencies' commits. List in `context`
only what else the task needs, typically a few files: every entry is something the implementer
must read.

`plan/oracle.json`:

```json
{"acceptance_tests": ["tests/acceptance/test_users_api.py"],
 "protected_support": ["tests/acceptance/conftest.py"],
 "setup_commands": ["uv sync --frozen"],
 "acceptance_commands": ["pytest -q tests/acceptance"],
 "integration_commands": ["pytest -q"],
 "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "…",
               "tests": ["tests/acceptance/test_users_api.py"],
               "commands": ["pytest -q tests/acceptance"]}],
 "surfaces": [{"id": "users-api",
               "description": "HTTP API served by docker compose, checked against the OpenAPI schema",
               "commands": ["docker compose up -d && pytest -q tests/integration/test_api.py"],
               "pins": ["docker-compose.yml", "schema/openapi.yaml"]}]}
```

**Commands run in a fresh clone, in order:** `setup_commands`, then `acceptance_commands`, then
`integration_commands`, in one verifier sandbox – nothing is installed or built beforehand. Put
every step the tests need first (dependency installs such as `bun install --frozen-lockfile` or
`uv sync --frozen`, code generation, a build) in `setup_commands`; a command that only installs
dependencies anywhere else is rejected (G-P13), since it would run after the acceptance commands
that need it. Right after the oracle's tests are merged, the controller runs the setup and
acceptance commands this way (preflight, G-PF1): a setup step that fails, or a command the
verifier image lacks, stops the plan there.

**Criteria** name the acceptance tests and commands that check them (`tests` ⊆
`acceptance_tests`, `commands` ⊆ `acceptance_commands`, G-P12). Give every criterion at least one:
an unlinked criterion is flagged to the human approving the plan, and a linked command's result
at the integration head is shown against its criterion. Cover every accepted requirement with at
least one criterion.

**Delta specs:** one document per accepted requirement, `plan/specs/<REQ-id>.md`: current
behaviour, the change, contracts, and the acceptance criteria that prove it. Each is approved
separately by a human (G-P9). `plan/spec.md` is the overview tying them together.

**Verification surfaces** (R2+, G-P10) declare what the integration commands really reach and pin
the files that make that true (compose files, schemas, fixtures). The controller pins those files
when the oracle is approved. If a task edits a pinned file, the gate stays unsatisfied until a human
re-affirms the surface, so pin what defines the real interface, not files the tasks will change.

Rules the controller enforces: every REQ covered and every task traced (G-P3); ≥1 verification
command and acceptance criterion per task (G-P4); tasks that may run in parallel (no dependency
path) must have disjoint `files_modified` (G-P6); nothing in `files_modified` may touch the
acceptance tests or `protected_support` files (G-P7). Acceptance tests are written by the oracle
task, not implementers, and must be **black-box and out of process**: they run the system as a
separate process and check its outputs, so code under test cannot fake a passing exit code. List
any runner configuration the acceptance commands depend on in `protected_support`.

**Scaffold** (when your assignment says the repository is blank): exactly one task is marked
`"scaffold": true`. It has no dependencies, and every other task depends on it (G-P11). It is
built before the oracle, so it sets up the project and its tooling only, and its `verification`
cannot use the acceptance tests. In a repository that is not blank, no task is a scaffold.

**Fix rounds** (when your assignment says "Fix round n"): revise `plan/taskgraph.json` by
**appending** tasks; every task already built stays exactly as it is, and new tasks use ids no
task has used (G-FR2). A new task may depend on accepted tasks and modify their files; it cites at
least one accepted requirement (G-FR1), and only the new tasks are checked for parallel overlap.
