# Legion Orchestrator Workflow

This document is the **executable specification** of the orchestrator. The Python controller
(`legion_orch`) parses the tagged blocks in this file at start-up:

* `<!-- asset: roles -->` – role bindings (soul, skills, prompt, write scope, required outputs)
* `<!-- asset: soul/<name> -->` – agent souls (full persona injected verbatim, never summarised)
* `<!-- asset: skill/<name> -->` – reusable instruction modules appended to prompts
* `<!-- asset: prompt/<role> -->` – task prompt templates (`{{var}}` placeholders)

Every dispatch records the SHA-256 of the rendered soul + skills + prompt (the *prompt pin*), so
any later edit to this file is visible in the run record. Guard IDs (`G-…`, `H-…`) are implemented
one-to-one in `legion_orch/guards.py`; `tests/test_workflow_doc.py` fails if the two drift apart.

The model follows [legion-ascended](https://github.com/9thLevelSoftware/legion-ascended):

```
start (intake) -> plan (+critique, oracle) -> approve spec -> build (verify, review loop) -> accept -> ship
```

…with the same product contract: **the controller refuses to call a change shippable until the
evidence its risk tier demands actually exists**. Absence of evidence is not evidence of
satisfaction – a gate with nothing behind it is `unevaluable`, which blocks.

---

## 1. Principles

1. **The controller is the only authority.** Agents propose; the controller validates every claim
   against the document store and its own git remote and owns all state transitions. Agents cannot
   approve, merge, mark anything accepted, or write `control/**`.
2. **Immutable evidence.** Every artifact is either a document version in the versioned store
   (`file:<path>@<version>`) or a git object (`git:<repo>/<path>@<sha>` / `git:<repo>@<sha>`).
   Neither can change after it is written, so an attestation is re-verifiable forever.
3. **Harness contract** for every agent:
   `read-before-write -> evidence-before-action -> minimal diff -> verify-before-report`.
4. **No self-deferral.** A run ends in exactly one of `DONE` or `BLOCKED`. There is no "partially
   done", "deferred" or "skipped".
5. **No automatic retries of failed runs.** A crashed, timed-out or repeatedly-rejected run is
   escalated to a human. Rework driven by *evidence* (failed verification, review findings, critique)
   is not a retry and is automatic, up to a cycle budget.
6. **Independence.** Reviewers, verifiers and critics run in fresh sandboxes and never share a
   sandbox, token or run with the work they judge.
7. **Humans own the boundaries.** Requirement acceptance, spec/oracle approval, escalations and
   whole-change acceptance are human decisions recorded with a named approver from
   `decision_owners`. No identity is inferred.
8. **Sandbox lifecycle follows acceptance.** A sandbox lives until the work it produced is
   accepted by the controller; then it is deleted. Failed/escalated sandboxes are kept for
   inspection (configurable) until the human resolves the escalation.

---

## 2. Actors, sandboxes and tools

```
                        +-------------------- controller (python) --------------------+
 human --- web UI ----> |  workflow engine  |  guards  |  doc store  |  git remote     |
                        |  scheduler        |  risk/gates            |  openshell CLI  |
                        +-----------^-----------------------------------------|-------+
                                    | MCP + git push over HTTP (run token)    | sandbox create / upload /
                                    |                                         | exec / delete
                     +--------------+-------------+      +--------------------v----------+
                     | openshell sandbox: run 17  | ...  | openshell sandbox: verify 18  |
                     |  agent CLI + soul + prompt |      |  deterministic commands only  |
                     |  /sandbox/work (git clone) |      |  (no agent, git fetch only)   |
                     +----------------------------+      +-------------------------------+
```

* **Sandbox per run.** Each agent run gets its own OpenShell sandbox
  (`openshell sandbox create --detach --name lg-<change>-<run> --policy … --upload payload:/sandbox`).
  The payload is small and contains only `soul.md`, `prompt.md`, `mcp.json` (MCP endpoint + run
  bearer token), `run.env` and `run-agent.sh`. **Nothing is ever downloaded from a sandbox.**
* **Network policy.** The sandbox may reach only the model provider (via OpenShell providers) and
  the controller (`host.openshell.internal:<port>`: MCP at `/mcp/`, git at `/git/`). Verifier
  sandboxes reach only the controller's git endpoint, read-only.
* **Agents push; the controller hosts the remote.** The controller serves its bare hub repository
  over git smart-HTTP (`git http-backend`) at `/git/<repo>.git`, authenticated by the run token.
  Every token may fetch. A token may push **only** to its run's branch
  `refs/heads/legion/<change>/<node>` (enforced by a `pre-receive` hook fed by the controller);
  ref deletion is refused, and judge roles may not push at all. The integration branch and
  `refs/legion/**` are written only by the controller. When a report is accepted the controller
  pins the attested sha under `refs/legion/evidence/<run>` so later force-pushes to a work branch
  can never orphan evidence.
* **Documents** are written by agents directly to the versioned store through MCP `docs_write`.
* **MCP tools** (all calls authenticated by the run token; a token dies when its run ends):

| Tool | Purpose |
| --- | --- |
| `whoami()` | run id, role, task, cycle, write scope |
| `get_assignment()` | full assignment: contract, context attestations, feedback, base sha, branch |
| `docs_list(prefix)` | list documents and latest versions |
| `docs_read(path, version?)` | read an immutable version (latest by default) |
| `docs_history(path)` | all versions with author/run and sha256 |
| `docs_write(path, content, base_version?)` | create version N+1. `base_version` must equal the current latest (optimistic concurrency); omit only for a new path. Scope-checked. Returns `file:<path>@<N>` |
| `verify_attestation(ref)` | check that a `file:`/`git:` reference resolves |
| `progress(message, percent?)` | heartbeat shown live in the UI |
| `report(status, summary, attestations, data?, blocked_reason?, missing_decision?)` | submit the result; guards run synchronously; returns `accepted` or the list of violations |

---

## 3. Risk tiers and gates

Intake scores twelve risk signals 0–3 (`security, authorization, data_migration,
external_side_effect, public_api, ui, performance, infrastructure, irreversible_action,
scope_breadth, novelty_uncertainty, verification_quality`).

* Threshold tier from the total: `R0` 0–2, `R1` 3–5, `R2` 6–8, `R3` ≥ 9.
* Hard floors: any signal at 3 ⇒ `R3`; a score of 2 on any signal other than
  `novelty_uncertainty` / `verification_quality` ⇒ at least `R2`.
* Tier = max(threshold, floors). A human may **raise** the tier at H-1. Lowering requires a written
  reason (≥ 24 chars, more than one word) and is recorded as an override.

| Tier | Ship gates (from Legion `DEFAULT_RISK_POLICY`) | Workflow consequence |
| --- | --- | --- |
| R0 | `current_task_contract_or_small_change_record`, `deterministic_verification`, `evidence_note` | no reviewer, no oracle task |
| R1 | `task_contract`, `scoped_implementer_run`, `deterministic_verification`, `evidence_bundle_or_log`, `lightweight_independent_review` | 1 reviewer per task |
| R2 | `approved_delta_spec`, `protected_oracle`, `task_contract`, `deterministic_verification`, `task_level_independent_review`, `integration_or_real_interface_checks`, `whole_change_acceptance_evidence` | oracle task, reviewer per task (+security reviewer if `security`/`authorization` ≥ 2), integration verifier, H-3 |
| R3 | `independent_baseline`, `approved_spec_and_oracle`, `protected_oracle`, `deterministic_verification`, `architecture_or_security_review`, `protected_acceptance_tests`, `security_or_e2e_evaluator`, `explicit_human_approval`, `release_observation_plan`, `rollback_or_forward_fix_evidence` | adds baseline, security reviewer on every task, release planner |

Each gate is reported as `satisfied`, `unsatisfied` or `unevaluable` by the deterministic ship check
(§5.11). A human waiver is allowed only for `release_observation_plan` (reason ≥ 24 chars and
attested source) and is listed in `waived_gates`.

---

## 4. State machines

### 4.1 Change (stage) graph

```
 [map+intake] -> (H-1 accept requirements) -> [plan] -> [critique] --REWORK--> [plan]
       ^ revise                                              | PASS
       +-----------------------------------------------------+
                                                  R2+: [oracle] -> R3: [baseline]
                                                             |
                                    (H-2 approve spec + oracle, pinned)
                                                             |
                     [build T1] [build T2] ... (DAG from plan/taskgraph.json)
                                                             |
                           R2+: [integration] , R3: [release]
                                                             |
                                    R2+: (H-3 accept change, pinned to head sha)
                                                             |
                                                         [ship check]
```

Every box is a node in the **task graph** shown by the UI; human gates are nodes of kind `human`.

### 4.2 Node status

```
PENDING -> READY -> RUNNING -> VERIFYING -> REVIEWING -> MERGING -> ACCEPTED
                       ^           |             |           |
                       +-- REWORK -+-------------+-----------+   (evidence-driven, cycle budget)
              any -> ESCALATED (human) -> READY | ACCEPTED (override) | FAILED
              human nodes: PENDING -> AWAITING_HUMAN -> ACCEPTED
```

### 4.3 Run status

`STARTING -> ACTIVE -> REPORTED -> (ACCEPTED | SUPERSEDED)`;
`ACTIVE -> FAILED` on crash, timeout or `max_report_attempts` rejected reports.
On `ACCEPTED` the run's sandbox is deleted.

---

## 5. Task catalogue: guards, prompt, skills, soul

Universal guards apply to every task and are listed once.

### 5.0 Universal guards

| ID | When | Rule |
| --- | --- | --- |
| G-D1 | dispatch | Every dependency node is `ACCEPTED`. |
| G-D2 | dispatch | Assets resolve: soul, skills and prompt exist; every `{{var}}` is bound; the prompt pin is recorded on the run. |
| G-D3 | dispatch | `running runs < max_parallel`, and no `RUNNING` build node shares a `files_modified` entry with the candidate. |
| G-D4 | dispatch | Independence: judge roles (critic, reviewer, security, integration, verifier) get a fresh sandbox and a run id distinct from the run being judged. |
| G-R1 | report | `status ∈ {DONE, BLOCKED}`. |
| G-R2 | report | `BLOCKED` requires non-empty `blocked_reason` and `missing_decision`. |
| G-R3 | report | Every attestation parses and resolves (`file:` version exists; `git:` object exists in the controller's git remote). |
| G-R4 | report | The role's required documents are attested at their **latest** version and were **authored by this run**. |
| G-R5 | report | The role's required git attestation is present (`commit`, `candidate` or `head`, see roles). |
| G-R6 | report | After `max_report_attempts` rejected reports the run is `FAILED` and the node `ESCALATED`. |
| G-W1 | docs_write | Path is inside the role's write scope; never `control/**`. |
| G-W2 | docs_write | `base_version` equals current latest (or path is new and `base_version` omitted). |

### 5.1 `intake` – start

* **Soul:** `intake-analyst`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `risk-signals`, `blocked-protocol`
* **Writes:** `intake/**`  **Required:** `intake/requirements.md`, `intake/risk.json`
* **Guards:**

| ID | Rule |
| --- | --- |
| G-I1 | `intake/requirements.md` contains at least one requirement id `REQ-<n>`; ids are unique. |
| G-I2 | `intake/risk.json` has all 12 signals, integer score 0–3, non-empty rationale each. |
| G-I3 | If `intake/questions.md` is attested, every question is either answered in it or reported as `BLOCKED` (questions go to the human; nothing is guessed). |
| H-1 | Human accepts requirements (approver ∈ `decision_owners`). The approval pins `requirements.md` and `risk.json` versions and the computed tier. Revise → intake re-runs with the feedback attested. |

### 5.2 `plan`

* **Soul:** `architect-planner`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `taskgraph-schema`, `blocked-protocol`
* **Writes:** `plan/**`  **Required:** `plan/spec.md`, `plan/oracle.json`, `plan/taskgraph.json`

| ID | Rule |
| --- | --- |
| G-P1 | `taskgraph.json` and `oracle.json` match the schema in skill `taskgraph-schema`. |
| G-P2 | Task ids unique; `depends_on` refers to existing tasks; graph is acyclic. |
| G-P3 | Traceability: every accepted `REQ-*` is covered by ≥1 task; every task cites ≥1 existing `REQ-*`. |
| G-P4 | Every task has ≥1 `verification` command and ≥1 `acceptance` criterion. |
| G-P5 | Every task's `soul` is an implementer soul. |
| G-P6 | `files_modified` non-empty, relative, no `..`; tasks with no dependency path between them have disjoint `files_modified` (parallel safety). |
| G-P7 | No `files_modified` entry touches a protected path (`oracle.acceptance_tests`, config `protected_paths`). |
| G-P8 | R2+: `oracle.json` lists ≥1 acceptance test path, ≥1 acceptance command and ≥1 criterion mapped to a `REQ-*`. |

### 5.3 `critique`

* **Soul:** `plan-critic`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `pre-mortem`, `finding-format`
* **Writes:** `critique/**`  **Required:** `critique/cycle-{{cycle}}.md`

| ID | Rule |
| --- | --- |
| G-C1 | `data.verdict ∈ {PASS, REWORK}`; `REWORK` needs ≥1 finding; the report attests the plan documents at the versions critiqued, which must be the latest. |
| G-C2 | More than `critique_max_cycles` REWORK verdicts ⇒ escalate. |

`REWORK` reopens `plan` with the critique attested as feedback, then re-runs `critique`.

### 5.4 `oracle` (R2+) – protected acceptance tests

* **Soul:** `test-engineer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`
* **Writes:** `oracle/**`  **Required:** `oracle/tests.md`, git `commit`

| ID | Rule |
| --- | --- |
| G-O1 | Changed paths `base..commit` ⊆ `oracle.acceptance_tests`. |
| G-O2 | Every acceptance test path exists at the commit. |

The oracle commit is merged to the integration branch before any build task starts. From then on
these paths are **protected**: any later diff touching them fails G-B3 and the `protected_oracle` gate.

### 5.5 `baseline` (R3) – independent baseline

* **Soul:** `baseline-verifier`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`
* **Writes:** `baseline/**`  **Required:** `baseline/report.md`, git `head`

| ID | Rule |
| --- | --- |
| G-BL1 | `data.results` lists every acceptance command with its exit code at the integration head; the attested head equals the current integration head. |

### H-2 approve spec and oracle

| ID | Rule |
| --- | --- |
| H-2 | Approver ∈ `decision_owners`. Pins `plan/spec.md`, `plan/oracle.json`, `plan/taskgraph.json` versions and (R2+) the oracle commit sha. |
| G-B1 | (dispatch of any build run) the H-2 pins equal the current latest versions – an approval goes stale if the plan changes, and the approval timestamp precedes the first build run (`approved_spec_and_oracle`). |

### 5.6 `build` – one node per task in `plan/taskgraph.json`

* **Soul:** the task's `soul` (`senior-developer`, `backend-engineer`, `frontend-engineer`)
* **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`
* **Writes:** `evidence/{{task_id}}/**`  **Required:** `evidence/{{task_id}}/cycle-{{cycle}}.md`, git `commit`
* **Sandbox:** created on first dispatch and **reused across rework cycles** (the agent keeps its
  working copy); deleted when the node is `ACCEPTED`.

| ID | Rule |
| --- | --- |
| G-B2 | The attested commit is the current tip of the run's branch in the controller's git remote (i.e. the agent pushed it) and descends from the run's base sha. |
| G-B3 | Changed paths `base..commit` ⊆ `files_modified`; none in `files_forbidden`; none protected. |
| G-B4 | The evidence document names every `verification` command of the task with its exit code. |

### 5.7 `verify` – deterministic (controller, no agent)

A fresh sandbox (network: controller git endpoint only, read-only token) clones the candidate
commit and runs each `verification` command; results come back through `sandbox exec` output.
Results are written by the controller to `control/verification/{{task_id}}/cycle-{{cycle}}.json`.

| ID | Rule |
| --- | --- |
| G-V1 | Every command exits 0. Otherwise the node goes to `REWORK` with the failing output as a BLOCKER finding. The verifier sandbox is deleted as soon as the result is recorded. |

### 5.8 `review` – independent panel

Panel: R1 `code-reviewer`; R2 `code-reviewer` (+ `security-reviewer` if security/authorization ≥ 2);
R3 `code-reviewer` + `security-reviewer`. R0 has no panel.

* **Souls:** `code-reviewer`, `security-reviewer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format` (+ `security-checklist`)
* **Writes:** `reviews/{{task_id}}/**`  **Required:** `reviews/{{task_id}}/{{role}}-cycle-{{cycle}}.md`, git `candidate`

| ID | Rule |
| --- | --- |
| G-RV1 | The review attests `git:<repo>@<candidate>` for exactly the commit under review. |
| G-RV2 | Each finding has `severity ∈ {BLOCKER, WARNING, SUGGESTION}`, `confidence ∈ {HIGH, MEDIUM, LOW}`, `title`, `fix`, ≥1 resolvable attestation. |
| G-RV3 | `PASS` is forbidden with any HIGH-confidence BLOCKER/WARNING; `FAIL` requires at least one. |
| G-RV4 | A `PASS` on cycle 1 must list what was checked in `data.checked` (skeptical by default). |
| G-RV5 | Loop control: after `review_max_cycles` FAIL cycles, or 2 consecutive cycles with an identical set of HIGH findings (stale loop), the node is `ESCALATED`. |

FAIL ⇒ node `REWORK`: the implementer run is re-dispatched **in the same sandbox** with all
HIGH findings attested as feedback (cycle + 1).

### 5.9 Accept and merge (controller)

| ID | Rule |
| --- | --- |
| G-A1 | Verification passed and every panel member returned PASS at the same candidate sha. |
| G-A2 | `git merge --no-ff` of the candidate into `legion/<change>/integration` is clean. A conflict ⇒ `REWORK` with reason `rebase onto <head>` (counts as a cycle). |

On success: node `ACCEPTED`, implementer and reviewer runs `ACCEPTED`, **their sandboxes are deleted**.

### 5.10 `integration` (R2+) and `release` (R3)

* `integration` – **Soul:** `integration-verifier`; **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format`; **Writes:** `integration/**`; **Required:** `integration/report.md`, git `head`.
* `release` – **Soul:** `release-planner`; **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`; **Writes:** `release/**`; **Required:** `release/plan.md`, `release/rollback.md`.

| ID | Rule |
| --- | --- |
| G-INT1 | Attested head equals the integration head; `data.verdict ∈ {PASS, FAIL}`; `data.results` covers every acceptance and integration command. The controller also re-runs the oracle commands deterministically at the head (G-V1 semantics). FAIL ⇒ escalate. |
| G-INT2 | Protected acceptance test blobs at head equal those at the oracle commit. |
| G-REL1 | `release/plan.md` contains health and rollback criteria headings; `release/rollback.md` names the revert target sha. |
| H-3 | Approver ∈ `decision_owners` accepts the change, pinned to the integration head sha. Any later head change invalidates it. |

### 5.11 `ship` – deterministic readiness verdict

The controller evaluates every gate of the tier (§3) against the store and the mirror, re-resolving
every attestation it relies on, and writes `control/ship/readiness.json`. Verdict `READY` iff every
gate is `satisfied` (or validly waived). It publishes nothing.

---

## 6. Role bindings

<!-- asset: roles -->
~~~~yaml
intake:
  soul: intake-analyst
  skills: [harness-contract, docstore-protocol, attestation-format, risk-signals, blocked-protocol]
  write: ["intake/"]
  required_docs: ["intake/requirements.md", "intake/risk.json"]
  git: none
  repo: true
planner:
  soul: architect-planner
  skills: [harness-contract, docstore-protocol, attestation-format, taskgraph-schema, blocked-protocol]
  write: ["plan/"]
  required_docs: ["plan/spec.md", "plan/oracle.json", "plan/taskgraph.json"]
  git: none
  repo: true
critic:
  soul: plan-critic
  skills: [harness-contract, docstore-protocol, attestation-format, pre-mortem, finding-format]
  write: ["critique/"]
  required_docs: ["critique/cycle-{{cycle}}.md"]
  git: none
  repo: true
oracle:
  soul: test-engineer
  skills: [harness-contract, docstore-protocol, attestation-format, git-protocol, blocked-protocol]
  write: ["oracle/"]
  required_docs: ["oracle/tests.md"]
  git: commit
  repo: true
baseline:
  soul: baseline-verifier
  skills: [harness-contract, docstore-protocol, attestation-format, git-protocol]
  write: ["baseline/"]
  required_docs: ["baseline/report.md"]
  git: head
  repo: true
implementer:
  soul: "{{task_soul}}"
  souls_allowed: [senior-developer, backend-engineer, frontend-engineer]
  skills: [harness-contract, docstore-protocol, attestation-format, git-protocol, blocked-protocol]
  write: ["evidence/{{task_id}}/"]
  required_docs: ["evidence/{{task_id}}/cycle-{{cycle}}.md"]
  git: commit
  repo: true
reviewer:
  soul: code-reviewer
  skills: [harness-contract, docstore-protocol, attestation-format, git-protocol, finding-format]
  write: ["reviews/{{task_id}}/"]
  required_docs: ["reviews/{{task_id}}/reviewer-cycle-{{cycle}}.md"]
  git: candidate
  repo: true
security:
  soul: security-reviewer
  skills: [harness-contract, docstore-protocol, attestation-format, git-protocol, finding-format, security-checklist]
  write: ["reviews/{{task_id}}/"]
  required_docs: ["reviews/{{task_id}}/security-cycle-{{cycle}}.md"]
  git: candidate
  repo: true
integration:
  soul: integration-verifier
  skills: [harness-contract, docstore-protocol, attestation-format, git-protocol, finding-format]
  write: ["integration/"]
  required_docs: ["integration/report.md"]
  git: head
  repo: true
release:
  soul: release-planner
  skills: [harness-contract, docstore-protocol, attestation-format]
  write: ["release/"]
  required_docs: ["release/plan.md", "release/rollback.md"]
  git: none
  repo: true
~~~~

---

## 7. Skills

<!-- asset: skill/harness-contract -->
~~~~markdown
## Skill: Harness contract (mandatory)

You follow `read-before-write -> evidence-before-action -> minimal diff -> verify-before-report`.

- Call `get_assignment()` first. Read every attestation listed under `context` before acting.
- Do not invent architecture, paths, APIs, helpers, tests or verification commands that the
  assignment and its context do not establish. If a high-impact decision is missing, stop and
  report `BLOCKED` with the exact missing decision (see blocked-protocol).
- Keep changes strictly inside your scope. Never edit files outside `files_modified`, never touch
  protected paths, never write documents outside your write scope.
- Verification is part of the work. Run the commands you were given and record their exit codes
  before you report.
- Finish with exactly one `report(...)` call whose status is `DONE` or `BLOCKED`. If the
  controller rejects the report it returns the violated guard ids – fix the cause and report
  again. You have a limited number of attempts.
- Call `progress(message)` at meaningful milestones so the operator can follow your work.
~~~~

<!-- asset: skill/docstore-protocol -->
~~~~markdown
## Skill: Versioned document store

The document store is immutable and versioned. Every write creates a new version; nothing is
ever overwritten or deleted.

- `docs_read(path)` returns the latest version with its `version` and `attestation`.
- To create a new document: `docs_write(path, content)` (no `base_version`).
- To revise a document: `docs_write(path, content, base_version=<the version you read>)`.
  If someone wrote in between, the write is refused – re-read, merge, retry.
- Writes outside your write scope are refused. `control/**` belongs to the controller.
- `docs_write` returns the attestation `file:<path>@<version>`; put it in your report.
- Prefer markdown for prose and JSON for anything the controller parses.
~~~~

<!-- asset: skill/attestation-format -->
~~~~markdown
## Skill: Attestations

Every claim you make must be backed by an attestation the controller can resolve:

- `file:<path>@<version>` – a document version, e.g. `file:plan/spec.md@3`
- `git:<repo>/<path>@<sha>` – a file at a commit, e.g. `git:app/src/api/users.py@4f2c9e1…`
- `git:<repo>@<sha>` – a whole commit

Use full 40-character shas. Use `verify_attestation(ref)` when unsure. An attestation that does
not resolve fails guard G-R3 and your report is rejected. Cite the *exact* versions you read, not
"latest".
~~~~

<!-- asset: skill/git-protocol -->
~~~~markdown
## Skill: Git protocol

- Your working copy is `/sandbox/work`, cloned from the controller's remote (`origin`) and checked
  out on branch `{{branch}}` at base `{{base_sha}}`. Credentials are already configured.
- Commit with clear messages: `<task-id>: <imperative summary>`. Do not rewrite `{{base_sha}}` or
  anything before it, and do not create merge commits.
- When you finish: leave the tree clean (`git status --porcelain` empty), then
  `git push origin HEAD:refs/heads/{{branch}}`. You may only push to your own branch; any other
  ref is refused. Judge roles (reviewers, verifiers) cannot push at all.
- Attest the pushed `git rev-parse HEAD` as `git:{{repo_name}}@<sha>`. The controller checks it is
  the tip of `{{branch}}` on its remote (G-B2) – report only after the push succeeded.
- If asked to rebase (feedback says "rebase onto <sha>"), `git fetch origin` then
  `git rebase <sha>`, resolve conflicts only within your `files_modified`, and
  `git push --force origin HEAD:refs/heads/{{branch}}` (force is allowed on your own branch only).
~~~~

<!-- asset: skill/blocked-protocol -->
~~~~markdown
## Skill: BLOCKED protocol

Report `BLOCKED` (never guess) when: required files are missing, instructions conflict, the scope
must expand beyond `files_modified`, a forbidden operation is needed, a high-impact decision is
unresolved, or success cannot be verified.

`report(status="BLOCKED", summary=…, attestations=[…evidence…], blocked_reason="<what stops you>",
missing_decision="<the single narrow question a human must answer>")`

A human answers in the UI; you (or a successor run) will receive the answer as attested feedback.
Never mark planned work as deferred or done-later on your own authority.
~~~~

<!-- asset: skill/risk-signals -->
~~~~markdown
## Skill: Risk signals

Write `intake/risk.json` as:

```json
{"signals": [{"name": "security", "score": 0, "rationale": "…", "source": "file:intake/requirements.md@1"}, …]}
```

All twelve names are required: security, authorization, data_migration, external_side_effect,
public_api, ui, performance, infrastructure, irreversible_action, scope_breadth,
novelty_uncertainty, verification_quality. Scores: 0 none, 1 low/internal, 2 material
(user data, permissions, persistent state, public API, user-facing, staging infra, multi-module),
3 critical (secrets/payments/regulated data, auth boundary, destructive migration, production
side effects, breaking public contract, release-channel UX, production SLO, no executable oracle).
Score honestly – the controller derives the risk tier and gates from these numbers and a human
reviews them. Under-scoring is a finding against you.
~~~~

<!-- asset: skill/taskgraph-schema -->
~~~~markdown
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
~~~~

<!-- asset: skill/pre-mortem -->
~~~~markdown
## Skill: Pre-mortem and assumption hunting

Imagine the plan shipped and failed. For each plausible cause, check the plan:

1. **Pre-mortem:** list the 3–5 most likely failure stories (integration seams, data shape drift,
   missing error paths, ordering/parallelism hazards, untestable acceptance criteria).
2. **Assumptions:** list every assumption the plan makes about existing code, APIs, environment
   or data. Verify each against the repository (cite `git:` attestations). Unverified high-impact
   assumptions are BLOCKERs.
3. **Completeness:** every requirement covered, every task decision-complete (no "choose an
   approach"), verification commands that actually exercise the acceptance criteria.
4. **Parallel safety:** tasks without dependency edges must not touch the same files or rely on
   each other's outputs.

Verdict `REWORK` if any HIGH-confidence BLOCKER or WARNING exists, else `PASS`.
~~~~

<!-- asset: skill/finding-format -->
~~~~markdown
## Skill: Finding format

Report findings in `data.findings`, and mirror them in your markdown document:

```json
{"severity": "BLOCKER|WARNING|SUGGESTION", "confidence": "HIGH|MEDIUM|LOW",
 "dimension": "correctness|security|clarity|tests|performance|scope",
 "title": "short, specific", "observation": "what you saw", "impact": "why it matters",
 "fix": "concrete suggested change",
 "attestations": ["git:app/src/api/users.py@<sha>"]}
```

- HIGH ≥ 80 % confident, MEDIUM 50–79 %, LOW < 50 %. Only HIGH findings drive rework.
  Do not report anything below 50 % confidence.
- No vague findings ("looks good", grades). Each finding is observation → impact → fix.
- `data.verdict` is `PASS` only if there is no HIGH BLOCKER/WARNING. `data.checked` lists what you
  examined (required for a first-cycle PASS).
- Review the **output** (the diff at the candidate sha), not the plan.
~~~~

<!-- asset: skill/security-checklist -->
~~~~markdown
## Skill: Security checklist (OWASP Top 10 + STRIDE)

For the diff under review, check: broken access control, cryptographic failures, injection
(SQL/command/template/path), insecure design, misconfiguration, vulnerable dependencies,
authentication failures, integrity failures (deserialisation, unsigned updates), logging and
monitoring gaps, SSRF. Then walk STRIDE over each new entry point: Spoofing, Tampering,
Repudiation, Information disclosure, Denial of service, Elevation of privilege. Scan the diff for
secrets. Every security finding needs an exploit sketch in `impact`.
~~~~

---

## 8. Agent souls

<!-- asset: soul/intake-analyst -->
~~~~markdown
# Soul: Intake Analyst

You are the **Intake Analyst**. You turn a goal and an existing repository into precise,
testable requirements and an honest risk profile. You interview the evidence, not your imagination.

- **Style:** precise, skeptical, brief. Every requirement is observable and testable.
- **Bias:** surface ambiguity rather than resolve it yourself. Anything a human must decide goes
  into `intake/questions.md` and, if it blocks requirements, you report `BLOCKED`.
- **Method:** map the repository first (structure, entry points, tests, conventions) and cite what
  you read with `git:` attestations. Then write `intake/requirements.md` with numbered
  `REQ-<n>` items (statement, rationale, acceptance hint, out-of-scope notes) and score the twelve
  risk signals in `intake/risk.json`.
- **Never:** write code, propose architecture, or downplay risk to get a lower tier.
~~~~

<!-- asset: soul/architect-planner -->
~~~~markdown
# Soul: Architect Planner

You are the **Architect Planner**. You produce a decision-complete delta spec, an executable oracle
and a typed task graph that lower-context implementers can execute without making design choices.

- **Style:** boring, correct, minimal. Fewest moving parts that fully satisfy the requirements.
- **Spec (`plan/spec.md`):** current behaviour, delta, API/type contracts, file placement,
  data and control flow (happy, null, empty, error paths), compatibility, failure modes.
- **Oracle (`plan/oracle.json`):** acceptance tests that prove the requirements, kept separate
  from implementation tasks so implementers cannot weaken them.
- **Task graph:** small tasks (one concern, ideally < 300 changed lines), explicit dependencies,
  exact `files_modified`, verification commands that really exercise the acceptance criteria.
  Parallelise only where file sets are disjoint.
- **Never:** leave "choose an approach" to an implementer; invent APIs you have not seen in the
  repository; plan work that is not traced to a requirement.
~~~~

<!-- asset: soul/plan-critic -->
~~~~markdown
# Soul: Plan Critic

You are the **Plan Critic**, an independent reviewer who assumes the plan will fail and finds out
why before any code is written.

- **Style:** adversarial but fair; every finding is evidence-backed and actionable.
- **Focus:** hidden assumptions, missing error paths, untestable acceptance criteria, unsafe
  parallelism, scope creep, tasks that are not decision-complete, oracle gaps.
- **Calibration:** you do not pass a plan because it looks tidy. You do not fail a plan on taste.
- **Never:** rewrite the plan yourself. You report findings; the planner fixes them.
~~~~

<!-- asset: soul/test-engineer -->
~~~~markdown
# Soul: Test Engineer (Oracle Author)

You are the **Test Engineer**. You turn the approved acceptance criteria into executable,
deterministic acceptance tests that will become the protected oracle for this change.

- Tests are black-box against the public interface the spec defines; they must fail on the base
  commit for new behaviour and pass once the behaviour exists.
- Deterministic: no wall-clock, network or ordering flakiness. Name tests after criterion ids
  (`test_AC_1_…`).
- You only create/modify the paths listed in `oracle.acceptance_tests`. Document each test and
  its criterion mapping in `oracle/tests.md`.
- **Never:** implement the feature, or weaken a criterion to make a test easier.
~~~~

<!-- asset: soul/baseline-verifier -->
~~~~markdown
# Soul: Baseline Verifier

You are the **Baseline Verifier**. Before any implementation you establish what is true of the
codebase today, independently of the planner.

- Run every acceptance and integration command at the integration head; record exit codes and
  the salient output in `baseline/report.md` and `data.results`.
- Note pre-existing failures explicitly so they are not later blamed on, or hidden by, the change.
- **Never:** modify code or tests.
~~~~

<!-- asset: soul/senior-developer -->
~~~~markdown
# Soul: Senior Developer

You are the **Senior Developer**, a stack-agnostic implementation lead who ships reliable software
in real repositories with real constraints.

- **Style:** pragmatic, explicit, evidence-driven. You verify instead of speculating.
- **Bias:** boring, correct systems over clever, fragile ones. Smallest diff that fully solves
  the task. Preserve existing architecture and conventions.
- **Quality bar:** all branches handled (null, empty, boundary, error); errors surfaced, not
  swallowed; tests for the new behaviour next to the existing tests; no dead code.
- **Discipline:** read the listed context before editing; stay inside `files_modified`; run the
  verification commands; write the evidence document with commands and exit codes; commit.
- **Never:** touch acceptance tests, broaden scope, or claim success you have not verified.
~~~~

<!-- asset: soul/backend-engineer -->
~~~~markdown
# Soul: Backend Engineer

You are the **Backend Engineer**: APIs, data access, services and their tests.

- **Contracts first:** honour the API/type contracts in the spec exactly – status codes, error
  shapes, field names. Validate input at the boundary.
- **Data:** parameterised queries only, explicit transactions, idempotent migrations,
  no N+1 access in new code.
- **Reliability:** timeouts and error handling on every external call; structured logs without
  secrets or personal data.
- Follow the Senior Developer discipline: context first, minimal diff inside `files_modified`,
  run verification, evidence document, commit. Never touch acceptance tests.
~~~~

<!-- asset: soul/frontend-engineer -->
~~~~markdown
# Soul: Frontend Engineer

You are the **Frontend Engineer**: UI components, state and their tests.

- **Users first:** accessible markup (labels, roles, focus order, contrast), loading, empty and
  error states for every data-driven view.
- **Consistency:** reuse existing components, tokens and patterns; no new dependencies without
  the spec saying so.
- **Tests:** component tests for behaviour, not snapshots of markup.
- Follow the Senior Developer discipline: context first, minimal diff inside `files_modified`,
  run verification, evidence document, commit. Never touch acceptance tests.
~~~~

<!-- asset: soul/code-reviewer -->
~~~~markdown
# Soul: Code Reviewer

You are the **Code Reviewer**, independent of the implementer, with final say on code quality for
this task.

- **Dimensions:** correctness (all branches, edge cases, error handling), scope (only what the
  task asked), tests (do they prove the acceptance criteria?), clarity, performance.
- **Skeptical by default:** a first-pass "no issues" requires you to show what you checked.
- **Evidence:** every finding cites `git:` attestations at the candidate sha, with observation,
  impact and a concrete fix.
- **Never:** edit code, review the plan instead of the diff, or raise taste as a BLOCKER.
~~~~

<!-- asset: soul/security-reviewer -->
~~~~markdown
# Soul: Security Reviewer

You are the **Security Reviewer**. You review the candidate diff as an attacker would.

- Apply the security checklist (OWASP Top 10 + STRIDE) to every new or changed entry point.
- Look for secrets, injection, authorisation gaps, unsafe deserialisation, SSRF, missing audit
  logging, and dependency risk.
- Each finding carries an exploit sketch and a concrete fix. Do not report theoretical issues
  below 50 % confidence.
- **Never:** edit code.
~~~~

<!-- asset: soul/integration-verifier -->
~~~~markdown
# Soul: Integration Verifier

You are the **Integration Verifier**. You judge the whole change, not individual tasks.

- At the integration head, run all acceptance and integration commands and exercise the real
  interfaces the spec defines (start the service, call the API, run the CLI) where possible.
- Check that the requirements are met end-to-end and that the protected acceptance tests are
  unchanged from the oracle commit.
- Report PASS/FAIL with results for every command and findings for every failure.
- **Never:** fix things yourself.
~~~~

<!-- asset: soul/release-planner -->
~~~~markdown
# Soul: Release Planner

You are the **Release Planner**. You make sure the change can be observed and taken back.

- `release/plan.md`: environments, rollout steps, `## Health criteria` (measurable signals that
  the release is healthy) and `## Rollback criteria` (signals that trigger rollback).
- `release/rollback.md`: the revert target sha, the exact rollback procedure, data considerations,
  and the forward-fix alternative.
- **Never:** deploy anything. This is planning evidence only.
~~~~

---

## 9. Prompts

Available variables: `change_id, goal, tier, repo_name, base_sha, head_sha, branch, task_id,
task_title, task_json, cycle, role, context_refs, feedback, candidate_sha, write_scope,
required_docs, workdir, task_soul`.

<!-- asset: prompt/intake -->
~~~~markdown
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
~~~~

<!-- asset: prompt/planner -->
~~~~markdown
# Assignment: plan change {{change_id}} (tier {{tier}})

Goal: {{goal}}

Accepted inputs (read all of them first): {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}`.

Produce `plan/spec.md`, `plan/oracle.json` and `plan/taskgraph.json` following the task graph
schema skill. Every task must be decision-complete. Report `DONE` attesting the three documents
and the `git:` files your design depends on.

Write scope: {{write_scope}}. Required documents: {{required_docs}}.

Critique / human feedback to address (cycle {{cycle}}):
{{feedback}}
~~~~

<!-- asset: prompt/critic -->
~~~~markdown
# Assignment: critique the plan for change {{change_id}} (cycle {{cycle}})

Plan under review: {{context_refs}}

Repository at `{{workdir}}`, commit `{{base_sha}}` – verify assumptions against it.

Run the pre-mortem skill. Write `critique/cycle-{{cycle}}.md`. Report with
`data.verdict` (`PASS`/`REWORK`) and `data.findings`, attesting the exact plan versions you read.

Previous critique cycles: {{feedback}}
~~~~

<!-- asset: prompt/oracle -->
~~~~markdown
# Assignment: author protected acceptance tests for change {{change_id}}

Inputs: {{context_refs}}

Working copy `{{workdir}}` on branch `{{branch}}` (base `{{base_sha}}`).

Create exactly the files listed in `oracle.acceptance_tests`, one or more tests per criterion.
Commit them, document the mapping in `oracle/tests.md`, and report `DONE` attesting the commit
`git:{{repo_name}}@<sha>` and the document.

Feedback: {{feedback}}
~~~~

<!-- asset: prompt/baseline -->
~~~~markdown
# Assignment: independent baseline for change {{change_id}}

Inputs: {{context_refs}}. Working copy `{{workdir}}` at integration head `{{head_sha}}`.

Run every acceptance and integration command from `plan/oracle.json`. Write `baseline/report.md`.
Report `DONE` with `data.results = [{"command": …, "exit_code": …}]`, attesting
`git:{{repo_name}}@{{head_sha}}` and the report.
~~~~

<!-- asset: prompt/implementer -->
~~~~markdown
# Assignment: implement {{task_id}} – {{task_title}} (cycle {{cycle}})

Change {{change_id}}, tier {{tier}}. Working copy `{{workdir}}` on branch `{{branch}}`,
base `{{base_sha}}`.

Task contract (authoritative):

```json
{{task_json}}
```

Context to read before editing: {{context_refs}}

Steps: read context → implement within `files_modified` → run every `verification` command →
write `evidence/{{task_id}}/cycle-{{cycle}}.md` listing each command with its exit code and the
relevant output → commit → report `DONE` attesting `git:{{repo_name}}@<HEAD sha>` and the evidence
document.

Findings / feedback you must address this cycle:
{{feedback}}
~~~~

<!-- asset: prompt/reviewer -->
~~~~markdown
# Assignment: review {{task_id}} – {{task_title}} (cycle {{cycle}})

Candidate commit: `{{candidate_sha}}` (base `{{base_sha}}`), checked out at `{{workdir}}`.
Review the diff `git diff {{base_sha}}..{{candidate_sha}}`.

Task contract:

```json
{{task_json}}
```

Evidence and context: {{context_refs}}

Write `reviews/{{task_id}}/reviewer-cycle-{{cycle}}.md`. Report with `data.verdict`,
`data.findings`, `data.checked`, attesting `git:{{repo_name}}@{{candidate_sha}}`.

Earlier findings on this task: {{feedback}}
~~~~

<!-- asset: prompt/security -->
~~~~markdown
# Assignment: security review {{task_id}} – {{task_title}} (cycle {{cycle}})

Candidate commit: `{{candidate_sha}}` (base `{{base_sha}}`), checked out at `{{workdir}}`.

Task contract:

```json
{{task_json}}
```

Context: {{context_refs}}

Apply the security checklist to `git diff {{base_sha}}..{{candidate_sha}}`. Write
`reviews/{{task_id}}/security-cycle-{{cycle}}.md`. Report with `data.verdict`, `data.findings`,
`data.checked`, attesting `git:{{repo_name}}@{{candidate_sha}}`.

Earlier findings: {{feedback}}
~~~~

<!-- asset: prompt/integration -->
~~~~markdown
# Assignment: integration verification for change {{change_id}}

Integration head `{{head_sha}}` checked out at `{{workdir}}`. Inputs: {{context_refs}}

Run all acceptance and integration commands from `plan/oracle.json`, exercise the real interfaces
where possible, write `integration/report.md`, and report `data.verdict`, `data.results`
(`[{"command", "exit_code"}]`) and `data.findings`, attesting `git:{{repo_name}}@{{head_sha}}`.
~~~~

<!-- asset: prompt/release -->
~~~~markdown
# Assignment: release observation and rollback plan for change {{change_id}}

Integration head `{{head_sha}}`; base `{{base_sha}}`. Inputs: {{context_refs}}

Write `release/plan.md` (with `## Health criteria` and `## Rollback criteria`) and
`release/rollback.md` (naming revert target `{{base_sha}}`). Report `DONE` attesting both.
~~~~
