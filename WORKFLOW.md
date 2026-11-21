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
Requirements (intake) ◆H-1 -> Plan [ Design (plan + critique) ◆H-E1 -> Oracle (oracle, preflight, baseline) ◆H-2 ]
                     -> Delivery (build, verify, review loop; integration, release) ◆H-3 (its preconditions: the ship gates)
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
                        |  dispatcher       |  risk/gates            |  openshell CLI  |
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
  `soul.md`, `prompt.md`, `mcp.json` (MCP URL, no credentials), `run.env` and one script per
  preparation step, which the controller runs in order (each logged to the run's agent log):
  `clone.sh` (a partial clone: history, contents on demand); `branch.sh <branch> <start>` for a
  role that pushes (its branch at its tip on the hub if pushed before, else at its base) or
  `checkout.sh <sha>` – a detached checkout, no branch – for every other role (judges at the
  commit they judge, read-only roles at their base); `mise-setup.sh` when a setup is pinned
  (§5.3c; roles that work with a commit); then `run-agent.sh` starts the agent. A reused sandbox
  skips the clone. Verifier sandboxes run the same `clone.sh` and `checkout.sh`.
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
| `changes/<id>/tasks/<task_id>/` | everything about one build task: `evidence/cycle-<n>.md` and `reviews/<role>-cycle-<n>.md` (agents); `verification.json`, `feedback/<n>.md`, `decisions/<n>.md` (controller: the escalation's question, then the human's answer) |
| `changes/<id>/control/change.json` | change record: goal, base, tier, risk, approvals, oracle, integration head, ship result |
| `changes/<id>/control/workflow.json` | the workflow's stage rules (§4.1): the tree's parameters (tier rank, environment, blank repository) and every input the workflow core was given, in order – human decisions, step results, task graphs. The core is pure, so replaying them rebuilds it exactly |
| `changes/<id>/control/nodes/<node>.json` | a node's detail (its state inside its step, cycle, commits, verification, reviews, feedback, escalation, sandbox); one new version per transition, so the version history is the node's audit trail. Which nodes exist and their order come from the workflow's tree |
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
merges, provisioning and gate evaluations are resumed.

### 2.2 Attention and notifications

What a decision owner should look at is derived from the change's state by one function
(`legion_orch/workflow/attention.py`), which the UI, the CLI, the change list and the notifier
all use. Nothing here gates the workflow; it only says what is waiting.

| Item | When | Blocking |
| --- | --- | --- |
| `gate` | a human gate is `AWAITING_HUMAN` (H-1, H-E1, H-2, H-3) | yes |
| `escalation` | a node is `ESCALATED` (its kind and reason) | yes |
| `not_ready` | H-3 is accepted (or automatic, at R0/R1) but waits on its preconditions: the ship gates not satisfied, and why; resolved when a recheck finds them all met | yes |
| `change_ended` | the change is `READY` or `ABANDONED` | no (inbox) |
| `retrospective` | the retrospective report is ready (each cycle is a new item) | no (inbox) |

**Needs you** lists the blocking items: they stay until the decision is made. The **inbox** holds
the others: nothing waits for them, and the UI keeps which ones this browser has read.

Every save compares the items with those of the previous save: a new item is recorded on the
timeline (`attention`, `raised: …`) and notified, a resolved one is recorded with how long it
waited (`resolved after 9h02m: …`). A node's `status_since` records when it last changed
status. After a restart the items already waiting are taken as known: they are neither raised
nor notified again.

**Notifications** (config `notify:`) go from the controller only; sandboxes never reach a sink.
Each sink – `webhook` (JSON POST, optionally signed `X-Legion-Signature: sha256=<HMAC>`), `ntfy`
(a topic URL) or `command` (JSON on stdin) – receives the item kinds in `notify.items` (default the
blocking ones) or its own `items`. A blocking item still waiting after `notify.remind_after_s` is
sent again; `notify.resolved: true` also sends its resolution. A delivery is tried three times,
then recorded on the timeline (`notified`) as sent or failed; it never blocks or fails the
change. The payload carries the item, the decision owners and a link to it in the UI
(`notify.ui_url` + `#<change>/overview/<node>`). An escalation's reason can quote agent output,
so it is cut to 300 characters; no document content is sent.

**While you were away.** `GET /api/digest?since=<iso>` (UI: shown after 30 minutes away;
CLI: `legion-orch digest --since 8h`) counts each change's timeline events since then – runs
started, reports accepted and rejected, reworks, merges, escalations, human decisions – without a
model call, and lists everything that needs you in full.

### 2.3 Gate screens: traceability and what H-3 accepts

The gate cards show how the pieces of the plan fit together, computed from the change's records
by `legion_orch/workflow/traceability.py` (API `GET /api/changes/<id>/trace`, CLI
`legion-orch trace <change> [--format md|json]`). Like §2.2 it decides nothing: the guards stay
the authority.

**The matrix** has one row per accepted requirement (its first line from `requirements.md` as
accepted at H-1): its delta spec and H-2 status, the tasks of the latest task graph that cite it
(once built: status, latest verification, the panel's verdicts at the current cycle), and the
oracle's criteria for it with the tests and commands each names (`criteria[].tests`,
`criteria[].commands`, checked by G-P12). After integration each linked criterion shows `pass` or
`fail` from the controller's oracle command results at the integration head.

**Warnings**, listed first: an H-2 subject changed since it was approved (with a diff from the
approved version: `GET /api/doc/diff?path=&from=&to=`), a requirement no task covers, at R2+ a
requirement with no acceptance criterion (G-P8 needs only one for the whole oracle), a
requirement covered only by the scaffold task, a task verified only by lint or type checks, and
a criterion that names no test or command.

**At H-3** (`GET /api/changes/<id>/acceptance`) the card adds the diff `base..integration head`
as a whole and per merged task (the task's own commits), the protected paths changed since the
oracle commit with the G-PP1 approvals behind them, and a dry evaluation of the ship gates at the
head (§5.11): what would not be satisfied if the change were accepted now.

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

Each gate is reported as `satisfied`, `unsatisfied` or `unevaluable` by the deterministic ship check,
which is H-3's precondition (§5.11). Where each gate's evidence comes from:

| Gate | Evidence |
| --- | --- |
| `approved_delta_spec` | every delta spec `plan/specs/<REQ>.md` (and the plan) approved at H-E1 against its current version |
| `approved_spec_and_oracle` | every subject of H-E1 (plan, delta specs) and H-2 (oracle) approved and fresh, the **last** approval before the first build run (a blank repository's scaffold excepted, §5.3b) |
| `protected_oracle` / `protected_acceptance_tests` | acceptance-test blobs unchanged since the oracle commit, except paths a human approved beforehand (G-PP1); acceptance commands pass at head |
| `integration_or_real_interface_checks` | integration verifier PASS, oracle and **surface** commands pass at head, surface pins unchanged or re-affirmed (G-SF1) |
| `independent_baseline` | human `independent-baseline` attestation (pass), recorded **before** the first build run (the scaffold excepted) |
| `security_or_e2e_evaluator` | human `security-evaluation` or `e2e-evaluation` attestation; any `fail` blocks |
| `architecture_or_security_review` | architecture or security panel PASS on every task, or a human `architecture-review` attestation; a `fail` attestation blocks |
| `rollback_or_forward_fix_evidence` | human `rollback-evidence` or `forward-fix-evidence` attestation |
| `release_observation_plan` | release planner's `release/plan.md`, or a `release-observation` `not_applicable` attestation (waiver) |

Gates can be added or waived by a human (G-OV1, §5.12); waived and human-judgement gates are
listed in the readiness result.

---

## 4. State machines

### 4.1 Stages, gates and steps

A change's workflow is a tree of **stages**, most ending in a **gate** – the stage's last step;
the steps inside a stage run in order (a stage's tasks as their dependencies allow). It is built for the change's tier,
environment and repository once H-1 fixes the tier (`legion_orch/workflow/engine/runtime/workflow_tree.py`):

```
Requirements   intake ─────────────────────────────────────────────── ◆ H-1 accept requirements & tier
Plan           (done when its stages are)
  Design       plan (planner ⇄ critic) ─────────────────────────────── ◆ H-E1 approve the plan
  Oracle       {provision} <scaffold> [oracle preflight] [baseline] ─── ◆ H-2 approve the oracle
Delivery       tasks (DAG of build:<id>) ─ [integration] ─ [release] ── ◆ H-3 accept the change
```

* **Per tier.** Stages without content are left out: R0/R1 have no oracle, preflight, baseline,
  integration or release (an Oracle stage there holds only provision or the scaffold, and has no
  gate). `[…]` R2+, the baseline and release R3.
* **Gates** are steps: the step's work checks the gate's **preconditions** (H-3's are the tier's
  ship gates, §3, §5.11) and then waits for a human's approval (H-E1's and H-2's subject by subject,
  §5.5a; at R3, H-2's subjects include the `independent-baseline` attestation). At R0/R1 nobody
  approves H-3: it passes as soon as its ship gates hold. A gate node is `AWAITING_HUMAN` while
  it waits for a decision – an expected escalation in the core.
* `{…}` exists only with an `environment:` section (§5.3a); `<…>` only in a blank repository
  (§5.3b). The build tasks are added once the critic passes the plan.

**Who decides what.** The stage rules run in a pure **workflow core** (`legion_orch/workflow/flow/`,
no I/O): which step starts when, send-backs, fix rounds and the end – a gate is a step to it.
Within a step the engine decides: its phases (§5.6–5.9), rework cycles, escalations and their
answers, what a gate checks. The step runner reports to the core – done, escalated, resumed – and
the core's effects start, stop, resume and retry steps (`engine/runtime/flow_runtime.py`).

**Actions are generic.** On a waiting gate: **approve** (each of its subjects, §5.5a) – the
answer to its step's escalation, **send back**, **reject** (H-3) and **recheck** (a retry: its
checks again). On an escalated step: **answer**, **retry**, **override** (build tasks), **run again
in place** (provisioning, §5.3a) and **send back**. **Send back** (`send_back`, with a stage
and feedback) is offered for every stage the node's work was built on – the stages enclosing it
and, at each level, the stages before them – nearest first; the UI's names map onto it:

| Action | Sends back |
| --- | --- |
| H-1 revise | Requirements |
| H-E1 revise | Design (of its round) |
| H-2 revise | Oracle (of its round) |
| H-2 restart, **Restart plan** | Plan: every round goes |
| an oracle, preflight or baseline escalation: **Revise the plan** (`revise_plan`) | Design |
| an `integration_failed` escalation: **Plan a fix** (`plan_fix`) | Delivery: a fix round (§5.10a) |

The state API returns, from the core, the actions each node offers now and the stages with
their nodes; the UI renders them as they come – a **Send back: <stage>** for each stage offered
(Requirements and Plan behind a confirmation, Delivery as **Plan a fix round**), override only
where the step allows it – and draws each stage as a box around its nodes on the task graph.

What sending a stage back does is the stage's **rework policy**. Requirements, Plan, Design and
Oracle **start over**: everything in them, and every later stage built on them, goes back to
pending and runs again, and the stage's first step gets the feedback. Delivery **appends a
round**: its merged tasks stay, and a Round stage is inserted before integration (§5.10a). What a
reset undoes outside the workflow is declared per step type (`on_reset`): the plan's restart
re-initialises the integration branch, the oracle, the rounds, the run branches and the surface
pins; provisioning's drops the environment approval and build.

### 4.2 Node status

```
PENDING -> READY -> RUNNING -> VERIFYING -> REVIEWING -> MERGING -> ACCEPTED
                       ^           |             |           |
                       +-- REWORK -+-------------+-----------+   (evidence-driven, cycle budget)
              any -> ESCALATED (human) -> READY | ACCEPTED (override) | FAILED
              human nodes: PENDING -> AWAITING_HUMAN -> ACCEPTED (they show their gate)
              plan: PENDING -> READY -> RUNNING -> REVIEWING (critique) -> ACCEPTED, REWORK on REWORK
```

### 4.3 Run status

`STARTING -> ACTIVE -> REPORTED -> (ACCEPTED | SUPERSEDED)`;
`ACTIVE -> FAILED` on crash, timeout or `max_report_attempts` rejected reports.
On `ACCEPTED` the run's sandbox is deleted.

**A run ends when its agent's turn does.** The `report` tool checks a report as it is filed, so a
rejected one can be fixed, and records an accepted one (`REPORTED`, with the instruction to end
the turn) – but the workflow does not move until the agent ends its turn and its process exits:
then the role's handler judges the report and the run's phase takes the result (a BLOCKED report
escalates). A turn that ends without an accepted report fails the run. An agent still in its turn
300 s after its report was accepted is stopped; a run whose turn ended while the controller was
down is concluded after the restart, its report checked again.

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
| G-D3 | dispatch | `live agent runs + verifier sandboxes in use < max_parallel` (across every change; the dispatcher runs a step's work in a slot, and a step's reservation keeps its slot for the work that follows straight on – its verification, its judges), and no `RUNNING` build node shares a `files_modified` entry with the candidate. |
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
| G-P11 | Blank repository (§5.3b): exactly one task is marked `scaffold`; it has no dependencies and every other task depends on it, directly or not. Otherwise no task is marked `scaffold` (nor a fix round's new task). |
| G-P12 | R2+: a criterion's optional `tests` are among `oracle.acceptance_tests` and its `commands` among `oracle.acceptance_commands`. Unlinked criteria are allowed; the gate screens warn about them (§2.3). |
| G-P13 | R2+: a command in `acceptance_commands` or `integration_commands` that only installs dependencies (`bun install …`, `npm ci`, `uv sync`, `pip install …`, …) belongs in `mise.toml`'s `setup` task (§5.3c): the verifier runs the setup, then acceptance, then integration – so an install placed later runs after the commands that need it. |

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
| G-E8 | A proposed `mise` (§5.3c) parses; every profile of a role that runs the setup (those that work with a commit), and the verifier's, installs its `[tools]` at compatible versions (mise never installs at run time); when it has a `setup` task, each of those profiles has the `setup_network` policies. |
| H-E1 | After a critique PASS, a human approves the plan (§5.5a) – with it the environment, its own subject (approver ∈ `decision_owners`); the approval pins the `plan/environment.json` version. Revise sends the Design back: `plan` is reworked with the feedback attested and critiqued again. Any send-back that resets `provision` drops the approval and the build. |

### `provision` – build the environment (controller, no agent)

The approved spec version is resolved against the library again and built: one image per
(profile, base image) – `FROM sandbox.image` for agents, `FROM sandbox.verifier_image` for the
verifier – with the profile's tools installed by mise under `/usr/local/share/mise` (read-only in
every sandbox) and their `env` settings as image `ENV`, via `podman build`, tagged per change and
recipe (`localhost/legion-env:<change>-<recipe hash>`, or pushed to `environment.registry`) and
labelled `org.legion.change` / `org.legion.recipe`. Roles of a change with the same recipe share
an image; another change with the same recipe gets its own tag on the same layers (the build
cache makes it nearly free), so a change's images are its own to remove when it ends (§5.14). Network policies
carry no credentials, so nothing is created for them in OpenShell: each is resolved into a
`network_policies` entry (`env_<name>`, open to the binaries that brought it) recorded with the
build and added to the policy of every sandbox of the role. CPU and memory become `sandbox create --cpu <vcpus>` and `--memory <n>Mi`; disk,
which has no CLI flag, is rendered through `policies/sandbox-resources.template.yaml` into
`--driver-config-json`. The result is recorded in `control/environment/build.json` and the
change record; the build log in `control/logs/provision.log`.

| ID | Rule |
| --- | --- |
| G-PV1 | Every image a mapped profile needs is built or found. Otherwise the node escalates `provision_failed` (retry provisions again). |
| G-PV3 | With an approved `mise.toml` that has a `setup` task: `mise run setup` succeeds at the integration head in a fresh verifier sandbox from this build. Otherwise `provision_failed` with the output. |
| G-E5 | (dispatch of a mapped role, and every verifier sandbox) the H-E1 pin still equals the latest `plan/environment.json`. |

From then on every sandbox of a mapped role is created from its profile's image, with its
network policies in the sandbox policy and its resources; unmapped roles keep the defaults.

The build is fixed at provisioning: later library edits do not reach it. When a step escalates
because of its environment (an agent BLOCKED for a missing tool, a verifier command not found,
a failed setup), a decision owner may answer by **provisioning again** (`rerun:provision`, a
reason of ≥24 chars): the provision step runs again in place – the H-E1-approved spec resolved
against the current library again, e.g. after a library entry was fixed – while what was built
on it stands, and the escalated step is retried in the new build. It does not change the spec:
a tool or policy the spec does not name needs a plan revision and a new H-E1. It is offered
only as an escalation's answer. Provisioning starts once no run of a mapped role is live and no
verifier sandbox is in use; meanwhile new runs of mapped roles and verifications wait for it.
Sandboxes kept across cycles by mapped roles (implementers) are then released, after their
agent reflects, so each node's next run starts fresh in the new build and continues from its
pushed branch. Unmapped roles' sandboxes are left alone: the planner keeps its session to H-2
through H-E1 and provisioning. A failure escalates the provision step like any other.

### 5.3c Repository setup: `mise.toml`

Every sandbox that works on the repository is a fresh clone. The repository's `mise.toml` says what
a checkout needs: `[tools]`, installed when the environment's images are built, and a
`[tasks.setup]` task – dependency installs, code generation, a build the tests need – that the
controller runs after every checkout as `mise run setup`: first in every verifier sandbox (task
verification, baseline, integration; G-V3), and once after the clone in the sandbox of every role
that works with a commit (oracle, baseline, implementer, reviewers, integration; a failure is
logged and the agent may still work). Read-only roles (intake, planner, critic, release) skip it.
No `setup` task: nothing runs.

**Which version runs (G-MS1).** Only a pinned `mise.toml` (`change.setup_pin`, its git blob): the
version approved at H-E1, else the base branch's when the change started (written before the
change). The setup script checks the checkout's file against the pin before `mise trust` and
`mise run setup`; any other version is not run. `mise.toml` is a protected path while a setup is
pinned or proposed (G-P7, G-B3, G-INT2); Restart plan returns to the base branch's pin.

**With the environment stage** the planner proposes the file in the spec (`"mise": {"content":
…}`, with the network its setup needs in `"setup_network"`): unchanged, edited or new. H-E1 shows
it; on approval the controller commits it to the integration branch (before any build) and pins
it; provisioning checks its setup runs in the verifier (G-PV3). **Without it** the operator's
images are assumed to have `mise`, and the base branch's setup task (if any) runs.

**In a blank repository** (§5.3b) the base's setup task is never pinned: what it needs – a
manifest, a lockfile – is what the scaffold creates. The setup comes with the environment stage,
and until the scaffold is accepted provisioning does not check it (G-PV3) and the scaffold's own
sandbox does not run it; the scaffold's **verification** does, at its candidate, so a setup that
does not work is a failed scaffold verification (reworked). Every sandbox after the scaffold runs
it as usual.

### 5.3b Scaffold (blank repository)

When a change starts, the controller lists the base commit's tree. It is **blank** if every file
is documentation (`*.md`, `*.rst`, `*.adoc`), a top-level licence or notice file (`LICENSE*`,
`LICENCE*`, `COPYING*`, `NOTICE*`, `AUTHORS*`), `.gitignore`, `.gitattributes`, `.editorconfig`,
a top-level mise file (`mise.toml`, `.mise.toml`, `.tool-versions`: tool versions are not code)
or under `.github/` – including a repository with no files at all (`repo.path` omitted). The
change record keeps it (`blank_repo`). Without this the oracle would have to guess the layout,
test runner and configuration the first build task will create.

In a blank repository the planner's assignment (`{{scaffold}}`) asks for one **scaffold task**
(`"scaffold": true`, G-P11): the project's layout, manifest, build, lint and test tooling, their
configuration and a placeholder entry point – none of the requirements' behaviour – whose
instructions fix every choice the acceptance tests depend on, and whose verification commands
pass without them. The critic is told to check this.

The scaffold is a build node (`build:<id>`) at every tier, but it opens the Oracle stage (after
provisioning) instead of joining the other tasks in Delivery:

```
plan (critique PASS) -> H-E1 -> {provision} -> build scaffold -> R2+: oracle -> preflight -> R3: baseline -> H-2 -> other builds
```

It is implemented, verified, reviewed and merged like any build task (§5.6–5.9) once H-E1 has
approved the plan (task graph, scaffold included). G-B1 does not apply to it, and it does not
count as the first build run for `approved_spec_and_oracle` and `independent_baseline`. The oracle
then works at the integration head that contains it, with the scaffold's commit in its context,
and writes its tests for that project. **Restart plan** resets the scaffold with every other build
task (a new task graph replaces them); a fix round never adds one.

### 5.4 `oracle` (R2+) – protected acceptance tests

* **Soul:** `test-engineer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`, `language-preferences`
* **Writes:** `oracle/**`  **Required:** `oracle/tests.md`, git `commit`

| ID | Rule |
| --- | --- |
| G-O1 | Changed paths `base..commit` ⊆ `oracle.acceptance_tests` ∪ `oracle.protected_support`. |
| G-O2 | Every acceptance test path exists at the commit. |

The oracle commit is merged to the integration branch before any build task starts (a blank
repository's scaffold excepted, §5.3b). From then on these paths are **protected**: any later
diff touching them fails G-B3 and the `protected_oracle` gate.

### 5.4a `preflight` (R2+) – the oracle's commands run (controller, no agent)

Once the oracle's tests are merged, the controller runs the repository's setup (§5.3c), then the
oracle's `acceptance_commands`, at the integration head in a **fresh verifier sandbox** (G-V1 semantics:
a new clone, the verifier image and environment profile) – not in the oracle's sandbox, whose
installed dependencies and build output would hide what a clean run lacks. It confirms the
commands can be run here, as the baseline and integration will run them. Before any build the
acceptance commands are expected to fail; one that already passes is noted on the timeline.
Results are recorded in `control/verification/preflight.json`; fix rounds run it again.

| ID | Rule |
| --- | --- |
| G-PF1 | The repository's setup (`mise run setup`, when pinned) exits 0 and every acceptance command is found (not 127) in a verifier sandbox that ran. A setup failure sends the output back to the oracle's author (one oracle cycle, `review_max_cycles`; the author reports BLOCKED if the fix is in the setup – the plan's environment – or the plan), then escalates `setup_failed`; a missing command escalates `verify_unrunnable` (G-V2), an infrastructure error `verify_infra`. **Retry** runs it again; **Revise the plan** sends the Design back. While the oracle's author reworks, preflight waits for the oracle to be accepted again. |

### 5.5 `baseline` (R3) – independent baseline

What is true of the code before any build: which acceptance tests fail (as they should, until the
change is built) and which failures were already there. The integration verifier is given it.

* **Soul:** `baseline-verifier`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`
* **Writes:** `baseline/**`  **Required:** `baseline/report.md`, git `head`

| ID | Rule |
| --- | --- |
| G-BL1 | `data.results` lists every acceptance command with its exit code at the integration head; the attested head equals the current integration head. |
| G-BL2 | The controller then runs the same commands at the head itself (G-V1 semantics; failures are expected and recorded, not reworked). At H-2 the oracle subject lists, for the approver to weigh, every acceptance command that already passes (it cannot show that the change works) and every result that differs from the baseline verifier's. |


**Revise the plan from an escalation.** An oracle, preflight or baseline that escalates (its
commands cannot run, say) never reaches H-2. Its escalation therefore also offers **Revise the
plan** (`revise_plan`, `feedback` required): it sends its round's Design back to the planner with
the feedback – the plan is critiqued and approved at H-E1 again, and the oracle stage starts
over from it (in round 1 with the integration branch, §4.1).

### 5.5a H-E1 approve the plan, H-2 approve the oracle

Each gate's subjects are separate decisions, possibly by different approvers ∈ `decision_owners`,
each pinned to the document versions approved. H-E1 approves the plan before the oracle is
written or any task is built on it; H-2 approves the oracle before any task but the scaffold is
built.

| ID | Rule |
| --- | --- |
| H-2 | **H-E1** subjects: the **plan** (`plan/spec.md` + `plan/taskgraph.json`), **each delta spec** `plan/specs/<REQ>.md` and, with an environment, the **environment** (§5.3a). **H-2** subject (R2+): the **oracle** (`plan/oracle.json` + oracle commit sha; approving it pins every verification surface). A gate completes when every one of its subjects is approved against current versions; at R3 H-2's precondition is an `independent-baseline` pass attestation, so the baseline is recorded before any build run. That attestation is decided with the oracle: approving the `independent-baseline` subject (or approving all) records it, citing `baseline/report.md` and the controller's own baseline verification (G-AT1); it is stale once the baseline runs again (a fix round). |
| G-B1 | (dispatch of any build run but the scaffold, §5.3b) every H-E1 and H-2 subject's pins equal the current latest versions – an approval goes stale if its document changes. |

The H-2 card shows the traceability matrix and its warnings (§2.3); a stale subject offers the
diff since the version approved.

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
| G-V3 | With a pinned setup (§5.3c), `mise run setup` runs first in every verifier sandbox (results mark it `setup`); if it fails, no other command runs. In a task's verification or at integration that is a failure like any other (the task may have caused it); at preflight and the baseline – before any build – it is handled as G-PF1 says (the baseline: escalates `setup_failed`). |
| G-MS1 | The setup runs only from the pinned `mise.toml`: a checkout whose file differs from the pin is not trusted and its setup is not run (the step fails). |

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

The panel's members run in parallel and the panel decides once **every** member has finished its
turn. A member that fails, reports BLOCKED or cannot be dispatched does not escalate the node
while the others still work: the panel's one task runs every member and collates their results
once all have finished, then the node escalates with each escalation (a retry dispatches only the members without a review at the cycle).

FAIL ⇒ node `REWORK`: the implementer run is re-dispatched **in the same sandbox** with all
HIGH findings attested as feedback (cycle + 1).

### 5.9 Accept and merge (controller)

| ID | Rule |
| --- | --- |
| G-A1 | Verification passed and every panel member returned PASS at the same candidate sha – checked from the evidence: the verification record and the panel's recorded pass (which judges sat, citing their reviews; `tasks/<id>/panel.json`). An override waives the panel, not verification. |
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
| G-REL1 | `release/plan.md` contains health and rollback criteria headings; `release/rollback.md` names the revert target sha and recommends a recovery strategy on its own line, `Strategy: rollback` or `Strategy: forward-fix` (with a `Rationale:`). |
| H-3 | Approver ∈ `decision_owners` accepts the change, pinned to the integration head sha. Any later head change invalidates it. The card shows the matrix with results, the diff per task, protected paths changed and the ship gates' dry evaluation (§2.3). The gates only a human attestation satisfies are rows on the card, attested with the acceptance (G-AT1). `rollback_or_forward_fix_evidence` shows the release planner's recommended strategy and rationale with `release/rollback.md`: confirming it attests `rollback-evidence` or `forward-fix-evidence` accordingly, citing the plan. `security_or_e2e_evaluator` is attested `pass` citing the evidence ticked (security reviews, the integration report, the controller's oracle run) or named. A row with no specific evidence behind it – any but a recommended recovery plan – may instead be attested `not_applicable` with a reason, recorded against the integration report (else the integration head); the gate itself is not waived. Every row has one **Attest** button that records it at once; the card's own action (Accept change, Re-check the ship gates; at H-2 Approve all pending for the baseline) also records every row that is complete, and says which. A gate left unattested is reported as a warning: H-3 waits on it (its preconditions, §5.11) until one is recorded. |

### 5.10a Fix rounds

A late failure – the integration verifier reports FAIL, or the oracle commands fail at the head
(`integration_failed`) – is fixed by **planning new work on top of the integration head**, as if
the change started from existing code. Accepted tasks are never reopened: a round adds tasks.
Round 1 is the original plan; each fix round `n ≥ 2` is a **Round stage** of Delivery, inserted
before integration: its own Design (plan, critique ◆H-E1), its own Oracle stage (provision and the
oracle only if the round changes them, preflight, baseline ◆H-2) and its new tasks; then
integration runs again. Its nodes are suffixed `@n` (`plan@2`, `H-E1@2`, `oracle@2`, …); every
earlier node keeps its record. A round **revises the plan documents in place**: it appends tasks to
`plan/taskgraph.json` (and revises the spec, a delta spec, the oracle or the environment only
when the fix needs it); the store's versions and the approval pins record what each round added.

**Start.** On an `integration_failed` escalation a decision owner chooses **Plan a fix**
(`plan_fix`, optional `comment`). Refused after `limits.fix_rounds` rounds (default 2); the change
can then still be answered, overridden at the gates or abandoned. The round is recorded in
`control/rounds/<n>.json` (approver, comment, the head it starts from, and the failure: the
integration run's report documents and the failed verification record). The integration node's
escalation is cleared and Delivery is sent back: its policy appends the Round stage, integration
returns to `PENDING` (its finished runs superseded, their sandboxes deleted after they reflect)
and runs again after the round, a new cycle. The round's `plan@n` starts it.

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

**After the critique passes**, the round's H-E1 approves its plan: the revised task graph makes
the `plan` subject stale, as does any revised spec or delta spec; unchanged subjects keep their
approval. Then what the round changed runs again:

* **Environment.** Unchanged ⇒ the build stands (`provision@n` passes at once). Changed (the
  latest `plan/environment.json` is not the one H-E1 pinned) ⇒ the round's H-E1 approves the new
  version with the plan and `provision@n` builds it, as in round 1 (§5.3a).
* **Oracle.** Unchanged ⇒ `oracle@n` passes at once: the oracle commit, protected paths and
  surface pins stand. Changed ⇒ the test engineer works at the integration head (its own branch
  for the round) and its commit must stay within the (new) protected paths (G-O1, G-O2); it is
  merged and becomes the oracle commit that G-INT2 and `protected_oracle` compare against, and its
  approval at the round's H-2 re-pins the surfaces.
* **Preflight** runs again at the round's head.
* **Baseline (R3).** Every round runs the baseline again at the integration head, after the
  oracle (changed or not) and before the round's builds: it records what fails before the fix. A
  decision owner attests `independent-baseline` again: H-2 only completes with an attestation
  newer than the current baseline run.
* **Build nodes** are added for the appended tasks (`build:<id>`, the round recorded on the node);
  they run after the round's H-2.

**H-2 for the round** waits once the round's oracle, preflight and baseline are accepted; a
revised oracle makes the `oracle` subject stale (G-B1). **Revise** at the round's H-E1 sends the
round's Design back to the planner with the feedback (the round's tasks are planned again;
earlier rounds stand). **Restart plan** sends the Plan back: every round goes, and every build,
the integration branch, the oracle and the surface pins start over.

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

### 5.11 Ship gates – H-3's preconditions

The controller evaluates every gate of the tier (§3) against the store and the mirror, re-resolving
every attestation it relies on, and writes `control/ship/readiness.json`. Verdict `READY` iff every
gate is `satisfied` (or validly waived). It publishes nothing.

The ship gates are **H-3's preconditions**: evaluated when H-3 opens, when it is accepted, and on
**recheck**; the gates H-3's own acceptance satisfies (`explicit_human_approval`,
`whole_change_acceptance_evidence`) count only once it is given. H-3 completes – and the change
is `READY` – when it is accepted (automatically at R0/R1) and every gate holds. Until then the
change stays active and H-3 waits: once accepted (or at R0/R1) it needs you as `not_ready`
(§2.2), and its card lists the gates not satisfied and why, takes the attestations only a human
can give, and rechecks once the evidence is recorded.

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
| `prune` | `change.pruned_at` | only once the change has ended: removes its integration branch and evidence refs (§5.14) |

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

**Retrospective.** When a change ends (`READY` or `ABANDONED`), node
`retrospective` is added and dispatched even though the change is no longer active – once the
reflections of the sandboxes released at the end have finished, as they are part of its material.
`retrospective.auto: false` leaves it to a decision owner, who can also ask for another at any time
after the change has ended (UI, or `change_action retrospective` with an optional `focus`).

**Abandoning.** A decision owner ends a change early with **Abandon** on any escalation, or
**Reject** at H-3, optionally giving a `reason`. Who, at which node and why is recorded in
`control/ended.json` and the change record (`ended`, which the retrospective reads in
`change.json`) and in the timeline. The retrospective then runs on whatever the change has
produced so far – also with `retrospective.auto: false`, unless the action passes
`retrospective: false`.

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

### 5.14 Cleanup

What a change leaves in the hub and on the controller's host, and when it goes:

| What | Goes |
| --- | --- |
| a run branch (`legion/<change>/build-<task>`, `…/oracle[-rN]`) | when its node is accepted: the merge made its commit part of the integration history, and the evidence ref keeps every candidate. **Restart plan** removes all of the change's run branches (a restarted task of the same name must not continue the old plan's commits). |
| leftover run branches and the change's environment images (local `podman` images labelled with the change) | once, when the change has ended (`READY` or `ABANDONED`), nothing of it is running and its retrospective is done (`change.cleaned_at`). A run after that – a retrospective asked for again – uses the default image. Images pushed to `environment.registry` stay: a shared registry is the operator's to clean. |
| `legion/<change>/integration` (the deliverable: what the ship check judged) and `refs/legion/evidence/<run>` (every reported candidate, so reviews' and reports' `git:` attestations resolve) | when a human **prunes** the ended change (change action `prune`, `legion-orch prune <change>`) after taking the integration branch. The change's records stay; its `git:` attestations no longer resolve, and the ship gates can no longer be rechecked. |

The controller never publishes: merging the integration branch into the repository's own branches
is the people's step, after which `prune` tidies the hub.

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
  fix round, what it asks of the planner or the critic; empty otherwise), `scaffold` (in a blank
  repository, what the scaffold task asks of the planner, the critic or the oracle; empty
  otherwise). An unknown or unbound variable
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
