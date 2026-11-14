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
  "context": ["file:plan/spec.md@2", "git:app/src/api/__init__.py@<sha>"],
  "instructions": "decision-complete instructions; no choices left to the implementer",
  "acceptance": ["GET /users/{id} returns 404 for unknown ids"],
  "verification": ["pytest -q tests/unit/test_users.py"]
}]}
```

`plan/oracle.json`:

```json
{"acceptance_tests": ["tests/acceptance/test_users_api.py"],
 "protected_support": ["tests/acceptance/conftest.py"],
 "acceptance_commands": ["pytest -q tests/acceptance"],
 "integration_commands": ["pytest -q"],
 "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "…"}],
 "surfaces": [{"id": "users-api",
               "description": "HTTP API served by docker compose, checked against the OpenAPI schema",
               "commands": ["docker compose up -d && pytest -q tests/integration/test_api.py"],
               "pins": ["docker-compose.yml", "schema/openapi.yaml"]}]}
```

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
