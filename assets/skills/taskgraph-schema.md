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
 "acceptance_commands": ["pytest -q tests/acceptance"],
 "integration_commands": ["pytest -q"],
 "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "…"}]}
```

Rules the controller enforces: every REQ covered and every task traced (G-P3); ≥1 verification
command and acceptance criterion per task (G-P4); tasks that may run in parallel (no dependency
path) must have disjoint `files_modified` (G-P6); nothing in `files_modified` may touch the
acceptance tests (G-P7). Acceptance tests are written by the oracle task, not implementers.
