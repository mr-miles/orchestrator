# Legion Orchestrator Workflow

This document specifies the orchestrator's workflow: the tickets every change is made of, the
guards on them, the risk tiers' policies, and which soul, skills and prompt each role uses. The
agent-facing text itself lives in [`assets/`](assets/) (see §6), one file per asset, and the
controller loads it from there. Apart from [`assets/workflow.yaml`](assets/workflow.yaml) (the
skeleton and the tier policies, §4.2, §3) this document is not parsed.

Every dispatch records two pins: the SHA-256 of the whole asset set and the SHA-256 of the rendered
soul + skills + prompt. Any edit to an asset is therefore visible in the run record. Guard IDs
(`G-…`) are implemented one-to-one in the code (`legion_orch/workflow/guards/`, the role handlers,
the executors); `tests/test_units.py` fails if this document and the code drift apart, or if a
link below points at a missing asset. Sign-offs (`H-…`) are tickets of the skeleton.

A change is one tree of **tickets**. A ticket is its own work, then its subtasks, then its
acceptance criteria – and criteria are tickets too, so a human's sign-off is just the criterion
of the work it signs. Every change starts from the same skeleton:

```
change
├─ workspace       sync (the change's base)
├─ requirements    intake ✓H-1
├─ plan            design (planner) ✓critique ✓H-E1 → provision → scaffold ✓verify ✓review ✓can-merge
│                  → [acceptance: oracle ✓preflight ✓H-2 → baseline ✓H-2b]
├─ delivery        the planner's builds (each ✓verify ✓review… ✓can-merge)   ✓verify ✓integration
├─ evaluate        describe                                        ✓H-3
└─ release         publish
```

`✓` marks a criterion, `→` the order of subtask groups, `[…]` R2+ (the baseline R3). The planner
gives `scaffold` its work or cancels it (§5.3d). The
contract is the one of [legion-ascended](https://github.com/9thLevelSoftware/legion-ascended): **the
controller refuses to call a change done until the evidence its risk tier demands exists** – here,
until every ticket its tier's policy requires has passed every criterion, the human sign-offs
included. A criterion with nothing behind it does not pass.

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
5. **Errors by kind.** A failed criterion is evidence: the work runs again with the findings (an
   *attempt*), up to the ticket's limit; then a human decides. A **system** error (sandbox,
   gateway, git, a verifier's infrastructure, a controller restart) is never the agent's: it is
   retried with backoff and then waits on a human, and never costs an attempt. An exceeded
   **budget** (the run timeout) and a **BLOCKED** agent wait on a human at once.
6. **Independence.** Judges (critics, reviewers, verifiers) run in fresh sandboxes and never share
   a sandbox, identity or run with the work they judge.
7. **Humans own the boundaries.** Requirement acceptance, plan and oracle approval, every
   escalation's remedy, replans and whole-change acceptance are human decisions recorded with a
   named approver from `decision_owners`. No identity is inferred.
8. **Finished work is never undone.** A passed ticket is closed; corrections are new tickets
   (§4.6).
9. **Sandbox lifecycle follows the ticket.** A ticket's agent keeps its sandbox and session until
   the ticket is closed; then it is deleted (after the agent reflects, §5.13).

---

## 2. Actors, sandboxes and tools

```
                        +-------------------- controller (python) --------------------+
 human --- web UI ----> |  ticket runner    |  guards  |  doc store  |  git remote     |
                        |  dispatcher       |  tier policy           |  openshell CLI  |
                        +-----------^-----------------------------------------|-------+
                                    | MCP + git (gateway-injected bearer)     | sandbox create / upload /
                                    |                                         | exec / delete
                     +--------------+-------------+      +--------------------v----------+
                     | openshell sandbox: run 17  | ...  | openshell sandbox: verify 18  |
                     |  agent CLI + soul + prompt |      |  deterministic commands only  |
                     |  /sandbox/work (git clone) |      |  (no agent, git fetch only)   |
                     +----------------------------+      +-------------------------------+
```

* **Sandbox per ticket.** A ticket's own agent (intake, planner, oracle, an implementer …) keeps
  one sandbox for the ticket's whole life: stopped between rounds (`openshell sandbox stop`),
  started again for the next attempt, its agent session continued (`claude --resume`), until the
  ticket is closed – so the intake keeps its session until H-1 passes, the planner until H-E1,
  the oracle until H-2. A criterion's agent (a judge) gets a fresh sandbox every round (G-D4)
  (`openshell sandbox create --detach --name lg-<first name> --provider legion-mcp --provider
  legion-git[-push] --policy … --upload payload:/sandbox`). The payload contains only `soul.md`,
  `prompt.md`, `mcp.json` (MCP URL, no credentials), `run.env` and one script per preparation
  step, which the controller runs in order (each logged to the run's log): `clone.sh` (a partial
  clone: history, contents on demand); `branch.sh <branch> <start>` for a role that commits (its
  ticket's branch at its tip on the hub if pushed before, else at its base) or `checkout.sh <sha>`
  – a detached checkout, no branch – for every other role (judges at the commit they judge,
  others at their base); `mise-setup.sh` when a setup is pinned (§5.3c; roles that work with a
  commit); then `run-agent.sh` starts the agent. A reused sandbox skips the clone. Verifier
  sandboxes run the same `clone.sh` and `checkout.sh`.
  **No credential ever enters a sandbox, and nothing is ever downloaded from one.**
* **Workload identity (SPIFFE/SPIRE).** Set up once per OpenShell workspace
  (`legion-orch setup-openshell`): one provider profile per controller endpoint – `legion-mcp`,
  `legion-a2a`, `legion-git` and `legion-git-push` (below) – each with a `token_grant` that makes
  the gateway's sandbox supervisor fetch the sandbox's JWT-SVID
  (`spiffe://…/openshell/sandbox/<sandbox-id>`) and exchange it at the controller's
  `POST /oauth/token` (OAuth2 client_credentials, `jwt-spiffe` client assertion). The controller
  verifies the SVID against the SPIRE JWKS (signature, issuer, audience, expiry, sandbox prefix),
  maps the sandbox id to the one ticket it serves and issues a short-lived, **ticket-scoped**
  token (sandbox, change, ticket, role) signed with a key that never leaves controller memory.
  The gateway injects it as `Authorization: Bearer` into requests to the controller only. Every
  MCP and git request is then bound to the sandbox's live run, which must match the token's
  change, ticket and role; when the run ends, the token is useless.
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
  * `legion-a2a` – `protocol: rest` for `/a2a/**` on the controller's dedicated A2A port, for the
    `a2a` CLI (`sandbox.a2a_binaries`): agent-to-agent messaging (§5.15).
  * `legion-git-push` (roles that commit: implementer, scaffolder, oracle) or `legion-git` (every other role)
    – `protocol: rest` for `/git/*.git/**` on the controller's dedicated git port, for git. Both
    allow fetch and `git-receive-pack`; `legion-git` also denies `git-receive-pack`, and a deny
    rule applies across the whole effective policy, so a sandbox attaches exactly one of them.

  The operator UI/API listens on a separate port that is never exposed to sandboxes; the token
  endpoint has its own port for the gateway supervisor.

  Verifier sandboxes attach `legion-git` (fetch only) and nothing else.

  With an environment (§5.3a), the policy of a mapped role's sandbox additionally holds
  its profile's network policies (`env_<name>`, each open to the binaries that brought it), and the
  sandbox runs the profile's image and resources.
* **Agents push; the controller hosts the remote.** The controller serves its bare hub repository
  over git smart-HTTP (`git http-backend`) at `/git/<repo>.git`. Every live run may fetch. A run
  may push **only** to its ticket's branch `refs/heads/legion/<change>/t/<ticket>` (enforced by a
  `pre-receive` hook fed by the controller); ref deletion is refused, and judges may not push at
  all. The change's integration branch (`legion/<change>/integration`, the root's branch) and
  `refs/legion/**` are written only by the controller. When a report with a commit is accepted
  the controller pins it under `refs/legion/evidence/<run>`, so evidence outlives the branches.
* **Documents** are written by agents directly to the versioned store through MCP `docs_write`.
  Every change has its own namespace `changes/<change-id>/`, and **every ticket its own
  directory** `tickets/<ticket>/`: a ticket's agent writes only there. Agents use paths relative
  to their change (`tickets/design/spec.md`); the tools resolve them to
  `changes/<id>/tickets/design/spec.md`, and attestations are always stored fully qualified
  (`file:changes/<id>/tickets/design/spec.md@2`). Agents may read any change's documents
  (cross-change context) except `*/control/runs/*/log*`.
* **Outputs are named.** A role declares the outputs it must produce (`roles.yaml` `outputs`:
  `spec`, `oracle`, `tickets`, `specs*` – a `*` is a list); the agent's report says which
  documents they are (`outputs: {spec: "file:…@3"}`), and the accepted report pins them on its
  ticket. Sign-offs and consumers name outputs, never paths (§4.2).
* **MCP tools** (every call bound to the calling sandbox's live run via its gateway-injected token):

| Tool | Purpose |
| --- | --- |
| `get_assignment()` | who the run is (run id, role, ticket, attempt, write scope) and its assignment: goal, work, inputs at their versions, findings to address, base sha, branch, the outputs to report |
| `docs_list(prefix)` | list documents and latest versions |
| `docs_read(path, version?)` | read an immutable version (latest by default) |
| `docs_history(path)` | all versions with author/run and sha256 |
| `docs_schema(path)` / `schema_get(name)` | the schema a path is bound to / a schema by name |
| `docs_write(path, content, base_version?, dry_run?)` | create version N+1; content must match the path's schema (G-W3). `base_version` must equal the current latest (optimistic concurrency); omit only for a new path. Scope-checked. Returns `file:<path>@<N>`. `dry_run`: every check (G-W1..G-W4), nothing written or counted; returns the attestation the write would produce (or `unchanged`) |
| `verify_attestation(ref)` | check that a `file:`/`git:` reference resolves |
| `progress(message, percent?)` | heartbeat shown live in the UI |
| `report(status, summary, attestations, outputs, data?, blocked_reason?, missing_decision?, dry_run?)` | submit the result; guards run synchronously; returns `accepted` or the list of violations. `dry_run`: every guard, but no attempt is counted (G-R6) and nothing is recorded or applied; returns `would_accept` and the violations (timeline event `report_checked`) |
| `ticket_status(ticket?)` / `workflow_context()` | where work stands (§5.15) |
| `verification_request(sha?)` / `verification_status(id?)` / `verification_evidence(id)` | the verification service (§5.7) |
| `inbox()` / `a2a_reply(task_id, text, state?)` | the agent's A2A inbox (§5.15) |

### 2.1 State and persistence

The document store and the git hubs are the **only durable state**. Several changes can run side
by side under one controller; they share `max_parallel` but nothing else.

**Workspaces.** The config names one or more workspaces (`workspaces:` – `name`, `source`,
`base_branch`, `publish_prefix`): the repositories changes are made in. Each has its own bare
hub (`<state_dir>/git/served/<name>.git`), cloned from its `source` (none: a new, empty
repository). A change is made in exactly one (`Change.workspace`, required when it starts); its
attestations name it (`git:<workspace>@<sha>`), and its runs' git credentials reach its hub and
no other. `legion/*` is the hub's own namespace (integration and ticket branches) and is never
published; a finished change is published as `<publish_prefix><change-id>` (default `agent/`,
§5.10c).

| Where | What |
| --- | --- |
| `changes/<id>/tickets/<ticket>/` | everything a ticket's agent wrote – its outputs (`tickets/intake/requirements.md`, `tickets/design/tickets.json`, `tickets/T1/evidence.md`, `tickets/review:T1/review.md`) – and, for a sign-off, the controller's `signoff.json`: the decision, approver, time, reason (a rejection) and each subject at the exact version shown. A rerun criterion keeps its id; the store's versions keep its rounds apart |
| `changes/<id>/control/runs/<run>/` | each agent run (run ids are unique): `assignment.json` from dispatch – its ticket and attempt, role, soul, sandbox and its SPIFFE sandbox id, branch and the shas it works at, the session it continues, its write scope, the outputs it must report and its context, **the asset-set sha, rendered-prompt sha and the template values bound into its prompt**, its rendered assignment – `outcome.json` once it is no longer live (status, failure, exit, the session it left, its report and reply, what it used; a new version as its conclusion moves on), and `log`, `log.2` … – the agent's whole output, in parts of ≤1 MiB (a log over 64 MiB keeps its first and last 32 MiB; each stream-json event carries `legion_ts`, the time the controller received it; not readable by agents). No credential exists to store. What only a live run has – its stage, latest progress message, A2A bookkeeping – stays in memory |
| `changes/<id>/control/verifications/<id>/record.json` | each verification (§5.7) |
| `changes/<id>/control/workspace.json` | the change's base (§5.0a) |
| `changes/<id>/control/environment/` | `build.json` (the provisioned environment) and `provision.log` |
| `changes/<id>/control/pr.json` | the published pull request (§5.10c): url, number, branch, head, labels |
| `changes/<id>/control/ended.json` | who abandoned the change, where it was and why (§5.13) |
| `changes/<id>/control/proposals/<ticket>-<n>.json` | the resolver's proposals (§5.16) |
| `changes/<id>/control/digests/<run>.json` | per run: tool calls and errors, repeated commands and reads, slowest tool calls, longest model waits, turns and cost |
| `changes/<id>/control/metrics.json` | the change's metrics, computed by the controller before each retrospective round (§5.13) |
| `changes/<id>/control/reflections/<run>.md` | the agent's own account of what cost it time and where its guidance failed (§5.13) |
| `changes/<id>/control/timeline/<n>.jsonl` | the controller's events (dispatch, sandbox, verification, rework, human actions), written in chunks |
| `changes/<id>/control/assets/<sha>.json` | the hash of each asset file of an asset set the change's runs used |
| `state.db` tables `changes`, `tickets`, `hooks` | the change's live state – what a restart loads, one row per record, updated in place as the change saves. `changes`: the change record (goal, workspace, base, tier, the **pointers** – the approved version of each named output –, …); `tickets`: every ticket of the tree, its definition and its state (status, attempt, commit, findings, waiting reason, outputs, runs, kept sandbox); `hooks`: the side processes outside the tree (the retrospective). The tickets are the state: there is nothing to replay. Which sandboxes exist and which are stopped is not stored: on start the controller lists each change's sandboxes (`openshell sandbox list --selector legion.change=<id>`, every page) and takes them as they are – a run's sandbox not listed is gone (only a listing read in full says so), one labelled for the change that no run knows is deleted |
| `journal/<id>.jsonl` (in `state_dir`, not the store) | the change's history, append-only and never read on restart: one JSON line per save, each entry a record's changed fields – every transition an entry, so the journal is each ticket's and run's audit trail |
| git `legion/<id>/integration`, `legion/<id>/t/<ticket>`, `refs/legion/evidence/<run>` | the root's branch (the change's result), ticket branches, pinned evidence |

Rendered prompts are not stored: a run is labelled with the SHA-256 of the asset set and of its
rendered soul + prompt, and keeps the template values it was rendered with. Re-rendering with those
values reproduces the prompt exactly while the pin matches; otherwise the recorded asset hashes
say which files have changed since (`Assets.check_prompt`).

On disk, `state_dir` is set once per controller and holds only `state.db` (the store and the
changes' live state), `journal/` (each change's history), `git/` (the bare remotes) and `tmp/`
(sandbox payloads, deleted after upload, and live agent logs). `tmp/` is wiped on start. The
access-token signing key exists only in memory, so tokens issued before a restart stop working
(gateways simply request new ones). **On restart** the controller rebuilds every change from its
state tables, its documents and the gateway's list of its sandboxes; every ticket under way goes
on **at the same attempt** (a restart is never an attempt): a run whose report was accepted is
concluded, any other agent's work runs again – a new run in the ticket's kept sandbox, its session
resumed –, a merge is made again (one made before the restart is found made), verifications,
provisioning and sign-offs resume.

### 2.2 Attention and notifications

What a decision owner should look at is derived from the change's state by one function
(`legion_orch/workflow/attention.py`), which the UI, the CLI, the change list and the notifier
all use. Nothing here gates the workflow; it only says what is waiting.

| Item | When | Blocking |
| --- | --- | --- |
| `gate` | a sign-off is waiting for a decision owner (H-1, H-E1, H-2, H-2b, H-3, any the plan adds) | yes |
| `escalation` | a ticket is `waiting` on a human: why (§4.4), its findings, the resolver's proposal | yes |
| `paused` | the workflow is paused, and by whom (§4.6) | yes |
| `question` | an agent asked the humans (A2A `ask_human`, §5.15) | yes |
| `change_ended` | the change is `READY` or `ABANDONED` | no (inbox) |
| `retrospective` | a retrospective report is ready (each attempt is a new item) | no (inbox) |

**Needs you** lists the blocking items: they stay until the decision is made. The **inbox** holds
the others: nothing waits for them, and the UI keeps which ones this browser has read.

Every save compares the items with those of the previous save: a new item is recorded on the
timeline (`attention`, `raised: …`) and notified, a resolved one is recorded with how long it
waited (`resolved after 9h02m: …`). A ticket's `since` records when it last changed status.
After a restart the items already waiting are taken as known: they are neither raised nor
notified again.

**Notifications** (config `notify:`) go from the controller only; sandboxes never reach a sink.
Each sink – `webhook` (JSON POST, optionally signed `X-Legion-Signature: sha256=<HMAC>`), `ntfy`
(a topic URL) or `command` (JSON on stdin) – receives the item kinds in `notify.items` (default
`gate`, `escalation`, `paused`, `question`) or its own `items`. A blocking item still
waiting after `notify.remind_after_s` is sent again; `notify.resolved: true` also sends its
resolution. A delivery is tried three times, then recorded on the timeline (`notified`) as sent
or failed; it never blocks or fails the change. The payload carries the item, the decision
owners and a link to it in the UI (`notify.ui_url` + `#<change>/overview/<ticket>`). An
escalation's reason can quote agent output, so it is cut to 300 characters; no document content
is sent.

**While you were away.** `GET /api/digest?since=<iso>` (UI: shown after 30 minutes away;
CLI: `legion-orch digest --since 8h`) counts each change's timeline events since then – runs
started, reports accepted and rejected, reworks, merges, escalations, human decisions – without a
model call, and lists everything that needs you in full.

---

## 3. Risk tiers and their policies

Intake scores twelve risk signals 0–3 (`security, authorization, data_migration,
external_side_effect, public_api, ui, performance, infrastructure, irreversible_action,
scope_breadth, novelty_uncertainty, verification_quality`).

* Threshold tier from the total: `R0` 0–2, `R1` 3–5, `R2` 6–8, `R3` ≥ 9.
* Hard floors: any signal at 3 ⇒ `R3`; a score of 2 on any signal other than
  `novelty_uncertainty` / `verification_quality` ⇒ at least `R2`.
* Tier = max(threshold, floors), computed from the `risk` output H-1 signs; signing it fixes the
  tier and **applies** it: the skeleton tickets only that tier has (`acceptance` at R2+, its
  `baseline` at R3) are linked into the tree, and the tier's attempt limits set on the tickets not
  yet started.

Each tier is a **policy** in [`assets/workflow.yaml`](assets/workflow.yaml) (`tiers`): rules a
tree must keep, stated by ticket **type** and order – never by id, since ids are only names. A
tier may `extend` another:

| Rule | Says | R0 | R1 | R2 | R3 |
| --- | --- | --- | --- | --- | --- |
| `exists` | the tree contains a ticket of each type | intake, planner, provision, integration, publish | as R0 | + oracle | + baseline |
| `order` | each ticket of a type finishes before (`after`) / starts after (`before`) one of another | builds after provision and before the integration (the skeleton orders everything else) | as R0 | + builds after the oracle | + builds after the baseline |
| `every` | the criteria every ticket of a type has | build, scaffold: verify, can-merge | build, scaffold: verify, reviewer, can-merge | + oracle: preflight, sign-off | + baseline: sign-off |
| `labels` | the criteria a ticket labelled so (labels are inherited) has | – | `security`: security reviewer | as R1 | as R1 |
| `limits` | attempts per type (default 3) | implementer, scaffolder, planner 3 | as R0 | as R0 | as R0 |

"Finished before it starts" is read off the tree (`rules.precedes`): at the two tickets' nearest
common ancestor, an earlier subtask group, a subtask before a criterion, or an earlier criterion;
tickets of one group run in parallel, and an ancestor is never before its descendants.

**The policy guard** (`check_policy`) checks every tree before it applies: the planner's (its
`tickets` output, dry-run when the planner reports and again when H-E1 applies it) and every
replan. A tree that breaks a rule is refused with the rules it breaks; the skeleton itself is
checked as a partial tree (rules about types still to be planned don't apply). `legion-orch
check` validates the file, and every run gets the skeleton and the policies as the generated
`legion-workflow` skill, so the planner and the resolver plan into them.

`workflow.yaml` also says which **criterion types** a decision owner may waive (`criteria`:
verify, preflight, can-merge and sign-offs may not) and the **hooks** – the side processes
outside the tree (`on_complete: retrospective`, `on_escalation: resolver`).

---

## 4. Tickets

### 4.1 The ticket

A ticket (`legion_orch/workflow/tickets/model.py`) is:

| Field | Meaning |
| --- | --- |
| `id`, `parent`, `role` | its name (unique in the change, path-safe), its parent, and whether it is the `root`, a `subtask` or a `criterion` of its parent |
| `type` | `grouping` (no work of its own: its subtasks), `agent:<role>`, `human:signoff`, `controller:<action>` (`sync`, `verify`, `preflight`, `baseline`, `can-merge`, `provision`, `publish`) |
| `goal`, `work` | what done means, in words; its role's contract (`roles.yaml` `work_schema` – an implementer's instructions, files, verification commands, requirements) |
| `inputs` | the pointers it consumes: approved outputs by name (`spec`, `oracle`, `environment`); its run is given them at their approved versions |
| `labels` | risk labels; its effective labels are its own and its ancestors' (add-only) |
| `subtasks`, `criteria` | groups of child ids – groups in order, a group's tickets in parallel –; criteria in order |
| `severity`, `waivable` | (criteria) what a failure does – `rework` the ticket, or `escalate` (always, on a grouping ticket) –; whether a human may waive one |
| `signs`, `applies` | (sign-offs) the outputs it signs, by name, and `head`; what its pass applies (`tier`, or a signed replan such as `tickets`) |
| `limit` | the attempts its own work is allowed |
| state | `status`, `attempt`, `sha` (its commit: the accepted report's, or a branch owner's head), `findings` (the failed criterion's, its next round's input), `waiting` (why it waits), `outputs` (pinned from its accepted report), `runs`, `sandbox` (kept across its attempts), `since` |

**Branches follow the tree.** A ticket owns a branch if it is the root (`legion/<change>/integration`),
an agent's own ticket, or a grouping ticket with criteria (they judge its subtree's work alone);
any other works on its nearest owning ancestor's. A branch starts at its parent's head when the
ticket starts, and a ticket's commit merges into its nearest owning ancestor's branch when it
passes (§4.5).

### 4.2 The skeleton, outputs and sign-offs

Every change starts from `workflow.yaml`'s `skeleton` (shown at the top): `requirements`, `plan`,
`delivery`, `evaluate` and `release`, with the bare minimum under them. The planner fills
`delivery` (its builds; `delivery`'s own criteria – the oracle's commands at its head, then the
integration – are the skeleton's) and may add to `evaluate` and `release`; its output
`tickets` is the **first replan** (§4.6), applied when H-E1 passes.

A **sign-off** (`human:signoff`) is a criterion of the work it checks. It names the outputs it
signs (`signs: [spec, specs, oracle, environment, tickets]`), resolved against that work: its
owner's pinned outputs, else the one ticket in the owner's subtree that produced the name (none,
or several, is a fault a human fixes); `head` is the commit the owner's criteria judge. The
subjects are resolved when the decision owners are asked; one that changes before they decide is
asked again. A pass records `tickets/<sign-off>/signoff.json` and moves the change's **pointers**
to exactly what was signed; a rejection is a failed criterion (the owner reworks with the reason,
or – a grouping owner – escalates). A sign-off that `applies` something pauses the workflow while
it is under way and applies it with its verdict: `tier` (H-1, §3) or the replan it signs (H-E1:
the planner's `tickets`) – a replan that breaks the tier's policy fails the sign-off, its
violations the findings, so the planner reworks.

| Sign-off | Criterion of | Signs | Applies |
| --- | --- | --- | --- |
| H-1 | `intake` | `requirements`, `risk` | `tier` |
| H-E1 | `design` (after the critique) | `spec`, `specs`, `oracle`, `environment`, `tickets` | `tickets` |
| H-2 | `oracle` (after preflight), R2+ | `tests` | – |
| H-2b | `baseline`, R3 | `baseline` (the controller's record) | – |
| H-3 | `evaluate` | `head` (the integrated change), `pr` | – |

### 4.3 Lifecycle

```
pending ─start→ working ─work done→ subtasks ─every group passed→ checking ─every criterion passed→ [merging] → passed
                   │                    │                             │
                   │                    └ a subtask waiting: it stays here until that is resolved
                   │                                                  └ a criterion fails:
                   │                                                      rework   → working (attempt+1, its findings)
                   │                                                      escalate → waiting
                   └→ waiting: attempts used up, the same findings again, a system error past its
                               backoff, an exceeded budget, a BLOCKED agent
waiting ─retry / waive→ back where it was       ─replan→ failed (immutable)
any ─cancel (a replan, abandon)→ cancelled              a criterion's fail verdict: rejected
```

* **Grouping tickets** run their subtask groups, then their criteria; one with no subtasks waits
  at once (`empty`): no stage is legitimately empty.
* **Criteria** run in order, after the work and the subtasks, stopping at the first failure; a
  new attempt reruns every criterion from the first, on the new commit, each a fresh judge. A
  criterion's outcome is its verdict (`passed` or `rejected`); a waived one counts as passed.
* **Merging.** A ticket with a commit that passes its criteria is merged into its parent's branch
  first (§4.5); merged, it passes, and the parent's commit is the new head.
* **Passed is closed.** Nothing is ever added under a passed ticket, and it never runs again.

### 4.4 Attempts, errors and waiting

An **attempt** is one round of a ticket's own work. Only the work's own failures count: a
failed `rework` criterion, the agent giving up (its report submissions used up, G-R6; a run that
ends without an accepted report). Past `limit` the ticket
waits. Everything else waits on a human without costing one. Each executor ends in one outcome
(`tickets/executors/base.py`): `Done` / `Judged` (a criterion's verdict), `GaveUp` (an attempt),
`Blocked`, `Fault` (system: retried `limits.system_retries` times from `limits.system_retry_s`,
doubling; a fault no retry can fix waits at once) or `OverBudget`.

| `waiting` | Why | Remedies |
| --- | --- | --- |
| `attempts` | its attempts are used up | waive (the failed criterion, if waivable), replan |
| `repeated` | a criterion failed with the same findings as on the previous attempt (G-RV5) | waive, replan |
| `escalated` | an `escalate` criterion failed (on a grouping ticket: any) | waive, replan |
| `system` | a system error outlasted its backoff | retry, replan |
| `budget` | the run timeout (`limits.run_timeout_s`) was exceeded | retry, replan |
| `blocked` | its agent reported BLOCKED (G-R2): a decision it lacks | retry (with an `answer`, which its next round is told), replan |
| `empty` | a grouping ticket with no subtasks | replan |

**Retry** runs the same ticket again – same id, same attempt count. **Waive** (`waive:<criterion>`
on the waiting ticket, with a reason) counts its failed criterion as passed. **Replan** (with its
`plan`) changes the constraints: the plan either fails the ticket – immutable from then on, its
place taken by new tickets – or adds to it (fix builds into a `delivery` whose integration
failed); it is checked at once and applied as soon as nothing is active (§4.6). Without a plan,
the workflow waits paused for a change-level `replan`. Abandoning the change is always possible (§5.12). Every
waiting ticket is sent to the resolver, which proposes one of these (§5.16).

### 4.5 Merges

`controller:can-merge` is an **early check**, so judges don't review a commit that will not merge:
the commit rebases cleanly onto the branch it merges into (G-A2). It is not a guarantee – parallel
siblings can both pass it, and the first to merge moves the head. The **merge** itself is not a
ticket but a rule: when a ticket with a commit passes its criteria, the runner merges exactly that
commit into its parent's branch, checked again under the branch's lock (`merge-tree`, then a
compare-and-swap `update-ref`). A conflict there is a failed "can merge" – a rework onto the new
head (an attempt, the finding names it) or, a grouping ticket, an escalation; a git error is a
fault. A merge that is due always runs, paused or not.

### 4.6 Pause and replans

**Pause** (change action `pause`; `resume`): nothing starts from then on – no ticket, no
criterion; work under way goes on to its next conclusion point (its work done, its verdict given,
its merge made) and rests there (`paused`, holding what it does next); on resume every ticket goes
on from where it rests. Each pause has a reason, and each resume lifts only its own.

A **replan** is a document (schema in `tickets/plan_doc.py`): `add` (tickets with their criteria
and subtasks, each `into` a parent at a subtask group or as a criterion), `cancel` and `update`
(tickets not yet started) and `fail` (work in progress that extra tickets cannot resolve). It
applies only while the workflow is paused and nothing is active, all of it or none of it, and
must keep the tier's policy (§3). Tickets are added only after a ticket's completed groups and
never under a closed one: finished work is never undone, corrections are new tickets – a failed
integration is followed by fix builds added to `delivery`, after which its criteria judge the
new head again. A decision owner applies one with
the change action `replan` (the replan document in `plan`), typically the resolver's proposal;
the planner's `tickets` is applied by H-E1. There is no limit on replans: each is started by a
human.

**Revised outputs** (`tickets/stale.py`). A replan that revises an approved output – a ticket it
adds produces, say, a new `environment` – says only that; what follows is mechanical:

* the producing ticket gets a sign-off of what it revises – a copy of the passed sign-off that
  approved those outputs, signing just what it produces (`H-E1.2`) – unless the replan gives it
  one. If what it signs includes what the original applies (the planner's `tickets`), approving
  it applies that too: a revised design's plan adds builds and cancels pending ones, against the
  tree as it is, under the tier's policy like any replan;
* when that sign-off passes and moves a pointer to a **different** version, every finished
  ticket that consumed the old one (its `inputs`) and whose type `workflow.yaml`'s `stale` says
  to `redo` – the provisioning, the oracle – gets a new instance (`provision.2`: the same work,
  fresh criteria; a redo type has one ticket per change, so instances are matched by type – the
  id is only a name) in a new subtask group right after the producer, applied with the sign-off's
  verdict (held, and the workflow paused, until nothing else is active). Every other finished
  ticket stands: builds' code is committed, and the replan itself adds new builds or cancels
  pending ones; the baseline is a snapshot from before any build. A version unchanged by the
  revision calls for nothing.

### 4.7 Run status

`STARTING -> ACTIVE -> REPORTED -> ACCEPTED` (its outcome went to its ticket);
`ACTIVE -> FAILED` on crash, timeout or `max_report_attempts` rejected reports.

**A run ends when its agent's turn does.** The `report` tool checks a report as it is filed, so a
rejected one can be fixed, and records an accepted one (`REPORTED`, with the instruction to end
the turn) – but the ticket does not move until the agent ends its turn and its process exits:
then the role's handler reads the report – its commit, its outputs, a judge's verdict – and the
ticket's executor takes the outcome (a BLOCKED report waits on a human). A turn that ends without
an accepted report fails the run. An agent still in its turn 300 s after its report was accepted
is stopped; a run whose turn ended while the controller was down is concluded after the restart,
its report checked again.

A live run's **stage** records how far its sandbox got: `PREPARING` (OpenShell is creating the
sandbox) -> `LAUNCHING` (the payload is in place; the setup script clones the working copy and
starts the agent) -> `RUNNING` (the agent's first MCP call: it is up and reaches the
controller) -> `EXITED`.

A failed run records its **failure source** and the stage it had reached, which decides its
outcome (§4.4):

| Source | When | Outcome |
| --- | --- | --- |
| `AGENT` | The agent failed: its CLI ended with an error result (API error, turn limit), it finished without an accepted report, or `max_report_attempts` reports were rejected. | an attempt |
| `SYSTEM` | What the agent runs on failed: sandbox creation or upload, the setup script, or the process exited with no result from the agent (crash, command not found, killed). | a fault: retried with backoff |
| `TIMEOUT` | The run exceeded `limits.run_timeout_s`. | over budget: waits |
| `CONTROLLER` | The controller failed while concluding its report. | a fault |

A sandbox that was never created is forgotten by its ticket, so the next round creates one. A
fresh implementer sandbox continues from the ticket's branch as pushed to the controller; work
that was never pushed is lost.

---

## 5. Ticket catalogue: guards, prompt, skills, soul

Universal guards apply to every agent ticket and are listed once. Each role's documents are
written under its ticket's directory and named by its outputs; its soul, skills, outputs and
git requirement are in [`assets/roles.yaml`](assets/roles.yaml).

### 5.0 Universal guards

| ID | When | Rule |
| --- | --- | --- |
| G-D1 | dispatch | A ticket's work starts only when the rules start it: every earlier subtask group of its parent passed, its own subtasks before its criteria, the criteria before it passed – and the workflow is not paused. |
| G-D2 | dispatch | Assets resolve: soul, skills and prompt exist; every `{{var}}` is bound; the prompt pin is recorded on the run. |
| G-D4 | dispatch | Independence: a criterion's agent (critic, reviewers, pr-reviewer) gets a fresh sandbox and a run id distinct from the work being judged, every round. |
| G-R1 | report | `status ∈ {DONE, BLOCKED}`. |
| G-R2 | report | `BLOCKED` requires non-empty `blocked_reason` and `missing_decision`. |
| G-R3 | report | Every attestation parses and resolves (`file:` version exists; `git:` object exists in the controller's git remote). |
| G-R4 | report | Every output the role declares is named in `outputs` (a `*` output: a list); each document is in the ticket's directory, at its **latest** version, and was **written by this run** – or written again unchanged by it, which creates no new version (G-W2) but counts as its own. |
| G-R5 | report | The role's required git attestation is present (`commit`, `candidate` – the commit it judges – or `head` – its base –, see roles). |
| G-R6 | report | After `max_report_attempts` rejected reports the run fails (an attempt). Dry runs (`report(..., dry_run=true)`) are not attempts. |
| G-R7 | report | `data` matches the role's `report_schema` (`roles.yaml`): verdicts, findings, results. |
| G-R8 | report | Size limits: summary and reasons ≤ 2,000 characters, ≤ 100 attestations, `data` ≤ 256 KiB (report schemas also cap item counts and text lengths); the checks of one report are time-limited (`limits.report_check_timeout_s`). Concluding an accepted report is transactional: any controller error restores the state and fails the run (a fault). |
| G-W1 | docs_write | Path resolves inside the run's own change and its ticket's directory; never controller-owned paths (`control/**`, a sign-off's `signoff.json`). |
| G-W2 | docs_write | `base_version` equals current latest (or path is new and `base_version` omitted). Content identical to the latest version creates no version: that version is returned (`unchanged: true`) and costs no G-W4 quota. |
| G-W3 | every store write | The content matches the schema bound to its path ([`bindings.yaml`](assets/schemas/bindings.yaml)); a writer may not pick a different schema. The store records the schema name and sha with the version. Applies to agents and the controller alike. |
| G-W4 | docs_write | Per-run quota: at most `limits.max_docs_per_run` document versions (default 200) and `limits.max_doc_bytes_per_run` bytes (default 50 MB). Refused writes say which limit was hit. |

### 5.0a `controller:sync` – the change's base (`workspace`)

The root's first ticket: nothing has a branch before the change has a base. The workspace's hub
is brought up to date with its `source`: cloned if
there is no hub yet, otherwise fetched, and its base branch fast-forwarded to the source's.
`legion/*` (every change's integration and ticket branches) is never touched. One sync of a hub
runs at a time: changes started together wait for each other, and one that waited takes the
result of the sync before it (no second fetch). The change is then based on the base branch's
commit: its integration branch, whether the repository is blank (§5.3d) and the base's setup
(§5.3c), recorded in `control/workspace.json`.

If the hub cannot follow its source – the clone's origin is not the configured `source`, the
source's base branch was rewritten (the hub's commit is not an ancestor of it), or the fetch
fails – the ticket waits on a human like any other (`system`, with what differs; an outage is
retried with backoff first): the decision owner fixes the hub or the source and **retries** the
ticket, or abandons the change. Run again (a retry, a restart), a change that has its base keeps
it.

### 5.1 `intake` – the requirements (`requirements` ▸ `intake`)

* **Soul:** `intake-analyst`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `risk-signals`, `blocked-protocol`
* **Outputs:** `requirements` (`requirements.md`), `risk` (`risk.json`)

| ID | Rule |
| --- | --- |
| G-I1 | `requirements` contains at least one requirement id `REQ-<n>`; ids are unique. |
| G-I2 | `risk` has all 12 signals, each once, integer score 0–3, a rationale that is not blank (the shape is the [`risk`](assets/schemas/risk.json) schema's, enforced on write). |
| G-I3 | If a `questions.md` is attested, every question is either answered in it or reported as `BLOCKED` (questions go to the human; nothing is guessed). |
| H-1 | A decision owner signs `requirements` and `risk`; the pass fixes the tier (§3) and moves the pointers. A rejection reworks the intake with the reason. |

### 5.2 `planner` – the design (`plan` ▸ `design`)

* **Soul:** `architect-planner`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `plan-format`, `sandbox-environment`, `blocked-protocol`, `language-preferences`
* **Inputs:** `requirements`, `risk`  **Outputs:** `spec` (`spec.md`), `specs` (a delta spec `specs/REQ-<n>.md` per requirement), `oracle` (`oracle.json`), `environment` (`environment.json`, §5.3a), `tickets` (`tickets.json`, the first replan: the builds under `delivery`, each with its criteria, then the integration; its decision about the skeleton's `scaffold` – its work, or cancelled –; additions to `evaluate` and `release`)

Each build is an `agent:implementer` ticket whose `work` is its contract
([`work-implementer`](assets/schemas/work-implementer.json): instructions, `files_modified`,
`files_forbidden`, `verification` commands, acceptance criteria, the requirements it serves, its
soul). Builds in one subtask group run in parallel; a later group's start from the
head the earlier groups merged into. The tickets' criteria follow the tier's `every` and `labels`
rules (§3) – the policy guard refuses a plan without them.

| ID | Rule |
| --- | --- |
| G-P1 | `tickets` parses as a replan (only adds, under tickets that exist, ids new and unique) and each ticket's `work` matches its role's work schema; `oracle` matches the [`oracle`](assets/schemas/oracle.json) schema. |
| G-P2 | Build ids are unique and the order the groups give them is acyclic. |
| G-P3 | Traceability: every accepted `REQ-*` is served by ≥1 build; every build cites ≥1 existing `REQ-*`. |
| G-P4 | Every build has ≥1 `verification` command that is not blank (≥1 acceptance criterion is the schema's). |
| G-P5 | Every build's `soul` is an implementer soul. |
| G-P6 | Builds that may run in parallel (one is not in a group before the other) have disjoint `files_modified`; that the paths are non-empty, relative and without `..` is the schema's (G-P1). |
| G-P7 | No `files_modified` entry touches a protected path (`oracle.acceptance_tests`, `oracle.protected_support`, config `protected_paths`, a pinned `mise.toml`). |
| G-P8 | R2+: `oracle` lists ≥1 acceptance test path, ≥1 acceptance command and ≥1 criterion mapped to a `REQ-*`. |
| G-P10 | R2+: `oracle` declares ≥1 verification surface, with unique ids and commands that are not blank (each surface's id, commands and pinned paths are the schema's, G-P1). |
| G-P11 | The plan decides about the skeleton's `scaffold` (§5.3d): it gives it its work (`update`) or cancels it – not both, not neither; in a blank repository it gives it its work. The scaffold's work meets G-P1, G-P4, G-P5 and G-P7 like a build's. |
| G-P12 | R2+: a criterion's optional `tests` are among `oracle.acceptance_tests` and its `commands` among `oracle.acceptance_commands`. |
| G-P13 | R2+: a command in `acceptance_commands` or `integration_commands` that only installs dependencies (`bun install …`, `npm ci`, `uv sync`, `pip install …`, …) belongs in `mise.toml`'s `setup` task (§5.3c): the verifier runs the setup, then acceptance, then integration – so an install placed later runs after the commands that need it. |
| H-E1 | After the critique passes, a decision owner signs the design – `spec`, `specs`, `oracle`, `environment`, `tickets` – and its pass applies `tickets` (§4.2): the builds are linked under `delivery`. A rejection reworks the planner with the reason. |

### 5.3 `critic` – the critique (criterion of `design`)

The critic judges the design's outputs: a fresh sandbox each attempt, given the design's outputs
(and its predecessor's critique, to read).

* **Soul:** `plan-critic`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `pre-mortem`, `finding-format`, `language-preferences`
* **Outputs:** `critique`

| ID | Rule |
| --- | --- |
| G-C1 | `data.verdict ∈ {PASS, REWORK}`; `REWORK` needs ≥1 finding; the report attests the design's outputs at the versions critiqued, which must be the latest. |
| G-C2 | `REWORK` reworks the planner with the findings – an attempt –; at the design's limit (the tier's `limits`) it waits on a human (`attempts`, or `repeated` for the same findings twice). |

With an environment configured the critique also covers `environment` (the `pre-mortem` skill checks
that every command can run in its role's sandbox).

### 5.3a Sandbox environment (when configured)

Part of the design: the planner also writes `environment` (schema
[`environment`](assets/schemas/environment.json)), given the library catalogue in its
assignment (`{{environment}}`). Without one configured it writes the default (no
profiles). The guards below are checked with the plan's.

The planner specifies the sandboxes for everyone who runs or works with the code: named
**profiles** (tools, optional extra network per tool, optional memory/vCPU/disk), and which
role (or the deterministic `verifier`) runs in which profile. Profiles are composed from the
**library** (`environment.library_dir`, default [`environments/`](environments/README.md)):
tools installed with mise, each declaring its binaries and the network policies it needs, and
network policies, credentialless endpoint lists that become rules in the sandbox policy. Selecting
a tool brings its network, open to that tool's binaries only; a profile adds per-tool extras or
goes `offline`. The spec may propose new library entries inline; they apply to this change once
signed off, and an operator can promote them into the library: the change-level action
`promote_proposal` (`legion-orch library promote`, by a decision owner), which copies the entry
from the signed spec version and never overwrites a library entry.

| ID | Rule |
| --- | --- |
| G-E1 | The planner's report names the latest `environment`. Every profile's tools, and the network policies they bring or the profile adds per tool, resolve to a library entry or a proposal (proposals may not shadow library names; `name@version` only for single-tool entries); every policy in use is reachable by at least one binary of the tools using it; every mapped profile exists; no two tools of a profile set the same environment variable to different values. |
| G-E2 | No network-policy endpoint targets the controller host (`server.public_host`, `server.token_host`) or loopback, and no tool environment variable has a credential-like name (`TOKEN`, `SECRET`, `PASSWORD`, `API_KEY`, …): the image environment is readable by every agent. Network policies cannot carry credentials (the schema has no field for them). |
| G-E3 | Requested resources are within `environment.max_memory_mib`, `max_vcpus`, `max_disk_mib`. |
| G-E4 | Only roles that run after provisioning, and `verifier`, are mapped (`intake`, `planner`, `critic` always use the default sandbox). |
| G-E8 | A proposed `mise` (§5.3c) parses; every profile of a role that runs the setup (those that work with a commit), and the verifier's, installs its `[tools]` at compatible versions (mise never installs at run time); when it has a `setup` task, each of those profiles has the `setup_network` policies. |

### 5.3b `controller:provision` – the environment, built (`plan` ▸ `provision`)

The signed-off `environment` (the pointer H-E1 moved) is resolved against the library again and
built: one image per (profile, base image) – `FROM sandbox.image` for agents, `FROM
sandbox.verifier_image` for the verifier – with the profile's tools installed by mise under
`/usr/local/share/mise` (read-only in every sandbox) and their `env` settings as image `ENV`, via
`podman build`, tagged per change and recipe (`localhost/legion-env:<change>-<recipe hash>`, or
pushed to `environment.registry`) and labelled `org.legion.change` / `org.legion.recipe`. Roles of
a change with the same recipe share an image; another change with the same recipe gets its own tag
on the same layers (the build cache makes it nearly free), so a change's images are its own to
remove when it ends (§5.14). Network policies carry no credentials, so nothing is created for them
in OpenShell: each is resolved into a `network_policies` entry (`env_<name>`, open to the binaries
that brought it) recorded with the build and added to the policy of every sandbox of the role. CPU
and memory become `sandbox create --cpu <vcpus>` and `--memory <n>Mi`; disk, which has no CLI
flag, is rendered through `policies/sandbox-resources.template.yaml` into `--driver-config-json`.
The result is recorded in `control/environment/build.json` and the change record; the build log in
`control/environment/provision.log`. Provisioning waits until no run of a mapped role is live and
no verifier sandbox is in use; meanwhile new runs of mapped roles and verifications wait for it.

| ID | Rule |
| --- | --- |
| G-PV1 | Every image a mapped profile needs is built or found. Otherwise a fault: retried with backoff, then the ticket waits (`system`; retry provisions again). |
| G-PV3 | With a pinned `mise.toml` that has a `setup` task: `mise run setup` succeeds at the change's head in a fresh verifier sandbox from this build. Otherwise the ticket waits with the output. |
| G-E5 | (dispatch of a mapped role, and every verifier sandbox) the build was made from the `environment` the pointer names. |

From then on every sandbox of a mapped role is created from its profile's image, with its
network policies in the sandbox policy and its resources; unmapped roles keep the defaults. The
build is fixed: later library edits do not reach it. A new build is a replan (a new `provision`
ticket, after a new sign-off of a changed `environment`).

### 5.3c Repository setup: `mise.toml`

Every sandbox that works on the repository is a fresh clone. The repository's `mise.toml` says what
a checkout needs: `[tools]`, installed when the environment's images are built, and a
`[tasks.setup]` task – dependency installs, code generation, a build the tests need – that the
controller runs after every checkout as `mise run setup`: first in every verifier sandbox (G-V3),
and once after the clone in the sandbox of every role that works with a commit (oracle,
implementer, reviewers, integration; a failure is logged and the agent may still work). Read-only
roles (intake, planner, critic) skip it. No `setup` task: nothing runs.

**Which version runs (G-MS1).** Only a pinned `mise.toml` (`change.setup_pin`, its git blob): the
version signed at H-E1, else the base branch's when the change started. The setup script checks
the checkout's file against the pin before `mise trust` and `mise run setup`; any other version is
not run. `mise.toml` is a protected path while a setup is pinned (G-P7, G-B3, G-INT2).

**With an environment configured** the planner proposes the file in the spec (`"mise": {"content":
…}`, with the network its setup needs in `"setup_network"`): unchanged, edited or new. H-E1 shows
it; on its pass the controller commits it to the integration branch (before any build) and pins
it; provisioning checks its setup runs in the verifier (G-PV3). **Without it** the operator's
images are assumed to have `mise`, and the base branch's setup task (if any) runs.

**In a blank repository** (§5.3d) the base's setup task is never pinned: what it needs – a
manifest, a lockfile – is what the scaffold creates.

### 5.3d `scaffolder` – the project's layout and tooling (`plan` ▸ `scaffold`)

The skeleton's `scaffold` ticket (`agent:scaffolder`) runs after provisioning and before the
oracle and every build: the project's layout, manifest, build, lint and test tooling and their
configuration – or a change to what is there (a new test runner, a restructured layout) – that
the acceptance tests and the builds rely on. The planner decides about it in `tickets` (G-P11):
it gives it its `work` (a build's contract, [`work-implementer`](assets/schemas/work-implementer.json),
`soul` included) with `update`, or cancels it when what is there will do. Its criteria are a
build's (verify, review, can-merge); it merges into the change's branch before the oracle starts,
so the oracle writes its tests for the project as the scaffold left it.

* **Soul:** the work's `soul`  **Skills:** as the implementer's  **Outputs:** `evidence`, git
  `commit` on its branch; its guards are the implementer's (G-B2, G-B3, G-B4).

When a change starts, the controller lists the base commit's tree. It is **blank** if every file
is documentation (`*.md`, `*.rst`, `*.adoc`), a top-level licence or notice file (`LICENSE*`,
`LICENCE*`, `COPYING*`, `NOTICE*`, `AUTHORS*`), `.gitignore`, `.gitattributes`, `.editorconfig`,
a top-level mise file (`mise.toml`, `.mise.toml`, `.tool-versions`: tool versions are not code)
or under `.github/` – including a repository with no files at all (a workspace without a
`source`). The change record keeps it (`blank_repo`). In a blank repository the scaffold is
required: none of the requirements' behaviour, a placeholder entry point, instructions that fix
every choice the acceptance tests depend on, and verification commands that pass without them.
Until it has passed no agent's sandbox runs the repository's setup (what it needs does not exist
yet); its own verification does, at its commit.

### 5.4 `oracle` (R2+) – protected acceptance tests (`plan` ▸ `acceptance` ▸ `oracle`)

* **Soul:** `test-engineer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`, `language-preferences`
* **Inputs:** `spec`, `specs`, `oracle`  **Outputs:** `tests` (`tests.md`), git `commit` on its branch

| ID | Rule |
| --- | --- |
| G-O1 | Changed paths `base..commit` ⊆ `oracle.acceptance_tests` ∪ `oracle.protected_support`. |
| G-O2 | Every acceptance test path exists at the commit. |
| H-2 | After preflight, a decision owner signs the oracle's `tests`; the pass merges its commit into the integration branch before any build. A rejection reworks the oracle. |

From the merge on these paths are **protected**: any later diff touching them fails G-B3 and
G-INT2.

**`controller:preflight`** (the oracle's first criterion): the repository's setup (§5.3c), then
the oracle's `acceptance_commands`, at the oracle's commit in a **fresh verifier sandbox** (G-V1
semantics: a new clone, the verifier image and environment profile) – not in the oracle's
sandbox, whose installed dependencies and build output would hide what a clean run lacks. Before
any build the commands are expected to fail: preflight passes if they **ran**. A failing setup is
a failed criterion (the oracle reworks); a command not found (G-V2) or an infrastructure error is
a fault.

Exit codes are only as trustworthy as the code a command runs: a test process that imports the
code under test can be made to exit 0. So the protected acceptance tests must be **black-box and
out of process** (they run the system as a separate process and check its outputs), and runner
configuration they depend on is listed in `oracle.protected_support` and protected like the tests
(G-P7, G-O1, G-INT2). The test-engineer soul requires this, the critic checks it, and humans
sign it at H-2. Per-build verification commands are a faster, weaker check backed by review.

The tests check what the criteria state and nothing more: a test per criterion (a few when it
names several cases), asserting the properties it names – not an exact output, snapshot, message
wording or format the criterion leaves open – and an edge case only where the spec names one.
A test that pins a shape the spec does not ask for fails correct builds, and the rework lands on
the implementer when the defect is the test's. So the planner writes criteria as observable
behaviour, fixing a format only where the requirement needs it, and the critic flags criteria
that pin one by accident.

### 5.5 `controller:baseline` (R3) – independent baseline (`plan` ▸ `acceptance` ▸ `baseline`)

What is true of the code before any build: which acceptance tests fail (as they should, until the
change is built) and which failures were already there. It is deterministic, so the controller
does it: the repository's setup and then the oracle's acceptance and integration commands, at the
head with the oracle merged, in a fresh verifier sandbox – the verification service, once per key
(§5.7). Failing commands are expected and recorded, not judged; a setup that fails, a command the
image lacks or the verifier's infrastructure is a fault (it waits on a human). The verification
record is its output `baseline`; the integration verifier is given it.

| ID | Rule |
| --- | --- |
| H-2b | A decision owner signs the `baseline` record – which commands already pass (they cannot show that the change works), which fail. A rejection escalates: rerunning the same commands at the same head would change nothing, so the remedy is a replan (a changed oracle, or abandoning the change). |

### 5.6 `implementer` – a build (`delivery` ▸ the planner's builds)

* **Soul:** the build's `soul` (`senior-developer`, `backend-engineer`, `frontend-engineer`)
* **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`, `language-preferences`
* **Outputs:** `evidence`, git `commit` on its branch
* **Sandbox:** created on its first round and **kept across its attempts** (the agent keeps its
  working copy and session); deleted when the ticket is closed.

| ID | Rule |
| --- | --- |
| G-B2 | The attested commit is the current tip of the ticket's branch in the controller's git remote (i.e. the agent pushed it) and descends from the run's base sha. |
| G-B3 | Changed paths `base..commit` ⊆ `files_modified`; none in `files_forbidden`; none protected. |
| G-B4 | The evidence document names every `verification` command of the build with its exit code. |

Its criteria, in order, by the tier's policy: `controller:verify` (§5.7), the review panel's
judges (§5.8), `controller:can-merge` (§4.5). A failure reworks the build with the findings, in
the same sandbox and session.

### 5.7 `controller:verify` – deterministic (criterion)

A fresh sandbox (network: controller git endpoint only, fetch-only grant) clones the commit the
criterion judges and runs the commands of the work it checks – a build's `verification`, else the
signed oracle's – after the repository's setup; results come back through `sandbox exec` output.

**The verification service** (`engine/services/verification.py`) runs every verification **once
per key**: the commit, the commands, the setup pin, the verifier image and the timeout. A passed
or failed run is the key's result for good: anyone asking again gets it without a new sandbox. A
result that says nothing of the commit – an infrastructure error, a command not in the image – is
not a result: asked again (a retry), it runs again. (Environment images have content-addressed
tags; a configured default verifier image should be pinned too – a moving tag such as `:latest`
is not part of the key's meaning.)

* **On push.** Every tip a run pushes to its branch is verified (its ticket's commands), so the
  criterion usually finds its commit verified. A newer push cancels its branch's verification
  that has not started yet.
* **Agents** ask over MCP: `verification_request(sha?)` (their ticket's commands; the pushed tip
  by default; `limits.verification_requests_per_run`), `verification_status`,
  `verification_evidence` (each command's exit code and output).
* **Limits.** At most `limits.max_parallel_verifications` verifier sandboxes run at once, inside
  `limits.max_parallel`, served by priority – a criterion's verification, then pushes, then
  agents.
* Runs are recorded on the change and in full at `control/verifications/<id>/record.json`.

| ID | Rule |
| --- | --- |
| G-V1 | Every command exits 0. Otherwise the criterion fails, each failing command's output a BLOCKER finding. The verifier sandbox is deleted as soon as the result is recorded. |
| G-V2 | A command that exits 127 (not found in the verifier image) is not the agent's to fix: the ticket waits (`system`) at once, without retries. **Retry** verifies again, e.g. after the image is fixed. |
| G-V3 | With a pinned setup (§5.3c), `mise run setup` runs first in every verifier sandbox (results mark it `setup`); if it fails, no other command runs and the verification fails like any other (the commit may have caused it). |
| G-MS1 | The setup runs only from the pinned `mise.toml`: a checkout whose file differs from the pin is not trusted and its setup is not run (the verification fails). |

### 5.8 The review panel (criteria)

The tier's policy gives every build its judges, each a criterion with its own fresh sandbox at
the commit it judges: R1+ `reviewer`; a ticket labelled `security` also a `security` reviewer. The
planner may add an `architecture` reviewer (skill `architecture-checklist`) or more. A judge may
read its predecessor's review from the earlier attempt.

* **Souls:** `code-reviewer`, `security-reviewer`, `architecture-reviewer`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format` (+ `language-preferences` for `reviewer`, `security-checklist` for `security`)
* **Outputs:** `review`, git `candidate`

| ID | Rule |
| --- | --- |
| G-RV1 | The review attests `git:<repo>@<candidate>` for exactly the commit under review. |
| G-RV2 | Each finding has `severity ∈ {BLOCKER, WARNING, SUGGESTION}`, `confidence ∈ {HIGH, MEDIUM, LOW}`, `title`, `fix`, ≥1 resolvable attestation. |
| G-RV3 | `PASS` is forbidden with any HIGH-confidence BLOCKER/WARNING; `FAIL` requires at least one. |
| G-RV4 | A `PASS` on the first attempt must list what was checked in `data.checked` (skeptical by default). |
| G-RV5 | Loop control: the same set of blocking findings on two attempts in a row makes the ticket wait (`repeated`) instead of reworking again; past its limit it waits (`attempts`). |

`FAIL` reworks the build: the implementer runs again **in the same sandbox and session** with the
HIGH findings as its input (attempt + 1), and every criterion runs again from the first.

### 5.9 Merging (controller)

| ID | Rule |
| --- | --- |
| G-A2 | The commit merges cleanly into the branch it merges into (`controller:can-merge`, and again at the merge, §4.5). A conflict ⇒ rework with the finding `rebase onto <head>` (an attempt). |

On a merge the ticket passes; its branch is deleted (the merge made its commit part of the
parent's history, and the evidence ref keeps every reported commit), its runs are accepted and
its sandboxes are deleted after their agents reflect.

### 5.10 `integration` – the builds together (`delivery`'s criteria)

`delivery` has criteria, so it has a branch of its own: the builds merge into it, and once they
all have, its criteria judge its head – `verify:delivery` (`controller:verify`: the oracle's
acceptance and integration commands, in a fresh verifier; nothing to run at a tier whose oracle
names none passes) and then `integration`, the integration verifier. Both pass: `delivery`
merges into the change's branch. A failure escalates (a grouping ticket's criterion): the remedy
is a replan that adds fix builds to `delivery`, after which every one of its criteria judges the
new head again, from the first (or a waiver of the integration's verdict).

* **Soul:** `integration-verifier`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format`
* **Outputs:** `report`, git `head` (the commit it judges: `delivery`'s head)  **Context:** every build's evidence and, at R3, the baseline record

| ID | Rule |
| --- | --- |
| G-INT1 | The attested commit is the head it judges; `data.verdict ∈ {PASS, FAIL}` with findings that resolve; `data.results` covers every acceptance and integration command. |
| G-INT2 | Protected acceptance test and `protected_support` blobs at head equal those at the oracle's commit. |

`release` (role, `release-planner` soul) is kept for a live-system workflow; no tier requires it,
and a plan may add it to `release`:

* **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`  **Outputs:** the
  release plan and rollback

| ID | Rule |
| --- | --- |
| G-REL1 | The release plan has `## Health criteria` and `## Rollback criteria`; the rollback names the revert target sha and recommends a recovery strategy on its own line, `Strategy: rollback` or `Strategy: forward-fix` (with a `Rationale:`). |

### 5.10b `describer` – the pull request (`evaluate` ▸ `describe`)

The description H-3 signs with the change, published as the PR.

* `describer` – **Soul:** `pr-writer`; **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `blocked-protocol`; **Inputs:** `requirements`, `spec`; **Outputs:** `pr`, git `head`. At the change's head, with its whole diff (`{{change_base_sha}}..{{head_sha}}`).
* `pr-reviewer` – a criterion the plan may add: **Soul:** `pr-reviewer`; **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`, `git-protocol`, `finding-format`; **Outputs:** `review`, git `head`. Judges whether the description is *faithful*: its motivation is the change's, every significant change in the diff is described, nothing described is absent, and what it says was verified was.

| ID | Rule |
| --- | --- |
| G-PR1 | `pr` starts with the PR's title (`# <title>`, at most 120 characters) and has the sections `## Motivation`, `## Changes` and `## Verification`; every requirement of the change is cited by its id. |
| G-PR2 | The review attests the latest version of `pr`; `REWORK` names at least one finding, each with evidence the report attests. |
| H-3 | A decision owner signs the change – `head`, the integrated commit, and `pr` – as `evaluate`'s criterion. A rejection escalates (a grouping ticket's criterion): the remedy is a replan, or abandoning the change. |

### 5.10c `controller:publish` – the result leaves the workflow (`release` ▸ `publish`)

`release` always publishes the root's result (its branch head when `release` started):

* **With a source on GitHub**: the head is pushed to the source as `<publish_prefix><change-id>`
  (default `agent/<change-id>`; `legion/*` is never pushed) and a pull request is opened from it
  against the base branch, **ready for review**: its title and body are the signed `pr`,
  followed by a footer the controller adds – `Legion change: <change-id>` and the risk tier. Its
  labels: `risk:<tier>`. A label the repository lacks is created (an existing one's colour is
  left alone); the family's labels on the pull request are made exactly the wanted ones (a
  stale `risk:R1` goes when the tier is R2), and other labels – a human's included – stay. Run
  again (a retry, a restart), it finds the pull request and brings it up to date; the branch is
  never force-pushed. The result is `control/pr.json` (url, number, branch, head, labels). `gh`
  runs with the host's authentication, never a sandbox's.
* **Otherwise** (no source, or one not on GitHub): the head is merged into the hub's base
  branch, the PR's text the merge commit's message.

A push or GitHub failing is a fault, retried with backoff, then the ticket waits (`system`); a
base branch it no longer merges into waits at once. Once `release` passes, the root passes and
the change is `READY`.

### 5.11 Later failures: replans

A failure found late – an integration that fails, H-3 rejecting the change – is fixed by
**planning new work on top of what was built**, never by reopening it: the ticket waits, the
resolver proposes a replan (§5.16), and a decision owner applies it (§4.6) – fix builds after
`delivery`'s completed groups, after which `delivery`'s criteria judge the new head again; a changed signed document needs a
new sign-off and new instances of the tickets that used it. No passed ticket runs again.

### 5.12 Change-level actions (human)

Recorded by a decision owner (UI, CLI, or `POST /api/changes/<id>/action`); a ticket's own
actions (`approve`, `reject`, `retry`, `waive`, `replan`) go to
`POST /api/changes/<id>/tickets/<ticket>/action`.

| Action | Does |
| --- | --- |
| `pause` / `resume` | §4.6 |
| `replan` | applies a replan document (`plan`), while paused and nothing is active (§4.6) |
| `abandon` | ends the change where it is: running work stops, everything not finished is cancelled; `reason`; `retrospective: false` skips the retrospective (§5.13) |
| `promote_proposal` | an environment proposal into the library (§5.3a) |
| `retrospective` | the retrospective again on an ended change, with an optional `focus` (§5.13) |
| `prune` | only once the change has ended: removes its integration branch and evidence refs (§5.14) |

### 5.13 Reflection and retrospective

**Reflection (automatic).** When an agent's sandbox is deleted because its work is done (its
ticket closed – not after a failure), the controller first resumes the agent's session
(`--resume`) for one reflection turn: what cost it time, which instructions were missing or
wrong, tool and environment friction, mistakes (for an implementer: each finding raised against
its work) and suggestions. The answer is stored as `control/reflections/<run>.md`. The agent still
has its whole context, which a later retrospective does not. If the agent runs in a new session
instead (the resume did not take), the reflection says so at the top and the timeline notes it.
Best-effort: a missing session, an error or `retrospective.reflect_timeout_s` skips it, and the
deletion goes ahead. Turn it off with `retrospective.reflect: false`; `reflect_skip_roles` lists
roles that never reflect – by default the judges (`reviewer`, `security`, `architecture`,
`critic`): what a judge missed shows in later outcomes, not in its own account (see
`findings.json` below).

**Retrospective** (`hooks.on_complete`). When a change ends – `READY` or `ABANDONED` – the
retrospective runs as a ticket outside the tree (a `hook`), even though the change is no longer
active, once the reflections of the sandboxes released at the end have finished, as they are
part of its material. `retrospective.auto: false` leaves it to a decision owner, who can also ask
for another at any time after the change has ended (change action `retrospective`, with an
optional `focus`). The change is *completed* once it is over; then it is cleaned up (§5.14). A
restart resumes it at the same attempt.

**Abandoning.** A decision owner ends a change early with **Abandon**, optionally giving a
`reason`. Who, where it was (the tickets waiting on a human) and why is recorded in
`control/ended.json` and the change record (`ended`, which the retrospective reads) and on the
timeline. The retrospective then runs on whatever the change has produced so far – also with
`retrospective.auto: false`, unless the action passes `retrospective: false`.

* **Soul:** `retrospective-analyst`  **Skills:** `harness-contract`, `docstore-protocol`, `attestation-format`
* **Material:** `<payload>/retro/` – reflections, digests, whole logs, timeline, run and ticket
  records, the change's documents, the current assets and a prompt check per run (agents cannot
  read run logs through MCP). Its context lists earlier changes' retrospectives.
* **Metrics:** `control/metrics.json` (`retro/metrics.json`), computed by the controller
  (`workflow/metrics.py`) before each round from the change's journal, its runs and their
  digests, so the numbers are counted, not estimated:
  * the first-pass rate of the tickets with work: passed on the first attempt, never rejected by
    a criterion, no wait on a human;
  * every rework, by the criterion type that caused it and by ticket;
  * the waits on a human, by reason and length;
  * the sign-offs decided and rejected, and how long they waited for a decision;
  * per role and per ticket: runs, how they ended, rejected reports, wall time, cost and tokens.
* **Judgement ledger:** `retro/findings.json`, built by the controller from the ticket and run
  records: per ticket with criteria each commit judged (verification, verdicts) and each finding
  with the attempt it was first raised, the attempts it came back in and the earlier commits the
  same judge reviewed without raising it; human feedback. The agent judges escapes (by diffing the
  pinned commits) and noise against the review criteria.
* **Outputs:** `report` (summary, review effectiveness, ranked findings with evidence and root
  cause, proposed changes as diffs against the assets, what is not worth fixing).
* It proposes; a human applies. Asking again runs a new attempt.

### 5.14 Cleanup

What a change leaves in the hub and on the controller's host, and when it goes:

| What | Goes |
| --- | --- |
| a ticket branch (`legion/<change>/t/<ticket>`) | when its ticket has merged: the merge made its commit part of the parent's history, and `refs/legion/evidence/<run>` keeps every commit an agent reported |
| leftover ticket branches and the change's environment images (local `podman` images labelled with the change) | once, when the change is completed (ended, its retrospective done), nothing of it is running (`change.cleaned_at`). A run after that – a retrospective asked for again – uses the default image. Images pushed to `environment.registry` stay: a shared registry is the operator's to clean. |
| `legion/<change>/integration` (the result) and `refs/legion/evidence/<run>` (every reported commit, so reviews' and reports' `git:` attestations resolve) | when a human **prunes** the ended change (change action `prune`, `legion-orch prune <change>`). The change's records stay; its `git:` attestations no longer resolve. |

### 5.15 Agent-to-agent (A2A)

A change's agents can talk to each other and to its humans over A2A (v1.0, JSON-RPC), with the
`a2a` CLI in their sandboxes ([a2aproject/a2a-cli](https://github.com/a2aproject/a2a-cli)). The
controller hosts every card on its A2A listener (`server.a2a_port`, provider `legion-a2a`); a
sandbox makes outbound calls only. The URLs name no change: the gateway-injected bearer names
the caller's sandbox, change, ticket and role, and each request is answered for that change only
(the registry and catalog list it, a card of another change is 404).

* **Cards** ([`workflow/cards.py`](legion_orch/workflow/cards.py)) say who an agent is – id,
  ticket, role, title – and nothing of its state (that is `ticket_status` and its tasks'). They
  are **pushed** to the A2A layer's catalog as a side effect of the workflow and kept nowhere
  else: a catalog that subscribes (the A2A listener, after a restart) is given every change's
  cards again. Each publication and withdrawal is a timeline event (`a2a`).
  * `orchestrator` – the controller itself, not a model, for as long as the change is active.
    Its one skill: `ask_human` (a question to the decision owners: a blocking attention
    item, notified like a sign-off, answered in the UI or
    `POST /api/changes/<id>/questions/<task>/answer`; the answer completes the task).
  * `resolver` – the change's resolver agent (§5.16), for as long as the change is active.
  * one per **agent that has started** on a ticket, `<role>@<ticket>` (`implementer@T1`,
    `reviewer@review:T1`, `critic@critique`), while it has a sandbox – running, or stopped
    between rounds – until its sandboxes are gone. An agent that has not started has no card: a
    message to it is refused. The controller's own tickets (verification, merges, sign-offs) have
    none.
* **Discovery** is one thing: `GET /a2a/ai-catalog.json`, an
  [AI Catalog](https://ai-catalog.io/) of the cards the caller may message (identifiers
  `urn:air:legion:agent:<card>`). `run-agent.sh` registers it as catalog `legion` before the
  agent starts (`a2a catalog add`, or `update` in a reused sandbox), and sets
  `A2ACLI_AGENT_CARD` to the orchestrator. Cards come and go as agents start and finish, so an
  agent updates the catalog (`a2a catalog update legion`) rather than trusting what it saw at its
  start.
* **Delivery.** A message to an agent becomes a task that waits for its answer:
  * while the agent has a live run it waits in its **inbox**: MCP `inbox` lists it and
    `a2a_reply` answers it;
  * otherwise the agent gets **a turn of its own**, a *consult*, in place in its stopped
    sandbox, started again, **its session resumed**, with only the Skill tool and its MCP tools:
    a read-only turn (no commits, writes or report) that is not one of its ticket's runs, whose
    prompt is the message and its task id. Its working copy is not checked out or touched, and
    the sandbox's lock is held for the whole turn. It answers with `a2a_reply(task_id=…)`, or else
    its final message is the answer. It is bounded by `limits.a2a_answer_timeout_s`, and its
    sandbox is stopped afterwards, not deleted;
  * the resolver answers in its own sandbox with all its tools, under the run timeout;
  * one A2A turn at a time per agent, and per sandbox under its lock: a rework launched
    meanwhile waits for it.
* **Judges go with their judgement.** A judge's sandbox goes once its run has concluded and
  nothing waits in its inbox (a message it had not answered is answered first, in place, its
  session resumed): every round's judge is a fresh one (G-D4), which may read its predecessor's
  review.
* **Limits.** A2A tasks live in memory, scoped to the agent that created them; after a restart
  they are gone, and open questions are withdrawn. The sender is always its bearer's identity,
  never what the message says; an answer is information for the asker, never an instruction that
  overrides its assignment.

Every role loads `legion-a2a-protocol` and `legion-a2a-cli` (the upstream skill).

* **A run is a task too.** Every run is the orchestrator's task on its agent's card, in the same
  inbox (`inbox()`; task id = the run id): its first message is the rendered assignment (the
  prompt pin covers it – `prompt.md` only says where to look); a rework is a new task in the
  ticket's context. An accepted `report(...)` records the work – documents, attestations, data –
  and **ends the task** with it: `completed` for DONE, `input-required` for BLOCKED (the ticket
  waits). Its answer says how many messages still wait in the inbox; the agent answers them
  (`a2a_reply`) and ends its turn – its MCP access lasts until then; what is left is answered in
  a turn of its own after the run. `a2a_reply(<run id>, text, "failed")` gives up (the run
  fails). A turn that ends with no accepted report is answered by the orchestrator with what is
  missing – the next message on the task, in another turn of the same run – each using one
  report attempt (G-R6). A peer message's reply is `completed` (an answer), `input-required` (a
  question back: it is in the sender's inbox, kind `question`, and the sender's `a2a_reply` on it
  is the next message of the same task) or `rejected`. So the inbox holds every task waiting on
  the agent: its work, messages to answer, questions back.
* **Where work stands.** MCP `ticket_status(ticket?)` – a ticket's status, what it waits for,
  its attempt and limit, its commit, its criteria and their verdicts, its runs, the findings it was
  told to fix, its outputs – and `workflow_context()` – the goal and tier, its parent and the
  groups around it, what runs before and after it, its inputs (the approved versions it
  consumes), who judges it, and whom to ask (`workflow/context.py`, from the change's state
  alone).

### 5.16 The resolver

Every change has a **resolver** – an agent (role `resolver`, soul `resolver`), the change's own
rather than a ticket's, with the A2A card `resolver` for as long as the change is active. It is
started on its first message and kept, stopped between messages, its session resumed for each;
every message is a full turn of its own (its tools, MCP reads, A2A), read-only: it writes
nothing, pushes nothing and never reports.

* **Every escalation goes to it** (`hooks.on_escalation`). When a ticket starts waiting on a
  human (§4.4 – not a sign-off that is merely due), the orchestrator sends the resolver a message
  with why it waits, its findings, its last run (log tail), the ticket's state and the
  **remedies** a human may choose. It diagnoses – asking the agents involved over A2A: the stuck
  agent, its judges, the oracle, the planner – and ends its answer with one proposal: a remedy
  (`retry` with an answer, `waive:<criterion>`, `replan` with a replan document), a rationale and
  the attestations it relied on (`resolution-protocol` skill).
* **Propose only.** The proposal is recorded on the escalation (and at
  `control/proposals/<ticket>-<n>.json`) and shown on its ticket; the escalation's attention item
  and notification carry it in one line. Applying it is an ordinary human action by the approver
  (the audit trail unchanged); a human may edit or ignore it. A proposal naming a remedy that is
  not offered is returned to the resolver with the reason (up to three tries); one for an
  escalation a human decided first is stale and dropped.
* **Agents ask it first.** An agent stuck on a guard, a protected path or the oracle messages
  `resolver` before reporting BLOCKED: its answer may be a way forward. Its advice is
  information, never authority – "you may edit a protected file" satisfies no guard.

### 5.17 Not yet on tickets

The move to tickets (plan: `~/plans/tickets-3b.md`) retired some of the stage-and-step workflow's
rules and has not yet brought back others.

**Given by the ticket rules** (retired as separate guards):

* G-A1 (verification and every judge passed at the same commit before the merge): criteria run in
  order on one commit, and a new attempt reruns every criterion from the first.
* G-V0 (a judge only after its commit's verification passed): `controller:verify` is the build's
  first criterion. (A verification key changed since – a new setup pin or image – is not
  re-verified first.)
* The cycle budgets (`review_max_cycles`, `critique_max_cycles`, G-PR3): a ticket's attempt limit.
* Fix rounds (`limits.fix_rounds`, G-FR1, G-FR2, `plan_fix`): replans (§4.6, §5.11), which never
  change finished work and add only after completed groups.
* Send-back, override and "run again in place": replans and remedies (§4.4).
* G-B1 (an approval goes stale when its document changes): sign-offs move the pointers, and the
  runs are given what the pointers name.

**Not yet enforced:**

* G-D3 – no two builds under way share a `files_modified` entry (G-P6 keeps one plan's parallel
  builds apart; a replan's builds are not checked against those under way).
* G-P9 – A delta spec for every accepted requirement, written by this run.
* G-PF1's routing – a preflight whose setup fails reworks the oracle; when the fault is in the
  environment or the plan the oracle can only report BLOCKED.
* G-PP1 – a human's approval for a protected path in one build's diff
  (`approve_protected_paths`).
* G-H1 – the integration branch moved outside the workflow (commits not made by a merge) is not
  detected before a merge or a run at the head.
* The ship gates and H-3's preconditions, with the human attestations behind them: G-AT1
  (`attest`, `control/attestations/<kind>.json`: `independent-baseline`, `security-evaluation`,
  `e2e-evaluation`, `architecture-review`, `rollback-evidence`, `forward-fix-evidence`,
  `release-observation`), G-SF1 (surface pins unchanged at the end, `approve_surface`) and
  G-OV1 (`add_gate`, `waive_gate`). What a tier must contain is now its policy (§3), and the
  final sign-off is H-3.
* The gate screens' traceability matrix (requirements → builds → oracle criteria and their
  results) and H-3's acceptance view: to be rebuilt on tickets with the UI (step 6).

---

## 6. Assets

Agent-facing text is stored as files under `assets/`, with this naming convention:

```
assets/
  roles.yaml               role -> soul, skills, outputs, work schema, report schema, git requirement
  souls/<soul>.md          agent persona, injected verbatim (never summarised)
  skills/<skill>.md        skill: frontmatter `description` (when to load it), then the body
  prompts/<role>.md        task prompt template for the role (file name = role name)
  workflow.yaml            the skeleton, the tier policies, waivable criteria and the hooks
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
* Templates use `{{var}}` placeholders. Available variables – the change: `change_id, goal, tier,
  repo_name, change_base_sha, oracle_sha` (the oracle's commit, once it passed; empty before);
  the ticket and its round: `ticket_id, ticket_goal, work_json` (its work – a judge's, the work
  it judges), `attempt, role, context_refs` (its inputs and context at their versions),
  `findings` (what it is to address), `outputs` (the outputs it must report), `write_scope` (its
  ticket's directory), `task_soul` (its work's soul); where it works: `workdir, branch, base_sha,
  head_sha` (its base), `judged_sha` (the commit a judge judges), `sandbox_tools`; and two its
  handler binds: `environment` (the planner's environment instructions with the library
  catalogue) and `scaffold` (what the scaffold asks of the planner and the critic). An unknown or
  unbound variable fails G-D2. Load-time validation rejects unknown variables, missing files,
  souls and prompts not referenced by any role, and skills without a valid `description`.
* A prompt names where its role writes (`tickets/{{ticket_id}}/…`, file names the
  [`bindings.yaml`](assets/schemas/bindings.yaml) schemas expect) and the outputs it reports; on
  a later attempt the documents exist, and are revised with `base_version`.
* The implementer's and the scaffolder's soul is chosen per ticket (its work's `soul`,
  `task_soul`) from `souls_allowed` in `roles.yaml`.
* Each role has a handler in `legion_orch/workflow/roles/`: the context it sees beyond its
  inputs, its prompt variables, its role-specific guards, and what its accepted report says for
  its ticket – its commit and outputs, a judge's verdict and findings. Handlers work against the
  narrow `RoleContext` interface, not engine internals, and never move a ticket or touch a
  sandbox (where its agent works follows from the ticket: §2, `engine/sandboxes/plan.py`).
  Adding a role means assets plus one handler module; the controller refuses to start if a role
  in `roles.yaml` has none.
* **Context is scoped.** A run is given its ticket's inputs at the versions the pointers name,
  its work, and what its handler adds: a judge, the outputs of the work it judges (and its
  predecessor's review); a build's implementer and judges, the delta specs of the build's own
  requirements only and the oracle's commit; the integration verifier, every build's evidence
  and the baseline record. A document already given at its approved version is not repeated
  at another, and relative `file:` refs are qualified to the change.

**Role bindings:** [`assets/roles.yaml`](assets/roles.yaml)

**The workflow:** [`assets/workflow.yaml`](assets/workflow.yaml) – every change's skeleton, what
each risk tier's tree must contain (§3), the waivable criteria and the hooks. `legion-orch check` validates it; every run also gets it as the
generated `legion-workflow` skill.

| Role | Prompt |
| --- | --- |
| `intake` | [`prompts/intake.md`](assets/prompts/intake.md) |
| `planner` | [`prompts/planner.md`](assets/prompts/planner.md) |
| `critic` | [`prompts/critic.md`](assets/prompts/critic.md) |
| `oracle` | [`prompts/oracle.md`](assets/prompts/oracle.md) |
| `scaffolder` | [`prompts/scaffolder.md`](assets/prompts/scaffolder.md) |
| `implementer` | [`prompts/implementer.md`](assets/prompts/implementer.md) |
| `reviewer` | [`prompts/reviewer.md`](assets/prompts/reviewer.md) |
| `security` | [`prompts/security.md`](assets/prompts/security.md) |
| `architecture` | [`prompts/architecture.md`](assets/prompts/architecture.md) |
| `integration` | [`prompts/integration.md`](assets/prompts/integration.md) |
| `describer` | [`prompts/describer.md`](assets/prompts/describer.md) |
| `pr-reviewer` | [`prompts/pr-reviewer.md`](assets/prompts/pr-reviewer.md) |
| `release` | [`prompts/release.md`](assets/prompts/release.md) |
| `retrospective` | [`prompts/retrospective.md`](assets/prompts/retrospective.md) |
| `resolver` | [`prompts/resolver.md`](assets/prompts/resolver.md) |

**Souls**

* [`intake-analyst`](assets/souls/intake-analyst.md)
* [`architect-planner`](assets/souls/architect-planner.md)
* [`plan-critic`](assets/souls/plan-critic.md)
* [`test-engineer`](assets/souls/test-engineer.md)
* [`senior-developer`](assets/souls/senior-developer.md)
* [`backend-engineer`](assets/souls/backend-engineer.md)
* [`frontend-engineer`](assets/souls/frontend-engineer.md)
* [`code-reviewer`](assets/souls/code-reviewer.md)
* [`security-reviewer`](assets/souls/security-reviewer.md)
* [`architecture-reviewer`](assets/souls/architecture-reviewer.md)
* [`integration-verifier`](assets/souls/integration-verifier.md)
* [`pr-writer`](assets/souls/pr-writer.md)
* [`pr-reviewer`](assets/souls/pr-reviewer.md)
* [`release-planner`](assets/souls/release-planner.md)
* [`retrospective-analyst`](assets/souls/retrospective-analyst.md)
* [`resolver`](assets/souls/resolver.md)

**Schemas** ([`bindings.yaml`](assets/schemas/bindings.yaml) maps path patterns to these; agents
can fetch them with MCP `docs_schema(path)` / `schema_get(name)`; an agent's outputs are bound
under `tickets/*/`, run and verification records under `control/`). JSON documents use JSON Schema
2020-12; `x-format: markdown` schemas require headings/patterns; `x-format: text` is free text.
Cross-document rules (coverage, cycles, parallel overlap, resolvable attestations) stay in the
guards, because a per-file schema cannot express them.

* [`change`](assets/schemas/change.json)
* [`delta-spec`](assets/schemas/delta-spec.json)
* [`environment`](assets/schemas/environment.json)
* [`environment-build`](assets/schemas/environment-build.json)
* [`evidence`](assets/schemas/evidence.json)
* [`json`](assets/schemas/json.json)
* [`markdown`](assets/schemas/markdown.json)
* [`oracle`](assets/schemas/oracle.json)
* [`questions`](assets/schemas/questions.json)
* [`release-plan`](assets/schemas/release-plan.json)
* [`report-critique`](assets/schemas/report-critique.json)
* [`report-integration`](assets/schemas/report-integration.json)
* [`report-none`](assets/schemas/report-none.json)
* [`report-review`](assets/schemas/report-review.json)
* [`requirements`](assets/schemas/requirements.json)
* [`risk`](assets/schemas/risk.json)
* [`run-assignment`](assets/schemas/run-assignment.json)
* [`run-outcome`](assets/schemas/run-outcome.json)
* [`text`](assets/schemas/text.json)
* [`verification`](assets/schemas/verification.json)
* [`work-implementer`](assets/schemas/work-implementer.json) – an implementer ticket's `work` (tickets)

**Skills**

* [`harness-contract`](assets/skills/harness-contract.md)
* [`docstore-protocol`](assets/skills/docstore-protocol.md)
* [`attestation-format`](assets/skills/attestation-format.md)
* [`git-protocol`](assets/skills/git-protocol.md)
* [`blocked-protocol`](assets/skills/blocked-protocol.md)
* [`risk-signals`](assets/skills/risk-signals.md)
* [`plan-format`](assets/skills/plan-format.md) – the planner's `tickets.json` (builds, groups,
  criteria, the scaffold) and `oracle.json`
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
* [`resolution-protocol`](assets/skills/resolution-protocol.md) – how the resolver diagnoses an
  escalation and writes a proposal (§5.16)
* [`a2a-protocol`](assets/skills/a2a-protocol.md) – talking to the change's other agents and its
  humans over A2A (the change's catalog, the orchestrator card, the inbox); every role
* [`a2a-cli`](assets/skills/a2a-cli.md) – the `a2a` CLI itself, copied from
  [a2aproject/a2a-cli](https://github.com/a2aproject/a2a-cli/blob/main/skills/a2a-cli/SKILL.md);
  every role
