# Legion Orchestrator

A Python controller that runs a [legion-ascended](https://github.com/9thLevelSoftware/legion-ascended)-style
guarded delivery workflow with LLM agents in [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandboxes.

* **[WORKFLOW.md](WORKFLOW.md)** is the specification: the tickets a change is made of, their
  guards, the risk tiers' policies, and which soul, skills and prompt each role uses.
* **[docs/architecture.md](docs/architecture.md)** shows how the controller is layered (the pure
  ticket rules, the runner and its executors, the services, sandboxes and infrastructure) and what
  each layer may know, as diagrams.
* **[docs/c4.md](docs/c4.md)** shows the application at four levels of zoom (C4): its system
  context, its containers, the controller's components and the ticket workflow's classes.
* **[assets/](assets/)** holds the agent-facing text and document schemas: `workflow.yaml` (every
  change's skeleton, the tier policies, the hooks), `roles.yaml`, `schemas/` (JSON Schemas bound to document paths; the store validates every write and records the schema),
  `souls/<soul>.md`, `skills/<skill>.md` and `prompts/<role>.md`. The controller loads and validates
  the folder at start-up, then pins the asset-set hash and the rendered-prompt hash on every run.
  WORKFLOW.md is documentation only and is never parsed.
* The controller enforces every rule. Agents can only act through MCP tools (the immutable versioned
  document store, progress, report) and through `git push` to their own branch on the controller's remote.
  Every claim is an attestation (`file:<path>@<version>`, `git:<repo>/<path>@<sha>`) that the controller
  resolves before anything moves forward.
* The web UI shows the tickets, agent progress, reviews, verification and documents, and it is where
  humans sign off, choose remedies for escalations and replan.
* A ticket's agent keeps its sandbox, stopped between rounds, with its session until the ticket is
  closed – the intake until H-1 passes, the planner until H-E1, the oracle until H-2 – so a rework
  continues the same conversation. Judges get a fresh sandbox every round.
* Before a finished agent's sandbox is deleted, the agent reflects on its session (what cost it
  time, where its instructions failed) into `control/reflections/`. When a change ends, a
  **retrospective** runs (and a decision owner can ask for more): an agent that reads the reflections, the whole agent
  logs (timestamped, with per-run digests), the timeline and the reviews, and proposes changes to
  the souls, skills, prompts and process. Configure with `retrospective:`
  (`auto`, `reflect`, `reflect_timeout_s`, `reflect_skip_roles` – judges are skipped by default; the
  retrospective judges reviews by their escapes instead); see WORKFLOW.md §5.13.
* The document store and the git remote are the only durable state. Each change is namespaced under
  `changes/<id>/` – each ticket's documents under `tickets/<id>/`, run and verification records
  under `control/` – and its tickets are rebuilt from the state tables on restart, every ticket
  under way going on at the same attempt. Several changes can run side by side, and `state_dir` is
  set once.

```
change
├─ workspace       sync (the change's base)
├─ requirements    intake ✓H-1 (requirements, risk → the tier)
├─ plan            design (planner) ✓critique ✓H-E1 → provision → scaffold → [acceptance: oracle ✓preflight ✓H-2 → baseline ✓H-2b]
├─ delivery        the planner's builds (each ✓verify ✓review… ✓can-merge) → integration
├─ evaluate        describe ✓H-3
└─ release         publish
```

Every change is one tree of **tickets** – a ticket is its own work, then its subtask groups (in
order; a group's tickets in parallel), then its acceptance criteria (in order) – and a human
sign-off is just a criterion. The skeleton comes from `assets/workflow.yaml`; the planner fills
`delivery`, and each risk tier's policy says which ticket types, criteria and order a tree must
have. A failed criterion reworks the ticket (an attempt); past its limit, or on a system error,
an exceeded budget or a BLOCKED agent, it waits on a human, whose remedies are retry, waive or
replan – finished work is never undone, corrections are new tickets (WORKFLOW.md §4). The rules
are pure (`legion_orch/workflow/tickets/rules.py`).

## Run

```bash
pip install -e '.[test]'
(cd frontend && bun install && bun run build)    # the web UI, into legion_orch/server/static/dist
cp orchestrator.example.yaml orchestrator.yaml   # set workspaces, decision_owners, sandbox image/providers
                                                 # (a workspace without a source starts from an empty repository)
gh auth login                                     # a workspace on GitHub: accepted changes become PRs
legion-orch -c orchestrator.yaml check            # validate the assets folder
legion-orch -c orchestrator.yaml serve            # UI at http://<host>:8765/
legion-orch -c orchestrator.yaml start "Add a /health endpoint" --workspace app   # or use the UI
legion-orch -c orchestrator.yaml changes                            # list changes
legion-orch -c orchestrator.yaml act <change-id> H-1 approve --approver miles
legion-orch -c orchestrator.yaml act <change-id> H-E1 reject --approver miles --payload '{"reason": "split T1"}'
# a waiting ticket's remedies: retry (with an answer for a blocked agent), waive, replan
legion-orch -c orchestrator.yaml act <change-id> T1 retry --approver miles --payload '{"answer": "use the v2 API"}'
legion-orch -c orchestrator.yaml act <change-id> T1 waive:review:T1 --approver miles --payload '{"reason": "…"}'
legion-orch -c orchestrator.yaml govern <change-id> pause --approver miles
legion-orch -c orchestrator.yaml govern <change-id> replan --approver miles --payload '{"plan": {"add": […]}}'
```

Five listeners, one purpose each:

| Listener | Default | Serves | Who reaches it |
| --- | --- | --- | --- |
| `server.host:port` | `127.0.0.1:8765` | `/` (UI), `/api/*` (JSON + SSE, human gates) | operators only; keep it off the sandbox network |
| `server.agent_host:mcp_port` | `0.0.0.0:8766` | `/mcp` | agents, vetted by the gateway as `protocol: mcp` |
| `server.agent_host:git_port` | `0.0.0.0:8767` | `/git/<repo>.git` (push only to the run's branch) | agents and verifiers |
| `server.agent_host:token_port` | `0.0.0.0:8768` | `/oauth/token` (SPIFFE JWT-SVID → ticket-scoped token) | the OpenShell gateway supervisor |
| `server.agent_host:a2a_port` | `0.0.0.0:8769` | `/a2a/*` (A2A cards of the caller's change, its AI Catalog) | agents (the `a2a` CLI) |

The UI ([`frontend/`](frontend/README.md): React, TypeScript, Vite, Tailwind CSS and shadcn/ui,
built with bun) has a left-hand menu in two groups. **Workloads**: *Work* (every change, counted by
status, in a table by workspace or all together), each workspace (its changes, a document store
browser and a repository browser at any branch, with copy and download), and the *Inbox*.
**Configuration**: *Skills*, *Workflows* and *Agents*, each a browser over the definition files the
controller reads from its assets. Breadcrumbs show where you are (`Workloads › <workspace> ›
<change>`). A controller serves one workspace: its repository and document store. The UI follows a
change over server-sent events: `/api/changes/<id>/events` pushes the
change's state, the change list and every change's attention items whenever they differ, and
`/api/runs/<run>/log/events` an agent's log as it grows. The header's bell lists what **needs
you** (sign-offs, escalations, paused changes, agents' questions, across all changes) and the **inbox** (ended changes, retrospective
reports); `notify:` sends the same items to ntfy, a webhook or a command, with reminders while
they wait (WORKFLOW.md §2.2). `legion-orch attention` and `legion-orch digest --since 8h` show
them on the command line. Build it
before packaging (`python -m build` ships whatever is in
`static/dist`); without a build, `/` says how to make one.

Sandboxes reach the agent-facing listeners at `server.public_host` (default
`host.openshell.internal`). The UI is served with a Content Security Policy that forbids inline
script, because it displays agent-authored text. The git listener streams with a byte cap
(`limits.git_max_push_bytes`), limits concurrent backends (`limits.git_max_concurrency`), and the hub
repository verifies every pushed object (`receive.fsckObjects`, `receive.maxInputSize`).

No credential is ever placed in a sandbox. Once per OpenShell workspace, run
`legion-orch -c orchestrator.yaml setup-openshell --apply`. Every OpenShell object (sandbox
policies, `sandbox create` flags, provider profiles, providers) is rendered from a template in
[`policies/`](policies/README.md) with one shared `${PLACEHOLDER}` vocabulary, and every
`openshell` call targets `sandbox.workspace`. This imports one provider profile per controller
endpoint and creates a provider of the same name for each: `legion-mcp` (MCP, for agents),
`legion-a2a` (A2A, for the agents' `a2a` CLI), `legion-git` (git fetch only) and
`legion-git-push` (fetch and push). Agent sandboxes attach `legion-mcp`, `legion-a2a` and one git
provider (`legion-git-push` for roles that push); verifiers attach only
`legion-git`. The attached profiles add the controller endpoints to the sandbox's effective
policy, so the sandbox policies carry no rule for them. The gateway exchanges each sandbox's
SPIFFE JWT-SVID at `/oauth/token` and injects the bearer into requests to the controller. Configure
`identity.jwks_uri` (and `issuer`) with your SPIRE OIDC discovery endpoint.

### OpenShell

Each agent run gets `openshell sandbox create --detach --name lg-<first name> --from <image>
--provider … --policy <rendered policy> --upload <payload>:/sandbox`. The payload holds only `soul.md`,
`prompt.md`, `mcp.json`, `run.env` and the preparation scripts. The controller then prepares the sandbox
one step at a time (`openshell sandbox exec`): `clone.sh` (a partial clone), then `branch.sh` for a
role that pushes or `checkout.sh` (detached at its commit) for any other, then `mise-setup.sh` when
a setup is pinned, and finally `run-agent.sh` starts the agent. The agent pushes only its ticket's
branch to the controller, and writes documents over MCP. Nothing is downloaded from a sandbox. Deterministic verification runs
in a separate, read-only sandbox through `openshell sandbox exec`. Adjust `policies/*.template.yaml`
to match your image's binary paths. The agent is the native `claude` CLI baked into the image;
`sandbox.agent_bin` names it if it is not `claude` on the PATH. The controller builds the rest of the
command, because it must read the payload, use the controller's MCP config and resume the agent's
session on rework and for reflection.

The image is the agent's whole toolset: sandboxes reach no package registry, so nothing can be
installed at run time. `images/agent/Dockerfile` adds what agents use for their own work –
`python3` (scripted edits) and `rsync` – to an existing Debian/Ubuntu-based agent image
(`--build-arg BASE=<image>`). The project's toolchain (bun, node, uv, …) comes from the
environment stage's profiles, which put it in both the agent and the verifier images; without that
stage, bake it into `sandbox.image` and `sandbox.verifier_image` alike, since verification commands
may only use what the verifier has.

### Sandbox environments

With an `environment:` section in the config, the planner's `environment` output names sandbox **profiles** (tools, network policies, and
memory/vCPU/disk if non-default) and which role runs in which profile. Profiles are composed from
a library in [`environments/`](environments/README.md): tools installed with **mise**, and network
policies (credentialless endpoint lists) that become rules in the sandbox's own policy. The spec is critiqued with the
plan and signed off at **H-E1**, with the plan. The `provision` ticket then builds it: `podman build` of one image per
profile (`FROM sandbox.image`, or `sandbox.verifier_image` for the verifier) tagged by content and
reused when identical, and the
resources as `sandbox create --cpu` / `--memory` (disk, which has no flag, rendered from
`policies/sandbox-resources.template.yaml` into `--driver-config-json`).
Every later sandbox of a mapped role gets its profile's image, network policies and resources. Proposed
library entries can be promoted with `legion-orch library promote <change> tool|policy <name> --approver <you>`
(a change-level action on the running controller).
Without the section the workflow is unchanged.

`sandbox.runner: local` runs everything as local processes. It provides **no isolation** and is for
development and tests only.

## Tracing (OpenTelemetry)

Opt-in: off unless configured. Install with `uv sync --extra telemetry`, then set `telemetry: {enabled: true, endpoint: http://localhost:4318}`
(otel-front's OTLP/HTTP receiver) or `OTEL_EXPORTER_OTLP_ENDPOINT`.
Details: [docs/telemetry.md](docs/telemetry.md).

## Release zip

`mise run package` (or `cd frontend && bun run build`, then `python scripts/package.py`) writes
`dist/legion-orchestrator-<version>.zip`: the controller with its web UI already built, and the
`assets`, `policies` and `environments` folders. It needs only [uv](https://docs.astral.sh/uv/)
to run – no bun, Node or frontend tools:

```bash
unzip legion-orchestrator-<version>.zip && cd legion-orchestrator-<version>
cp orchestrator.example.yaml orchestrator.yaml   # then edit it
uv run legion-orch -c orchestrator.yaml serve
```

CI (`.github/workflows/ci.yml`) builds, lints and tests the frontend
(bun) and the backend (uv), then builds this zip, checks it from a clean unpack with uv alone and
uploads it as the run's artifact. `mise run check` runs the same locally.

## Tests

`pytest` runs the unit tests, the git access-control test and a full end-to-end workflow. The e2e test
drives scripted agents through the real MCP and git endpoints, including critique rework, a failed
review, a failed verification and a scope-guard rejection, through to a published, READY change.
