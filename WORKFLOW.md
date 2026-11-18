# Legion Orchestrator Workflow

This document specifies the orchestrator's workflow: stages, guards, risk tiers and ship gates,
and which soul, skills and prompt each task uses. The agent-facing text itself lives in
[`assets/`](assets/) (see §6), one file per asset, and the controller loads it from there. This
document is not parsed.

Every dispatch records two pins: the SHA-256 of the whole asset set and the SHA-256 of the rendered
soul + skills + prompt. Any edit to an asset is therefore visible in the run record. Guard IDs
(`G-…`, `H-…`) are implemented one-to-one in `legion_orch/workflow/guards/`; `tests/test_units.py` fails if
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
  implementer sandbox is reused across that task's rework cycles; intake, planner and oracle keep
  theirs (stopped between runs with `openshell sandbox stop`, started again for a rework) and
  continue their agent session (`claude --resume`) until the gate that can send their work back
  has passed (intake: H-1; planner and oracle: H-2); critics, reviewers and verifiers always get
  their own (`openshell sandbox create --detach --name lg-<first name>
  --provider legion-mcp --provider legion-git[-push] --policy … --upload payload:/sandbox`). The payload contains only
  `soul.md`, `prompt.md`, `mcp.json` (MCP URL, no credentials), `run.env` and `run-agent.sh`.
  **No credential ever enters a sandbox, and nothing is ever downloaded from one.**
* **Workload identity (SPIFFE/SPIRE).** Set up once per OpenShell workspace
  (`legion-orch setup-openshell`): one provider profile per controller endpoint – `legion-mcp`,
  `legion-git` and `legion-git-push` (below) – each with a `token_grant` that makes
  the gateway's sandbox supervisor fetch the sandbox's JWT-SVID
  (`spiffe://…/openshell/sandbox/<sandbox-id>`) and exchange it at the controller's
  `POST /oauth/token` (OAuth2 client_credentials, `jwt-spiffe` client assertion). The controller
  verifies the SVID against the SPIRE JWKS (signature, issuer, audience, expiry, sandbox prefix),
  maps the sandbox id to the one task it serves and issues a short-lived, **task-scoped** token
  (sandbox, change, task, role) signed with a key that never leaves controller memory. The gateway
  injects it as `Authorization: Bearer` into requests to the controller only. Every MCP and git
  request is then bound to the sandbox's live run, which must match the token's change, task and
  role; when the run ends, the token is useless.
* **Network access** comes from the providers a sandbox attaches; OpenShell adds each attached
  profile's endpoints to the sandbox's effective policy, so the sandbox policy
  ([`policies/agent-policy.template.yaml`](policies/agent-policy.template.yaml)) has no rule for
  the controller. Every object legion-orch creates in OpenShell – policies, sandboxes, provider
  profiles, providers – comes from a template in [`policies/`](policies/README.md) using one
  shared placeholder vocabulary. Agents attach the model provider, plus
  * `legion-mcp` – `protocol: mcp` on the controller's dedicated MCP port (`/mcp`), for the agent
    CLI. The gateway parses every JSON-RPC request and allows only the handshake methods and
    `tools/call` for the controller's own tools (exactly the table below); anything else is
    denied before it reaches the controller. (MCP has its own port because OpenShell does not
    allow an MCP endpoint to share a host and port with another protocol.)
  * `legion-git-push` (roles that push: implementer, oracle) or `legion-git` (every other role) –
    `protocol: rest` for `/git/*.git/**` on the controller's dedicated git port, for git. Both
    allow fetch and `git-receive-pack`; `legion-git` also denies `git-receive-pack`, and a deny
    rule applies across the whole effective policy, so a sandbox attaches exactly one of them.

  The operator UI/API listens on a separate port that is never exposed to sandboxes; the token
  endpoint has its own port for the gateway supervisor.

  Verifier sandboxes attach `legion-git` (fetch only) and nothing else.

  With the environment stage (§5.3a), the policy of a mapped role's sandbox additionally holds
  its profile's network policies (`env_<name>`, each open to the binaries that brought it), and the
  sandbox runs the profile's image and resources.
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
| `docs_write(path, content, base_version?, dry_run?)` | create version N+1; content must match the path's schema (G-W3). `base_version` must equal the current latest (optimistic concurrency); omit only for a new path. Scope-checked. Returns `file:<path>@<N>`. `dry_run`: every check (G-W1..G-W4), nothing written or counted; returns the attestation the write would produce (or `unchanged`) |
| `verify_attestation(ref)` | check that a `file:`/`git:` reference resolves |
| `progress(message, percent?)` | heartbeat shown live in the UI |
| `report(status, summary, attestations, data?, blocked_reason?, missing_decision?, dry_run?)` | submit the result; guards run synchronously; returns `accepted` or the list of violations. `dry_run`: every guard, but no attempt is counted (G-R6) and nothing is recorded or applied; returns `would_accept` and the violations (timeline event `report_checked`) |

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
| `changes/<id>/control/runs/<run>.json` | run record: role, soul, cycle, sandbox and its SPIFFE sandbox id, branch, **asset-set sha, rendered-prompt sha and the template values bound into the prompt**, the agent session (and the run whose session it resumed), attempts, violations, report, progress. No credential exists to store. |
| `changes/<id>/control/logs/<run>.log` (`<run>.2.log`, …) | the agent's whole output, archived when it exits, in parts of ≤1 MiB (a log over 64 MiB keeps its first and last 32 MiB). Each stream-json event carries `legion_ts`, the time the controller received it. Not readable by agents |
| `changes/<id>/control/digests/<run>.json` | per run: tool calls and errors, repeated commands and reads, slowest tool calls, longest model waits, turns and cost |
| `changes/<id>/control/reflections/<run>.md` | the agent's own account of what cost it time and where its guidance failed (§5.13) |
| `changes/<id>/control/timeline/<n>.jsonl` | the controller's events (dispatch, sandbox, verification, rework, human actions), written in chunks |
| `changes/<id>/control/assets/<sha>.json` | the hash of each asset file of an asset set the change's runs used |
| `changes/<id>/control/{approvals,attestations,surfaces,overrides,verification,decisions,feedback,ship,environment}/…` | human decisions and controller evidence (`environment/build.json`: the provisioned environment) |
| git `legion/<id>/integration`, `legion/<id>/<node>`, `refs/legion/evidence/<run>` | integration branch, work branches, pinned evidence |

Rendered prompts are not stored: a run is labelled with the SHA-256 of the asset set and of its
rendered soul + prompt, and keeps the template values it was rendered with. Re-rendering with those
values reproduces the prompt exactly while the pin matches; otherwise the recorded asset hashes
say which files have changed since (`Assets.check_prompt`).

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
 [map+intake] -> (H-1 accept requirements) -> [plan: planner -> critique --REWORK--> planner]
       ^ revise                                                            | PASS
       +-------------------------------------------------------------------+
                         {(H-E1 approve sandbox environment, pinned) -> [provision]}
                                                             |
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
Like a build task with its review, the plan node holds its own critique: it is `REVIEWING` while
the critic judges the planner's documents and `ACCEPTED` only when the critic passes them.
Steps in `{…}` exist only when the controller is configured with an `environment:` section
(§5.3a); the planner then also writes the sandbox environment spec. Without it the graph is
unchanged.
A late integration failure is fixed by a **fix round** (§5.10a): plan, critique, H-2, new build
tasks and integration again, on top of the integration head.

### 4.2 Node status

```
PENDING -> READY -> RUNNING -> VERIFYING -> REVIEWING -> MERGING -> ACCEPTED
                       ^           |             |           |
                       +-- REWORK -+-------------+-----------+   (evidence-driven, cycle budget)
              any -> ESCALATED (human) -> READY | ACCEPTED (override) | FAILED
              human nodes: PENDING -> AWAITING_HUMAN -> ACCEPTED
              plan: PENDING -> READY -> RUNNING -> REVIEWING (critique) -> ACCEPTED, REWORK on REWORK
```

### 4.3 Run status

`STARTING -> ACTIVE -> REPORTED -> (ACCEPTED | SUPERSEDED)`;
`ACTIVE -> FAILED` on crash, timeout or `max_report_attempts` rejected reports.
On `ACCEPTED` the run's sandbox is deleted.

A live run's **stage** records how far its sandbox got: `PREPARING` (OpenShell is creating the
sandbox) -> `LAUNCHING` (the payload is in place; the setup script clones the working copy and
starts the agent) -> `RUNNING` (the agent's first MCP call: it is up and reaches the
controller) -> `EXITED`.

A failed run records its **failure source** and the stage it had reached:

| Source | When |
| --- | --- |
| `AGENT` | The agent failed: its CLI ended with an error result (API error, turn limit), it finished without an accepted report, or `max_report_attempts` reports were rejected. |
| `SYSTEM` | What the agent runs on failed: sandbox creation or upload, the setup script, or the process exited with no result from the agent (crash, command not found, killed). |
| `TIMEOUT` | The run exceeded `limits.run_timeout_s`. |
| `CONTROLLER` | The controller restarted during the run, or failed while applying its report. |

The escalation carries the same source. **Retry** first deletes the failed run's sandbox, then
resumes, so the new run always starts in a fresh sandbox. If OpenShell will not delete it, the
retry still goes ahead and the failure is reported. A fresh implementer sandbox continues from
the task branch as pushed to the controller; work that was never pushed is lost.

Whenever an escalation is resolved with a new run (**Retry**, or an answer that resumes it), the
node's earlier finished runs are superseded and their sandboxes deleted, after their agents
reflect – except a sandbox the node keeps for rework. When a change ends (shipped, rejected or
abandoned), every sandbox it still has is deleted.

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
| G-R4 | report | The role's required documents are attested at their **latest** version and were **written by this run** – or written again unchanged by it, which creates no new version (G-W2) but counts as its own. |
| G-R5 | report | The role's required git attestation is present (`commit`, `candidate` or `head`, see roles). |
| G-R6 | report | After `max_report_attempts` rejected reports the run is `FAILED` and the node `ESCALATED`. Dry runs (`report(..., dry_run=true)`) are not attempts. |
| G-R7 | report | `data` matches the role's `report_schema` (`roles.yaml`): verdicts, findings, results. |
| G-R8 | report | Size limits: summary and reasons ≤ 2,000 characters, ≤ 100 attestations, `data` ≤ 256 KiB (report schemas also cap item counts and text lengths); the checks of one report are time-limited (`limits.report_check_timeout_s`). Applying an accepted report is transactional: any controller error restores the state and escalates. |
| G-W1 | docs_write | Path resolves inside the run's own change and the role's write scope; never controller-owned paths (`control/**`, `tasks/*/verification.json`, `tasks/*/feedback/**`, `tasks/*/decisions/**`). |
| G-W2 | docs_write | `base_version` equals current latest (or path is new and `base_version` omitted). Content identical to the latest version creates no version: that version is returned (`unchanged: true`) and costs no G-W4 quota. |
| G-W3 | every store write | The content matches the schema bound to its path ([`bindings.yaml`](assets/schemas/bindings.yaml)); a writer may not pick a different schema. The store records the schema name and sha with the version. Applies to agents and the controller alike. |
| G-W4 | docs_write | Per-run quota: at most `limits.max_docs_per_run` document versions (default 200) and `limits.max_doc_bytes_per_run` bytes (default 50 MB). Refused writes say which limit was hit. |

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

* **Soul:** `architect-planner`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `taskgraph-schema`, `sandbox-environment`, `blocked-protocol`, `language-preferences`
* **Writes:** `plan/**`  **Required:** `plan/spec.md`, `plan/oracle.json`, `plan/taskgraph.json`; with the environment stage also `plan/environment.json` (§5.3a)

| ID | Rule |
| --- | --- |
| G-P1 | `taskgraph.json` and `oracle.json` match the [`taskgraph`](assets/schemas/taskgraph.json) and [`oracle`](assets/schemas/oracle.json) JSON Schemas (also enforced by the store on every write). |
| G-P2 | Task ids unique; `depends_on` refers to existing tasks; graph is acyclic. |
| G-P3 | Traceability: every accepted `REQ-*` is covered by ≥1 task; every task cites ≥1 existing `REQ-*`. |
| G-P4 | Every task has ≥1 `verification` command and ≥1 `acceptance` criterion. |
| G-P5 | Every task's `soul` is an implementer soul. |
| G-P6 | `files_modified` non-empty, relative, no `..`; tasks with no dependency path between them have disjoint `files_modified` (parallel safety). |
| G-P7 | No `files_modified` entry touches a protected path (`oracle.acceptance_tests`, `oracle.protected_support`, config `protected_paths`). |
| G-P8 | R2+: `oracle.json` lists ≥1 acceptance test path, ≥1 acceptance command and ≥1 criterion mapped to a `REQ-*`. |
| G-P9 | A delta spec `plan/specs/<REQ>.md` exists for every accepted requirement, written by this run. |
| G-P10 | R2+: `oracle.json` declares ≥1 verification surface: unique id, ≥1 command, ≥1 pinned repository path. |

### 5.3 critique (part of `plan`)

The critic judges the plan node while it is `REVIEWING` (after each planner report), like a
review-panel member a build task: a fresh sandbox, its verdict stored on the node
(`reviews["<cycle>:critic"]`), the cycle being the plan's.

* **Soul:** `plan-critic`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `pre-mortem`, `finding-format`, `language-preferences`
* **Writes:** `critique/**`  **Required:** `critique/cycle-{{cycle}}.md`

| ID | Rule |
| --- | --- |
| G-C1 | `data.verdict ∈ {PASS, REWORK}`; `REWORK` needs ≥1 finding; the report attests the plan documents at the versions critiqued, which must be the latest. |
| G-C2 | A REWORK verdict in plan cycle `critique_max_cycles` (plus cycles a human granted) ⇒ the plan node escalates; answering grants one more cycle. |

`PASS` accepts the plan node and creates the build nodes from `plan/taskgraph.json`. `REWORK` sends
the plan back to the planner (a new cycle, the critique attested as feedback); it is critiqued
again when the planner reports.
With the environment stage the critique also covers `plan/environment.json` (it must attest the
latest version, and the `pre-mortem` skill checks that every command can run in its role's
sandbox); `REWORK` sends both back to the planner.

### 5.3a Sandbox environment (when configured)

Part of `plan`: the planner also writes `plan/environment.json` (schema
[`environment`](assets/schemas/environment.json)), given the library catalogue in its
assignment (`{{environment}}`). The report must attest the spec's latest version; a rework may
leave it as an earlier planner run wrote it. The guards below are checked with the plan's.

The planner specifies the sandboxes for everyone who runs or works with the code: named
**profiles** (tools, optional extra network per tool, optional memory/vCPU/disk), and which
role (or the deterministic `verifier`) runs in which profile. Profiles are composed from the
**library** (`environment.library_dir`, default [`environments/`](environments/README.md)):
tools installed with mise, each declaring its binaries and the network policies it needs, and
network policies, credentialless endpoint lists that become rules in the sandbox policy. Selecting a tool
brings its network, open to that tool's binaries only; a profile adds per-tool extras or goes
`offline`. The spec
may propose new library entries inline; they apply to this change once approved, and an operator
can promote them into the library: the change-level action `promote_proposal` (`legion-orch library promote`,
by a decision owner), which copies the entry from the H-E1-approved spec version and never
overwrites a library entry.

| ID | Rule |
| --- | --- |
| G-E1 | The planner's report attests the latest `plan/environment.json`. Every profile's tools, and the network policies they bring or the profile adds per tool, resolve to a library entry or a proposal (proposals may not shadow library names; `name@version` only for single-tool entries); every policy in use is reachable by at least one binary of the tools using it; every mapped profile exists; no two tools of a profile set the same environment variable to different values. |
| G-E2 | No network-policy endpoint targets the controller host (`server.public_host`, `server.token_host`) or loopback, and no tool environment variable has a credential-like name (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, …): the image environment is readable by every agent. Network policies cannot carry credentials (the schema has no field for them). |
| G-E3 | Requested resources are within `environment.max_memory_mib`, `max_vcpus`, `max_disk_mib`. |
| G-E4 | Only roles that run after provisioning, and `verifier`, are mapped (`intake`, `planner`, `critic` always use the default sandbox). |
| H-E1 | After a critique PASS, a human approves the environment (approver ∈ `decision_owners`); the approval pins the `plan/environment.json` version. Revise → `plan` is reworked with the feedback attested and critiqued again. H-2 revise and a critique-budget rework drop the approval and the build. |

### `provision` – build the environment (controller, no agent)

The approved spec version is resolved against the library again and built: one image per
(profile, base image) – `FROM sandbox.image` for agents, `FROM sandbox.verifier_image` for the
verifier – with the profile's tools installed by mise under `/usr/local/share/mise` (read-only in
every sandbox) and their `env` settings as image `ENV`, via `podman build`, tagged by content (`localhost/legion-env:<hash>`, or pushed to
`environment.registry`). Identical images are reused, across changes too. Network policies
carry no credentials, so nothing is created for them in OpenShell: each is resolved into a
`network_policies` entry (`env_<name>`, open to the binaries that brought it) recorded with the
build and added to the policy of every sandbox of the role. CPU and memory become `sandbox create --cpu <vcpus>` and `--memory <n>Mi`; disk,
which has no CLI flag, is rendered through `policies/sandbox-resources.template.yaml` into
`--driver-config-json`. The result is recorded in `control/environment/build.json` and the
change record; the build log in `control/logs/provision.log`.

| ID | Rule |
| --- | --- |
| G-PV1 | Every image a mapped profile needs is built or found. Otherwise the node escalates `provision_failed` (retry provisions again). |
| G-E5 | (dispatch of a mapped role, and every verifier sandbox) the H-E1 pin still equals the latest `plan/environment.json`. |

From then on every sandbox of a mapped role is created from its profile's image, with its
network policies in the sandbox policy and its resources; unmapped roles keep the defaults.

The build is fixed at provisioning: later library edits do not reach it. A decision owner can
rebuild it with the change-level action `reprovision` (a reason of ≥24 chars), which resolves the
H-E1-approved spec against the current library again, e.g. after a library entry was fixed. It
does not change the spec: a tool or policy the spec does not name needs a plan revision and a
new H-E1. New runs of mapped roles wait from the request on; provisioning starts once their live
runs have ended and nothing is verifying or merging, and the old build stays in place until the
new one is accepted. Sandboxes kept across cycles by mapped roles (implementers) are then
released, after their agent reflects, so each node's next run starts fresh in the new build and
continues from its pushed branch. Unmapped roles' sandboxes are left alone: the planner keeps
its session to H-2 through H-E1 and provisioning.

### 5.4 `oracle` (R2+) – protected acceptance tests

* **Soul:** `test-engineer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`, `language-preferences`
* **Writes:** `oracle/**`  **Required:** `oracle/tests.md`, git `commit`

| ID | Rule |
| --- | --- |
| G-O1 | Changed paths `base..commit` ⊆ `oracle.acceptance_tests` ∪ `oracle.protected_support`. |
| G-O2 | Every acceptance test path exists at the commit. |

The oracle commit is merged to the integration branch before any build task starts. From then on
these paths are **protected**: any later diff touching them fails G-B3 and the `protected_oracle` gate.

### 5.5 `baseline` (R3) – independent baseline

What is true of the code before any build: which acceptance tests fail (as they should, until the
change is built) and which failures were already there. The integration verifier is given it.

* **Soul:** `baseline-verifier`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`
* **Writes:** `baseline/**`  **Required:** `baseline/report.md`, git `head`

| ID | Rule |
| --- | --- |
| G-BL1 | `data.results` lists every acceptance command with its exit code at the integration head; the attested head equals the current integration head. |
| G-BL2 | The controller then runs the same commands at the head itself (G-V1 semantics; failures are expected and recorded, not reworked). At H-2 the oracle subject lists, for the approver to weigh, every acceptance command that already passes (it cannot show that the change works) and every result that differs from the baseline verifier's. |

### H-2 approve spec and oracle

| ID | Rule |
| --- | --- |
| H-2 | Separate decisions, possibly by different approvers ∈ `decision_owners`: the **plan** (`plan/spec.md` + `plan/taskgraph.json`), **each delta spec** `plan/specs/<REQ>.md`, and the **oracle** (`plan/oracle.json` + oracle commit sha; approving it pins every verification surface). The node completes when every subject is approved against current versions; at R3 it also requires an `independent-baseline` pass attestation, so the baseline is recorded before any build run. |
| G-B1 | (dispatch of any build run) every H-2 subject's pins equal the current latest versions – an approval goes stale if its document changes. |

### 5.6 `build` – one node per task in `plan/taskgraph.json`

* **Soul:** the task's `soul` (`senior-developer`, `backend-engineer`, `frontend-engineer`)
* **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`, `language-preferences`
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
| G-V2 | A command that exits 127 (not found in the verifier image) is not the agent's to fix: the node escalates `verify_unrunnable` instead of spending a rework cycle (oracle commands at integration likewise). **Retry** re-runs verification at the same candidate, e.g. after the image is fixed. |

Exit codes are only as trustworthy as the code a command runs: a test process that imports the
code under test can be made to exit 0. So the protected acceptance tests must be **black-box and
out of process** (they run the system as a separate process and check its outputs), and runner
configuration they depend on is listed in `oracle.protected_support` and protected like the tests
(G-P7, G-O1, G-INT2). The test-engineer soul requires this, the critic checks it, and humans
approve it at H-2. Per-task verification commands are a faster, weaker check backed by review.

### 5.8 `review` – independent panel

Panel: R1 `code-reviewer`; R2 `code-reviewer` (+ `security-reviewer` if security/authorization ≥ 2,
+ `architecture-reviewer` if scope_breadth/novelty_uncertainty ≥ 2); R3 all three. R0 has no panel.
The architecture reviewer uses skill `architecture-checklist` and writes
`tasks/{{task_id}}/reviews/architecture-cycle-{{cycle}}.md`.

* **Souls:** `code-reviewer`, `security-reviewer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format` (+ `language-preferences` for `reviewer`, `security-checklist` for `security`)
* **Writes:** `tasks/{{task_id}}/reviews/{{role}}-*` (each panel member only its own files)  **Required:** `tasks/{{task_id}}/reviews/{{role}}-cycle-{{cycle}}.md`, git `candidate`

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
| G-H1 | Before a merge, and before dispatching a run at the integration head (integration, baseline, release), the integration branch is at the head the controller recorded. Otherwise commits were added outside the workflow: they are **not adopted** (they bypass verification and review); the node escalates `head_moved`, naming both shas, and a retry after the branch is reset to the recorded head resumes it (the merge, or a fresh run). |

On success: node `ACCEPTED`, implementer and reviewer runs `ACCEPTED`, **their sandboxes are deleted**.

### 5.10 `integration` (R2+) and `release` (R3)

* `integration` – **Soul:** `integration-verifier`; **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format`; **Writes:** `integration/**`; **Required:** `integration/report.md`, git `head`. **Inputs** add every task's evidence and, at R3, the baseline report and the controller's baseline run.
* `release` – **Soul:** `release-planner`; **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`; **Writes:** `release/**`; **Required:** `release/plan.md`, `release/rollback.md`.

| ID | Rule |
| --- | --- |
| G-INT1 | Attested head equals the integration head; `data.verdict ∈ {PASS, FAIL}`; `data.results` covers every acceptance and integration command. The controller also re-runs the oracle commands deterministically at the head (G-V1 semantics). FAIL ⇒ escalate. |
| G-INT2 | Protected acceptance test and `protected_support` blobs at head equal those at the oracle commit (except paths a human approved beforehand, G-PP1). |
| G-REL1 | `release/plan.md` contains health and rollback criteria headings; `release/rollback.md` names the revert target sha. |
| H-3 | Approver ∈ `decision_owners` accepts the change, pinned to the integration head sha. Any later head change invalidates it. |

### 5.10a Fix rounds

A late failure – the integration verifier reports FAIL, or the oracle commands fail at the head
(`integration_failed`) – is fixed by **planning new work on top of the integration head**, as if
the change started from existing code. Accepted tasks are never reopened: a round adds tasks.
Round 1 is the original plan; each fix round `n ≥ 2` goes through plan, critique, H-2, build and
integration again. A round **revises the plan documents in place**: it appends tasks to
`plan/taskgraph.json` (and revises the spec, a delta spec, the oracle or the environment only
when the fix needs it); the store's versions and the approval pins record what each round added.

**Start.** On an `integration_failed` escalation a decision owner chooses **Plan a fix**
(`plan_fix`, optional `comment`). Refused after `limits.fix_rounds` rounds (default 2); the change
can then still be answered, overridden at the gates or abandoned. The round is recorded in
`control/rounds/<n>.json` (approver, comment, the head it starts from, and the failure: the
integration run's report documents and the failed verification record). The integration node's
escalation is cleared, its finished runs are superseded and their sandboxes deleted (after they
reflect); it returns to `PENDING`, a new cycle, and waits for H-2 as well as for its tasks. The
plan node starts the round (a new cycle) and H-2 returns to `PENDING`.

**Plan (planner, fix mode).** A fresh planner sandbox and session, at the integration head. Its
assignment adds the round (`{{fix_round}}`: the failure, the comment, every task already built
with its accepted commit) and gives the approved documents at their approved versions, the round
record and the failure documents as context; feedback starts at the round. It revises:

* `plan/taskgraph.json` – **appending** one or more tasks; every task already built stays exactly
  as it is. Each new task's `instructions` say which failure it fixes and why, and its `context`
  cites the integration report. Task ids stay plain (`T5`) and are never reused across rounds;
  the graph records each task's round;
* `plan/spec.md` or delta specs `plan/specs/<REQ>.md` where the cause is a shared interface or
  contract (the planner writes every required document and delta spec again, as on any rework:
  an unchanged write counts as its own, G-R4/G-P9);
* `plan/oracle.json` **only if** the acceptance tests themselves must change;
* `plan/environment.json` **only if** the sandboxes must change.

| ID | Rule |
| --- | --- |
| G-FR1 | Requirements are unchanged: nothing under `intake/` is written and the H-1 pins stand. Every new task cites ≥1 accepted `REQ-*`; a round need not cover every requirement (earlier tasks do). |
| G-FR2 | The revised task graph keeps every task that has a build node exactly as built and appends at least one task with an id no task has used. The whole graph meets G-P1 and G-P2; the new tasks meet G-P4–G-P7. A new task may depend on accepted tasks and modify their files; only the new tasks are checked for parallel overlap (G-P6), since the accepted ones have finished. At R2+ a revised oracle meets G-P8 and G-P10. |

**Critique.** The critic judges the round with the integration failure as input: the appended
tasks must together address every failure it names, each saying which one it fixes and why. G-C1
as usual (the latest plan documents); G-C2 counts the round's own cycles.

**After the critique passes**, what the round changed runs again:

* **Environment.** Unchanged ⇒ the H-E1 approval and the build stand. Changed (the latest
  `plan/environment.json` is not the one H-E1 pinned) ⇒ H-E1 approves the new version and
  `provision` builds it, as in round 1 (§5.3a).
* **Oracle.** Unchanged ⇒ the oracle commit, protected paths and surface pins stand. Changed ⇒ the
  `oracle` node takes a new cycle: the test engineer works at the integration head (its own branch
  for the round) and its commit must stay within the (new) protected paths (G-O1, G-O2); it is
  merged and becomes the oracle commit that G-INT2 and `protected_oracle` compare against, and its
  approval at H-2 re-pins the surfaces.
* **Baseline (R3).** Every round runs the baseline again at the integration head, after the
  oracle (changed or not) and before the round's builds: it records what fails before the fix. A
  decision owner attests `independent-baseline` again: H-2 only completes with an attestation
  newer than the current baseline run.
* **Build nodes** are added for the appended tasks (`build:<id>`, the round recorded on the node);
  integration waits for them too.

**H-2 for the round.** The gate returns to `AWAITING_HUMAN` once the plan, oracle, baseline and
environment are accepted. The revised task graph makes the `plan` subject stale, as does any
revised spec, delta spec or oracle (G-B1); unchanged subjects keep their approval. **Revise**
sends the round back to the planner with the feedback (its build nodes are withdrawn; earlier
rounds stand). **Restart plan** (`restart`) restarts the whole plan as a round-1 revise does:
every build node, the integration branch, the oracle and the surface pins start over.

**Build.** The round's tasks are dispatched from the integration head as usual: implementer,
verification, review, merge (§5.6–5.9). Earlier rounds' tasks stay as accepted.

**Integration, H-3, ship.** Integration depends on every build task of every round and runs again
at the new head, given every round's record and failure as context. H-3 and the ship gates cover
all rounds' tasks: each task's scope, evidence and review are checked against its own base and
accepted commit, and the task contract is the approved `plan/taskgraph.json`, which lists every
round's tasks. Approvals and the independent baseline must precede the **current round's** first
build (`approved_spec_and_oracle`, `independent_baseline`); earlier rounds' builds were dispatched
only while every approval of their time was fresh (G-B1).

**Not covered.** A FAIL from `release` (R3) does not offer a round.

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
| `retrospective` | node `retrospective` (§5.13); optional `focus` in `control/focus/` | only once the change has ended; runs by itself then unless `retrospective.auto: false` |

| ID | Rule |
| --- | --- |
| G-AT1 | Kind ∈ `independent-baseline, security-evaluation, e2e-evaluation, architecture-review, rollback-evidence, forward-fix-evidence, release-observation`; verdict ∈ `pass, fail, unknown, not_applicable`; ≥1 resolvable source attestation (a waiver still names its bytes); `not_applicable` needs a waiver reason (≥24 chars, >1 word) and only it may carry one; `release-observation` cannot be `pass` (the release plan is the evidence); optional `covers` lists task ids – partial coverage leaves the gate unsatisfied. One record per kind: re-attesting supersedes, and a `fail` is never undone by a pass on a sibling kind. |
| G-SF1 | At ship, every file pinned by a verification surface has the same blob at the integration head as when pinned. A drift leaves `integration_or_real_interface_checks` unsatisfied until a human re-affirms the surface (`approve_surface`, with a reason) – a decision, never a silent re-pin. |
| G-OV1 | `add_gate` adds any known gate to this change's set; `waive_gate` needs a reason (≥24 chars) and resolvable replacement evidence, and is refused for `deterministic_verification`, `explicit_human_approval` and `whole_change_acceptance_evidence`. Tier changes happen only at H-1. |

### 5.13 Reflection and retrospective

**Reflection (automatic).** When an agent's sandbox is deleted because its work is done (accepted,
superseded, or its hold ended – not after a failure), the controller first resumes the agent's
session (`--resume`) for one reflection turn: what cost it time, which instructions were missing or
wrong, tool and environment friction, mistakes (for an implementer: each finding raised against its
work) and suggestions. The answer is stored as `control/reflections/<run>.md`. The agent still has
its whole context, which a later retrospective does not. If the agent runs in a new session instead
(the resume did not take), the reflection says so at the top and the timeline notes it; a rework
run that does not continue its session is noted the same way. Best-effort: a missing session, an error
or `retrospective.reflect_timeout_s` skips it, and the deletion goes ahead. Turn it off with
`retrospective.reflect: false`; `reflect_skip_roles` lists roles that never reflect – by default
the judges (`reviewer`, `security`, `architecture`, `critic`): what a judge missed shows in later
outcomes, not in its own account (see `findings.json` below).

**Retrospective.** When a change ends (`READY`, `NOT_READY` or `ABANDONED`), node
`retrospective` is added and dispatched even though the change is no longer active – once the
reflections of the sandboxes released at the end have finished, as they are part of its material.
`retrospective.auto: false` leaves it to a decision owner, who can also ask for another at any time
after the change has ended (UI, or `change_action retrospective` with an optional `focus`).

* **Soul:** `retrospective-analyst`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`
* **Material:** `<payload>/retro/` – reflections, digests, whole logs, timeline, run and node
  records, the change's documents, the current assets and a prompt check per run (agents cannot
  read `control/logs/` through MCP). Its context lists earlier changes' retrospectives.
* **Review ledger:** `retro/findings.json`, built by the controller from the node and run records
  when the retrospective is dispatched: per task each candidate (sha, verification, verdicts) and
  each finding with the cycle it was first raised, the cycles it came back in and the earlier
  candidates the same role reviewed without raising it; the critique's findings; integration
  outcomes; human feedback. The agent judges escapes (by diffing the pinned candidates) and noise
  against the review criteria.
* **Writes:** `retro/`  **Required:** `retro/report.md` (summary, review effectiveness, ranked
  findings with evidence and root cause, proposed changes as diffs against the assets, what is not worth fixing).
* It proposes; a human applies. Asking again runs a new cycle.

---

## 6. Assets

Agent-facing text is stored as files under `assets/`, with this naming convention:

```
assets/
  roles.yaml               role -> soul, skills, write scope, required docs, git requirement
  souls/<soul>.md          agent persona, injected verbatim (never summarised)
  skills/<skill>.md        skill: frontmatter `description` (when to load it), then the body
  prompts/<role>.md        task prompt template for the role (file name = role name)
```

* A role's prompt is `prompts/<role>.md`, kept light: the rendered template followed by one line
  naming the skills to load first (`legion-harness-contract`, then the role's `skills` in
  `roles.yaml`). The soul is delivered separately as the system prompt addition.
* **Skills are loaded on demand.** Every run gets **every** skill, rendered with its variables,
  as an Agent Skill `legion-<skill>/SKILL.md` (name and `description` up front, the body read when
  the agent loads it). `run-agent.sh` installs them into the agent's skills directory
  (`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills`), first removing any `legion-*` skills an earlier
  run in the sandbox left. A skill's frontmatter holds only `description`: keys such as
  `allowed-tools` would change what the agent may do. The prompt pin covers the soul, the prompt
  and every rendered skill. A reflection has only the Skill tool and brings no skills: its sandbox
  keeps those of the run it reflects on. The local runner gives each sandbox its own `CLAUDE_CONFIG_DIR` (with a link to the
  operator's Claude credentials), so runs never write to the operator's `~/.claude`.
* Templates use `{{var}}` placeholders. Available variables: `change_id, goal, tier, repo_name,
  base_sha, head_sha, oracle_sha (the protected oracle commit; empty before the oracle), branch, task_id, task_title, task_json, cycle, role, context_refs, feedback,
  candidate_sha, write_scope, required_docs, workdir, task_soul, environment` (the planner's
  environment instructions with the library catalogue; only the planner is given it), `fix_round` (in a
  fix round, what it asks of the planner or the critic; empty otherwise). An unknown or unbound variable
  fails G-D2. Load-time validation rejects unknown variables, missing files, souls and prompts
  not referenced by any role, and skills without a valid `description`.
* The implementer's soul is chosen per task (`task_soul`) from `souls_allowed` in `roles.yaml`.
* Each role has a handler in `legion_orch/workflow/roles/`: its dispatch policy (`pushes`,
  `judge`, `panel`, `judges_head`, `reuses_sandbox`, `keeps_session_until`,
  `needs_fresh_approval`, which context it
  sees), its role-specific guards, and the transition after an accepted report. Handlers work
  against the narrow `RoleContext` interface, not engine internals.
* **Context is scoped.** A build task's implementer and review panel get the accepted requirements,
  the approved `plan/spec.md`, oracle and task graph, the delta specs of **the task's own
  requirements only**, the task's declared `context`, its dependencies' commits and the oracle
  commit; the implementer is not given `intake/risk.json`. A document the controller gives at its
  approved version is not repeated from the task's `context` at another version, and relative
  `file:` refs there are qualified to the change. Adding a role means assets
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
| `retrospective` | [`prompts/retrospective.md`](assets/prompts/retrospective.md) |

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
* [`retrospective-analyst`](assets/souls/retrospective-analyst.md)

**Schemas** ([`bindings.yaml`](assets/schemas/bindings.yaml) maps path patterns to these; agents
can fetch them with MCP `docs_schema(path)` / `schema_get(name)`). JSON documents use JSON Schema
2020-12; `x-format: markdown` schemas require headings/patterns; `x-format: text` is free text.
Cross-document rules (coverage, cycles, parallel overlap, resolvable attestations) stay in the
guards, because a per-file schema cannot express them.

* [`approval`](assets/schemas/approval.json)
* [`attestation`](assets/schemas/attestation.json)
* [`change`](assets/schemas/change.json)
* [`delta-spec`](assets/schemas/delta-spec.json)
* [`environment`](assets/schemas/environment.json)
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
* [`sandbox-environment`](assets/skills/sandbox-environment.md)
* [`language-preferences`](assets/skills/language-preferences.md) – the index of the house
  conventions per stack, shared by every role that plans, writes, tests or judges code; edit the
  stack skills to steer what the orchestrator produces:
  * [`stack-python`](assets/skills/stack-python.md) – uv, ruff, pytest
  * [`stack-bun-typescript`](assets/skills/stack-bun-typescript.md) – React, TypeScript, Vite,
    bun, oxlint, Prettier, bun test with happy-dom
* [`pre-mortem`](assets/skills/pre-mortem.md)
* [`finding-format`](assets/skills/finding-format.md)
* [`security-checklist`](assets/skills/security-checklist.md)
* [`architecture-checklist`](assets/skills/architecture-checklist.md)
