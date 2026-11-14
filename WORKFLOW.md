# Legion Orchestrator Workflow

This document specifies the orchestrator's workflow: stages, guards, risk tiers and ship gates,
and which soul, skills and prompt each task uses. The agent-facing text itself lives in
[`assets/`](assets/) (see §6), one file per asset, and the controller loads it from there. This
document is not parsed.

Every dispatch records two pins: the SHA-256 of the whole asset set and the SHA-256 of the rendered
soul + skills + prompt. Any edit to an asset is therefore visible in the run record. Guard IDs
(`G-…`, `H-…`) are implemented one-to-one in `legion_orch/workflow/guards.py`; `tests/test_units.py` fails if
this document and the code drift apart, or if a link below points at a missing asset.

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
   sandbox, identity or run with the work they judge.
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
                                    | MCP + git (gateway-injected bearer)     | sandbox create / upload /
                                    |                                         | exec / delete
                     +--------------+-------------+      +--------------------v----------+
                     | openshell sandbox: run 17  | ...  | openshell sandbox: verify 18  |
                     |  agent CLI + soul + prompt |      |  deterministic commands only  |
                     |  /sandbox/work (git clone) |      |  (no agent, git fetch only)   |
                     +----------------------------+      +-------------------------------+
```

* **Sandbox per task.** A sandbox serves exactly one task in one role for its whole life: an
  implementer sandbox is reused across that task's rework cycles; critics, reviewers and verifiers
  always get their own (`openshell sandbox create --detach --name lg-<change>-<run>
  --provider legion-controller --policy … --upload payload:/sandbox`). The payload contains only
  `soul.md`, `prompt.md`, `mcp.json` (MCP URL, no credentials), `run.env` and `run-agent.sh`.
  **No credential ever enters a sandbox, and nothing is ever downloaded from one.**
* **Workload identity (SPIFFE/SPIRE).** Set up once per OpenShell workspace
  (`legion-orch setup-openshell`): a provider profile `legion-controller` whose `token_grant` makes
  the gateway's sandbox supervisor fetch the sandbox's JWT-SVID
  (`spiffe://…/openshell/sandbox/<sandbox-id>`) and exchange it at the controller's
  `POST /oauth/token` (OAuth2 client_credentials, `jwt-spiffe` client assertion). The controller
  verifies the SVID against the SPIRE JWKS (signature, issuer, audience, expiry, sandbox prefix),
  maps the sandbox id to the one task it serves and issues a short-lived, **task-scoped** token
  (sandbox, change, task, role) signed with a key that never leaves controller memory. The gateway
  injects it as `Authorization: Bearer` into requests to the controller only. Every MCP and git
  request is then bound to the sandbox's live run, which must match the token's change, task and
  role; when the run ends, the token is useless.
* **Network policy** (rendered per sandbox from [`policies/agent-policy.template.yaml`](policies/agent-policy.template.yaml);
  every object legion-orch creates in OpenShell – policies, sandboxes, provider profile, provider –
  comes from a template in [`policies/`](policies/README.md) using one shared placeholder
  vocabulary): the model provider, plus
  * `legion_mcp` – `protocol: mcp` on the controller's dedicated MCP port (`/mcp`). The gateway
    parses every JSON-RPC request and allows only the handshake methods and `tools/call` for the
    controller's own tools (exactly the table below); anything else is denied before it reaches
    the controller. (MCP has its own port because OpenShell does not allow an MCP endpoint to
    share a host and port with another protocol.)
  * `legion_git` – `protocol: rest` for `/git/<repo>.git`: fetch, and `git-receive-pack` only for
    roles that push (implementer, oracle); other roles have push in `deny_rules`.

  Verifier sandboxes get `legion_git` (fetch only) and nothing else.
* **Agents push; the controller hosts the remote.** The controller serves its bare hub repository
  over git smart-HTTP (`git http-backend`) at `/git/<repo>.git`. Every live run may fetch. A run
  may push **only** to its own branch
  `refs/heads/legion/<change>/<node>` (enforced by a `pre-receive` hook fed by the controller);
  ref deletion is refused, and judge roles may not push at all. The integration branch and
  `refs/legion/**` are written only by the controller. When a report is accepted the controller
  pins the attested sha under `refs/legion/evidence/<run>` so later force-pushes to a work branch
  can never orphan evidence.
* **Documents** are written by agents directly to the versioned store through MCP `docs_write`.
  Every change has its own namespace `changes/<change-id>/`. Agents use paths relative to their
  change (`plan/spec.md`); the tools resolve them to `changes/<id>/plan/spec.md`, and attestations
  are always stored fully qualified (`file:changes/<id>/plan/spec.md@2`). Agents may read any
  change's documents (cross-change context) except `*/control/logs/`, and write only inside their
  own change and write scope.
* **MCP tools** (every call bound to the calling sandbox's live run via its gateway-injected token):

| Tool | Purpose |
| --- | --- |
| `whoami()` | run id, role, task, cycle, write scope |
| `get_assignment()` | full assignment: contract, context attestations, feedback, base sha, branch |
| `docs_list(prefix)` | list documents and latest versions |
| `docs_read(path, version?)` | read an immutable version (latest by default) |
| `docs_history(path)` | all versions with author/run and sha256 |
| `docs_schema(path)` / `schema_get(name)` | the schema a path is bound to / a schema by name |
| `docs_write(path, content, base_version?)` | create version N+1; content must match the path's schema (G-W3). `base_version` must equal the current latest (optimistic concurrency); omit only for a new path. Scope-checked. Returns `file:<path>@<N>` |
| `verify_attestation(ref)` | check that a `file:`/`git:` reference resolves |
| `progress(message, percent?)` | heartbeat shown live in the UI |
| `report(status, summary, attestations, data?, blocked_reason?, missing_decision?)` | submit the result; guards run synchronously; returns `accepted` or the list of violations |

### 2.1 State and persistence

The document store and the git remote are the **only durable state**. Several changes can run side
by side under one controller; they share `max_parallel` and the git remote but nothing else.

| Where | What |
| --- | --- |
| `changes/<id>/<stage docs>` | change-level agent documents (`intake/`, `plan/`, `critique/`, `oracle/`, `baseline/`, `integration/`, `release/`) |
| `changes/<id>/tasks/<task_id>/` | everything about one build task: `evidence/cycle-<n>.md` and `reviews/<role>-cycle-<n>.md` (agents); `verification.json`, `feedback/<n>.md`, `decisions/<n>.md` (controller) |
| `changes/<id>/control/change.json` | change record: goal, base, tier, risk, approvals, oracle, integration head, ship result |
| `changes/<id>/control/graph.json` | controller task graph: node order, kinds, dependencies, waves, task ids |
| `changes/<id>/control/nodes/<node>.json` | node state; one new version per transition, so the version history is the node's audit trail |
| `changes/<id>/control/runs/<run>.json` | run record: role, soul, cycle, sandbox and its SPIFFE sandbox id, branch, **asset-set sha and rendered-prompt sha**, attempts, violations, report, progress. No credential exists to store. |
| `changes/<id>/control/events/<seq>.json` | append-only event log |
| `changes/<id>/control/logs/<run>.log` | agent output, archived when the agent exits (last 512 KiB); not readable by agents |
| `changes/<id>/control/{approvals,attestations,surfaces,overrides,verification,decisions,feedback,ship}/…` | human decisions and controller evidence |
| git `legion/<id>/integration`, `legion/<id>/<node>`, `refs/legion/evidence/<run>` | integration branch, work branches, pinned evidence |

Rendered prompts are not stored: a run is labelled with the SHA-256 of the asset set and of its
rendered soul + prompt.

On disk, `state_dir` is set once per controller and holds only `state.db` (the store), `git/` (the
bare remote and one integration worktree per change) and `tmp/` (sandbox payloads, deleted after
upload, and live agent logs). `tmp/` is wiped on start. The access-token signing key exists only
in memory, so tokens issued before a restart stop working (gateways simply request new ones). On
restart the controller rebuilds every change from the store; runs that were live lost their agent
process, so they are failed and escalated (no automatic retry), and interrupted verification,
merge or ship steps are resumed.

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
| R2 | `approved_delta_spec`, `protected_oracle`, `task_contract`, `deterministic_verification`, `task_level_independent_review`, `integration_or_real_interface_checks`, `whole_change_acceptance_evidence` | oracle task with verification surfaces, reviewer per task (+security reviewer if `security`/`authorization` ≥ 2, +architecture reviewer if `scope_breadth`/`novelty_uncertainty` ≥ 2), integration verifier, H-3 |
| R3 | `independent_baseline`, `approved_spec_and_oracle`, `protected_oracle`, `deterministic_verification`, `architecture_or_security_review`, `protected_acceptance_tests`, `security_or_e2e_evaluator`, `explicit_human_approval`, `release_observation_plan`, `rollback_or_forward_fix_evidence` | adds baseline, security and architecture reviewers on every task, release planner, and human attestations (§5.12) |

Each gate is reported as `satisfied`, `unsatisfied` or `unevaluable` by the deterministic ship check
(§5.11). Where each gate's evidence comes from:

| Gate | Evidence |
| --- | --- |
| `approved_delta_spec` | every delta spec `plan/specs/<REQ>.md` (and the plan) approved at H-2 against its current version |
| `approved_spec_and_oracle` | all H-2 subjects approved and fresh, the **last** approval before the first build run |
| `protected_oracle` / `protected_acceptance_tests` | acceptance-test blobs unchanged since the oracle commit, except paths a human approved beforehand (G-PP1); acceptance commands pass at head |
| `integration_or_real_interface_checks` | integration verifier PASS, oracle and **surface** commands pass at head, surface pins unchanged or re-affirmed (G-SF1) |
| `independent_baseline` | human `independent-baseline` attestation (pass), recorded **before** the first build run |
| `security_or_e2e_evaluator` | human `security-evaluation` or `e2e-evaluation` attestation; any `fail` blocks |
| `architecture_or_security_review` | architecture or security panel PASS on every task, or a human `architecture-review` attestation; a `fail` attestation blocks |
| `rollback_or_forward_fix_evidence` | human `rollback-evidence` or `forward-fix-evidence` attestation |
| `release_observation_plan` | release planner's `release/plan.md`, or a `release-observation` `not_applicable` attestation (waiver) |

Gates can be added or waived by a human (G-OV1, §5.12); waived and human-judgement gates are
listed in the readiness result.

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
| G-R7 | report | `data` matches the role's `report_schema` (`roles.yaml`): verdicts, findings, results. |
| G-W1 | docs_write | Path resolves inside the run's own change and the role's write scope; never controller-owned paths (`control/**`, `tasks/*/verification.json`, `tasks/*/feedback/**`, `tasks/*/decisions/**`). |
| G-W2 | docs_write | `base_version` equals current latest (or path is new and `base_version` omitted). |
| G-W3 | every store write | The content matches the schema bound to its path ([`bindings.yaml`](assets/schemas/bindings.yaml)); a writer may not pick a different schema. The store records the schema name and sha with the version. Applies to agents and the controller alike. |

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
| G-P1 | `taskgraph.json` and `oracle.json` match the [`taskgraph`](assets/schemas/taskgraph.json) and [`oracle`](assets/schemas/oracle.json) JSON Schemas (also enforced by the store on every write). |
| G-P2 | Task ids unique; `depends_on` refers to existing tasks; graph is acyclic. |
| G-P3 | Traceability: every accepted `REQ-*` is covered by ≥1 task; every task cites ≥1 existing `REQ-*`. |
| G-P4 | Every task has ≥1 `verification` command and ≥1 `acceptance` criterion. |
| G-P5 | Every task's `soul` is an implementer soul. |
| G-P6 | `files_modified` non-empty, relative, no `..`; tasks with no dependency path between them have disjoint `files_modified` (parallel safety). |
| G-P7 | No `files_modified` entry touches a protected path (`oracle.acceptance_tests`, config `protected_paths`). |
| G-P8 | R2+: `oracle.json` lists ≥1 acceptance test path, ≥1 acceptance command and ≥1 criterion mapped to a `REQ-*`. |
| G-P9 | A delta spec `plan/specs/<REQ>.md` exists for every accepted requirement, written by this run. |
| G-P10 | R2+: `oracle.json` declares ≥1 verification surface: unique id, ≥1 command, ≥1 pinned repository path. |

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
| H-2 | Separate decisions, possibly by different approvers ∈ `decision_owners`: the **plan** (`plan/spec.md` + `plan/taskgraph.json`), **each delta spec** `plan/specs/<REQ>.md`, and the **oracle** (`plan/oracle.json` + oracle commit sha; approving it pins every verification surface). The node completes when every subject is approved against current versions; at R3 it also requires an `independent-baseline` pass attestation, so the baseline is recorded before any build run. |
| G-B1 | (dispatch of any build run) every H-2 subject's pins equal the current latest versions – an approval goes stale if its document changes. |

### 5.6 `build` – one node per task in `plan/taskgraph.json`

* **Soul:** the task's `soul` (`senior-developer`, `backend-engineer`, `frontend-engineer`)
* **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`
* **Writes:** `tasks/{{task_id}}/evidence/**`  **Required:** `tasks/{{task_id}}/evidence/cycle-{{cycle}}.md`, git `commit`
* **Sandbox:** created on first dispatch and **reused across rework cycles** (the agent keeps its
  working copy); deleted when the node is `ACCEPTED`.

| ID | Rule |
| --- | --- |
| G-B2 | The attested commit is the current tip of the run's branch in the controller's git remote (i.e. the agent pushed it) and descends from the run's base sha. |
| G-B3 | Changed paths `base..commit` ⊆ `files_modified`; none in `files_forbidden`; none protected. |
| G-B4 | The evidence document names every `verification` command of the task with its exit code. |
| G-PP1 | A protected path may appear in the diff only if a human approved it for **this task** before **this run** was created (`approve_protected_paths`); it is then treated as in scope. |

### 5.7 `verify` – deterministic (controller, no agent)

A fresh sandbox (network: controller git endpoint only, fetch-only grant) clones the candidate
commit and runs each `verification` command; results come back through `sandbox exec` output.
Results are written by the controller to `tasks/{{task_id}}/verification.json` (one version per cycle).

| ID | Rule |
| --- | --- |
| G-V1 | Every command exits 0. Otherwise the node goes to `REWORK` with the failing output as a BLOCKER finding. The verifier sandbox is deleted as soon as the result is recorded. |

### 5.8 `review` – independent panel

Panel: R1 `code-reviewer`; R2 `code-reviewer` (+ `security-reviewer` if security/authorization ≥ 2,
+ `architecture-reviewer` if scope_breadth/novelty_uncertainty ≥ 2); R3 all three. R0 has no panel.
The architecture reviewer uses skill `architecture-checklist` and writes
`tasks/{{task_id}}/reviews/architecture-cycle-{{cycle}}.md`.

* **Souls:** `code-reviewer`, `security-reviewer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format` (+ `security-checklist`)
* **Writes:** `tasks/{{task_id}}/reviews/**`  **Required:** `tasks/{{task_id}}/reviews/{{role}}-cycle-{{cycle}}.md`, git `candidate`

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

### 5.12 Change-level governance (human)

Recorded at any time by a decision owner (UI, `legion-orch govern`, or `POST /api/changes/<id>/action`).
Each is a versioned document under `control/`, so superseding keeps the history.

| Action | Record | Rules |
| --- | --- | --- |
| `attest` | `control/attestations/<kind>.json` | G-AT1 |
| `approve_protected_paths` | `control/approvals/protected-paths/<n>.json` | G-PP1 |
| `approve_surface` | `control/surfaces/<id>.json` | G-SF1 |
| `override` | `control/overrides/<n>.json` | G-OV1 |

| ID | Rule |
| --- | --- |
| G-AT1 | Kind ∈ `independent-baseline, security-evaluation, e2e-evaluation, architecture-review, rollback-evidence, forward-fix-evidence, release-observation`; verdict ∈ `pass, fail, unknown, not_applicable`; ≥1 resolvable source attestation (a waiver still names its bytes); `not_applicable` needs a waiver reason (≥24 chars, >1 word) and only it may carry one; `release-observation` cannot be `pass` (the release plan is the evidence); optional `covers` lists task ids – partial coverage leaves the gate unsatisfied. One record per kind: re-attesting supersedes, and a `fail` is never undone by a pass on a sibling kind. |
| G-SF1 | At ship, every file pinned by a verification surface has the same blob at the integration head as when pinned. A drift leaves `integration_or_real_interface_checks` unsatisfied until a human re-affirms the surface (`approve_surface`, with a reason) – a decision, never a silent re-pin. |
| G-OV1 | `add_gate` adds any known gate to this change's set; `waive_gate` needs a reason (≥24 chars) and resolvable replacement evidence, and is refused for `deterministic_verification`, `explicit_human_approval` and `whole_change_acceptance_evidence`. Tier changes happen only at H-1. |

---

## 6. Assets

Agent-facing text is stored as files under `assets/`, with this naming convention:

```
assets/
  roles.yaml               role -> soul, skills, write scope, required docs, git requirement
  souls/<soul>.md          agent persona, injected verbatim (never summarised)
  skills/<skill>.md        reusable instruction module appended to the prompt
  prompts/<role>.md        task prompt template for the role (file name = role name)
```

* A role's prompt is `prompts/<role>.md`. The rendered prompt is that template followed by each
  skill in the order listed in `roles.yaml`. The soul is delivered separately as the system prompt
  addition.
* Templates use `{{var}}` placeholders. Available variables: `change_id, goal, tier, repo_name,
  base_sha, head_sha, branch, task_id, task_title, task_json, cycle, role, context_refs, feedback,
  candidate_sha, write_scope, required_docs, workdir, task_soul`. An unknown or unbound variable
  fails G-D2. Load-time validation rejects unknown variables, missing files, and files not referenced
  by any role.
* The implementer's soul is chosen per task (`task_soul`) from `souls_allowed` in `roles.yaml`.
* Each role has a handler in `legion_orch/workflow/roles/`: its dispatch policy (`pushes`,
  `judge`, `panel`, `judges_head`, `reuses_sandbox`, `needs_fresh_approval`, which context it
  sees), its role-specific guards, and the transition after an accepted report. Handlers work
  against the narrow `RoleContext` interface, not engine internals. Adding a role means assets
  plus one handler module; the controller refuses to start if a role in `roles.yaml` has none.

**Role bindings:** [`assets/roles.yaml`](assets/roles.yaml)

| Role | Prompt |
| --- | --- |
| `intake` | [`prompts/intake.md`](assets/prompts/intake.md) |
| `planner` | [`prompts/planner.md`](assets/prompts/planner.md) |
| `critic` | [`prompts/critic.md`](assets/prompts/critic.md) |
| `oracle` | [`prompts/oracle.md`](assets/prompts/oracle.md) |
| `baseline` | [`prompts/baseline.md`](assets/prompts/baseline.md) |
| `implementer` | [`prompts/implementer.md`](assets/prompts/implementer.md) |
| `reviewer` | [`prompts/reviewer.md`](assets/prompts/reviewer.md) |
| `security` | [`prompts/security.md`](assets/prompts/security.md) |
| `architecture` | [`prompts/architecture.md`](assets/prompts/architecture.md) |
| `integration` | [`prompts/integration.md`](assets/prompts/integration.md) |
| `release` | [`prompts/release.md`](assets/prompts/release.md) |

**Souls**

* [`intake-analyst`](assets/souls/intake-analyst.md)
* [`architect-planner`](assets/souls/architect-planner.md)
* [`plan-critic`](assets/souls/plan-critic.md)
* [`test-engineer`](assets/souls/test-engineer.md)
* [`baseline-verifier`](assets/souls/baseline-verifier.md)
* [`senior-developer`](assets/souls/senior-developer.md)
* [`backend-engineer`](assets/souls/backend-engineer.md)
* [`frontend-engineer`](assets/souls/frontend-engineer.md)
* [`code-reviewer`](assets/souls/code-reviewer.md)
* [`security-reviewer`](assets/souls/security-reviewer.md)
* [`architecture-reviewer`](assets/souls/architecture-reviewer.md)
* [`integration-verifier`](assets/souls/integration-verifier.md)
* [`release-planner`](assets/souls/release-planner.md)

**Schemas** ([`bindings.yaml`](assets/schemas/bindings.yaml) maps path patterns to these; agents
can fetch them with MCP `docs_schema(path)` / `schema_get(name)`). JSON documents use JSON Schema
2020-12; `x-format: markdown` schemas require headings/patterns; `x-format: text` is free text.
Cross-document rules (coverage, cycles, parallel overlap, resolvable attestations) stay in the
guards, because a per-file schema cannot express them.

* [`approval`](assets/schemas/approval.json)
* [`attestation`](assets/schemas/attestation.json)
* [`change`](assets/schemas/change.json)
* [`delta-spec`](assets/schemas/delta-spec.json)
* [`event`](assets/schemas/event.json)
* [`evidence`](assets/schemas/evidence.json)
* [`graph`](assets/schemas/graph.json)
* [`json`](assets/schemas/json.json)
* [`markdown`](assets/schemas/markdown.json)
* [`node`](assets/schemas/node.json)
* [`oracle`](assets/schemas/oracle.json)
* [`override`](assets/schemas/override.json)
* [`questions`](assets/schemas/questions.json)
* [`readiness`](assets/schemas/readiness.json)
* [`release-plan`](assets/schemas/release-plan.json)
* [`report-baseline`](assets/schemas/report-baseline.json)
* [`report-critique`](assets/schemas/report-critique.json)
* [`report-integration`](assets/schemas/report-integration.json)
* [`report-none`](assets/schemas/report-none.json)
* [`report-review`](assets/schemas/report-review.json)
* [`requirements`](assets/schemas/requirements.json)
* [`risk`](assets/schemas/risk.json)
* [`run`](assets/schemas/run.json)
* [`surface-pin`](assets/schemas/surface-pin.json)
* [`taskgraph`](assets/schemas/taskgraph.json)
* [`text`](assets/schemas/text.json)
* [`verification`](assets/schemas/verification.json)

**Skills**

* [`harness-contract`](assets/skills/harness-contract.md)
* [`docstore-protocol`](assets/skills/docstore-protocol.md)
* [`attestation-format`](assets/skills/attestation-format.md)
* [`git-protocol`](assets/skills/git-protocol.md)
* [`blocked-protocol`](assets/skills/blocked-protocol.md)
* [`risk-signals`](assets/skills/risk-signals.md)
* [`taskgraph-schema`](assets/skills/taskgraph-schema.md)
* [`pre-mortem`](assets/skills/pre-mortem.md)
* [`finding-format`](assets/skills/finding-format.md)
* [`security-checklist`](assets/skills/security-checklist.md)
* [`architecture-checklist`](assets/skills/architecture-checklist.md)
