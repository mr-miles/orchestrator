---
description: "The planner's tickets.json and oracle.json formats and the rules the controller checks. Load when you write or critique a plan."
---

## Skill: Plan format – tickets and oracle

Your plan goes into the change's skeleton (the `legion-workflow` skill shows it and what each tier
requires). `tickets.json` is a **replan**: it adds tickets, and decides about the skeleton's
`scaffold`. Nothing else in the skeleton is yours to change.

```json
{"add": [
   {"into": "builds", "group": 0, "ticket": {
      "id": "T1", "type": "agent:implementer", "goal": "Add the user lookup endpoint",
      "inputs": ["spec", "specs"],
      "work": {
        "soul": "backend-engineer",          // senior-developer | backend-engineer | frontend-engineer
        "requirements": ["REQ-1"],
        "files_modified": ["src/api/users/", "tests/unit/users/", "src/api/routes.py"],  // its area (dir/), exact shared files
        "files_forbidden": ["src/auth/"],
        "context": ["git:app/src/api/__init__.py@<sha>"],   // only what it needs beyond its inputs
        "instructions": "decision-complete instructions; no choices left to the implementer",
        "acceptance": ["GET /users/{id} returns 404 for unknown ids"],
        "verification": ["pytest -q tests/unit/test_users.py"]},
      "criteria": [{"id": "verify:T1", "type": "controller:verify"},
                   {"id": "review:T1", "type": "agent:reviewer"},
                   {"id": "can-merge:T1", "type": "controller:can-merge"}]}},
   {"into": "builds", "group": 0, "join": true, "ticket": {"id": "T2", …}},
   {"into": "builds", "group": 1, "ticket": {"id": "T3", …}}],
 "update": {"scaffold": {"work": {…}}},     // or "cancel": ["scaffold"]
}
```

* **Groups are the order.** `group` is the index of a subtask group under `into`; tickets in one
  group run in parallel (the first opens it, the others `"join": true`), and a group starts from
  the head the earlier groups merged into. Put a build in a later group when it needs another's
  code. Builds go `into: builds`, the skeleton's group for them inside `delivery` – after the
  oracle's tests (R2+), so every build starts with the tests there. Once every build has merged,
  `delivery`'s own criteria judge them together – the oracle's commands at its head, then the
  integration verifier: they are the skeleton's, not yours.
* **Ids** are names: builds `T1`, `T2` …, their criteria `<kind>:<build>` (`verify:T1`,
  `review:T1`, `security:T1`, `can-merge:T1`). Unique across the change.
* **Criteria** run in order after the build, on its commit: `controller:verify` first (its
  `verification` commands, in a fresh verifier), then the judges the tier requires
  (`agent:reviewer`; `agent:security` for a ticket labelled `security`; `agent:architecture` if
  you want one), then `controller:can-merge` last. A failed criterion sends the build back with
  its findings. Missing a criterion the tier requires is refused (the tier policy).
* **Labels** (`"labels": ["security"]`) bring the criteria the tier attaches to them.
* **Inputs** name signed-off outputs (`spec`, `specs`, `oracle`, `environment`): its runs are
  given them at their signed versions, with the delta specs of the build's own `requirements`.
  List in `work.context` only what else the build needs, typically a few files: every entry is
  something the implementer must read.
* You may add tickets to `evaluate` (before the description, `"into": "evaluate"`) or `release`
  – e.g. an `agent:pr-reviewer` criterion of `describe`, or a `release` planner – when the
  change needs them.

**The scaffold.** The skeleton has a `scaffold` ticket that runs after provisioning and before
the oracle and every build: the project's layout, manifest, build, lint and test tooling and their
configuration – or a change to what is there (a new test runner, a restructured layout) that the
tests and builds will rely on. Decide about it (G-P11): give it its `work` (the same format as a
build's, `soul` included; its `verification` must pass without any feature or test the builds
add) with `"update": {"scaffold": {"work": …}}`, or `"cancel": ["scaffold"]` when the repository
already has what the tests and builds need. In a **blank** repository it is required: it fixes
every choice the acceptance tests depend on (paths, runner, configuration) and builds a
placeholder entry point, none of the requirements' behaviour.

`oracle.json`:

```json
{"acceptance_tests": ["tests/acceptance/test_users_api.py"],
 "protected_support": ["tests/acceptance/conftest.py"],
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

**Commands run in a fresh clone, in order:** the repository's setup (`mise run setup`, when its
`mise.toml` has a `setup` task – proposed with the environment), then `acceptance_commands`, then
`integration_commands`, in one verifier sandbox; nothing else is installed or built beforehand.
Dependency installs, code generation and builds the tests need belong in that setup task, never
in these lists: a command that only installs dependencies here is rejected (G-P13), since it
would run after the acceptance commands that need it. Right after the oracle reports, the
controller runs the setup and the acceptance commands this way (preflight): a setup that fails
sends the oracle back; a command the verifier image lacks stops the change for a human.

**Criteria** name the acceptance tests and commands that check them (`tests` ⊆
`acceptance_tests`, `commands` ⊆ `acceptance_commands`, G-P12). Give every criterion at least one
and cover every accepted requirement with at least one criterion.
Write each criterion as observable behaviour (`GET /users/{id} returns 404 for unknown ids`), and
fix an exact format – output text, field order – only where the requirement needs that format: the
oracle tests what a criterion states and nothing more, so a criterion that leaves a shape open
lets any correct implementation pass, and one that pins a shape by accident fails them.

**Delta specs:** one document per accepted requirement, `specs/<REQ-id>.md`: current behaviour,
the change, contracts, and the acceptance criteria that prove it. `spec.md` is the overview tying
them together.

**`files_modified`** is the scope you expect a build to need. Give it as:

* **a directory prefix for the build's own area** (`src/api/users/`, `tests/unit/users/`): the
  code and tests the build owns, including files it creates that you cannot name exactly yet –
  a predicted file list misses the helpers, fixtures and small modules a real change grows;
* **exact paths for shared files** it must also touch – the wiring around its area: route tables,
  registries, `__init__` or index files, configuration, a lockfile or manifest when it adds a
  dependency, generated files and snapshots its change regenerates. These are what plans miss
  most: go through them for every build.

Prefixes overlap more easily than exact paths: builds in one group must still be disjoint (G-P6),
so give parallel builds separate directories, and put a shared file in exactly one of them (or
the builds in sequence). Do not use a broad prefix (`src/`) to avoid thinking about scope – it
defeats G-P6. A build that needs a file outside its scope asks for it (`request_scope`), and a
decision owner approves or denies it while the build waits: scope builds so they rarely need
to.

**Verification surfaces** (R2+, G-P10) declare what the integration commands really reach and the
files that make that true (compose files, schemas, fixtures) – pin what defines the real
interface, not files the builds will change.

Rules the controller checks when you report (dry-run with `report(..., dry_run=true)`): every
requirement served by a build and every build citing one (G-P3); ≥1 verification command per
build (G-P4); a known soul (G-P5); builds in one group have disjoint `files_modified` (G-P6);
nothing in `files_modified` – the scaffold's included – touches the acceptance tests,
`protected_support` or a configured protected path (G-P7); the oracle's strength at R2+ (G-P8,
G-P10, G-P12, G-P13); the scaffold decided (G-P11); and the tier's policy (types, order,
criteria). Acceptance tests are written by the oracle ticket, not builds, and must be **black-box
and out of process**: they run the system as a separate process and check its outputs, so code
under test cannot fake a passing exit code. List any runner configuration the acceptance commands
depend on in `protected_support` – or, if the scaffold writes it, have the scaffold write it
before the oracle and pin it as a verification surface.

The usual pattern: a protected driver script in `protected_support` (e.g.
`tests/acceptance/support/driver.ts`) reads a scenario as JSON on stdin, runs it against the code,
and prints one JSON result on stdout; each acceptance test spawns it as a process and asserts on
that result (the stack skill shows how to spawn one). Long fixtures the tests and several builds
need (a drawn game, a winning sequence, position builders) go in a protected support module,
verified once, that builds import read-only – never leave each build to search for them.
