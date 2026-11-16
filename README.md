# Legion Orchestrator

A Python controller that runs a [legion-ascended](https://github.com/9thLevelSoftware/legion-ascended)-style
guarded delivery workflow with LLM agents in [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandboxes.

* **[WORKFLOW.md](WORKFLOW.md)** is the specification: stages, guards, risk tiers, ship gates, and
  which soul, skills and prompt each task uses.
* **[assets/](assets/)** holds the agent-facing text and document schemas: `roles.yaml`, `schemas/` (JSON Schemas bound to document paths; the store validates every write and records the schema),
  `souls/<soul>.md`, `skills/<skill>.md` and `prompts/<role>.md`. The controller loads and validates
  the folder at start-up, then pins the asset-set hash and the rendered-prompt hash on every run.
  WORKFLOW.md is documentation only and is never parsed.
* The controller enforces every rule. Agents can only act through MCP tools (the immutable versioned
  document store, progress, report) and through `git push` to their own branch on the controller's remote.
  Every claim is an attestation (`file:<path>@<version>`, `git:<repo>/<path>@<sha>`) that the controller
  resolves before anything moves forward.
* The web UI shows the plan, the live task graph, agent progress, reviews, verification, documents and
  ship gates, and it is where humans approve, revise and resolve escalations.
* A sandbox is deleted as soon as the work it produced is accepted.
* The document store and the git remote are the only durable state. Each change is namespaced under
  `changes/<id>/`, including the controller's own graph, node and run records, and is rebuilt
  from the store on restart. Several changes can run side by side, and `state_dir` is set once.

```
start (intake) -> H-1 -> plan -> [environment] <-> critique -> [H-E1 -> provision] -> oracle -> [baseline] -> H-2 -> build DAG
   (verify -> review panel -> rework loop -> merge) -> integration -> [release] -> H-3 -> ship
```

## Run

```bash
pip install -e '.[test]'
cp orchestrator.example.yaml orchestrator.yaml   # set repo, decision_owners, sandbox image/providers
                                                 # (omit repo.path to start from an empty repository)
legion-orch -c orchestrator.yaml check            # validate the assets folder
legion-orch -c orchestrator.yaml serve            # UI at http://<host>:8765/
legion-orch -c orchestrator.yaml start "Add a /health endpoint"   # or use the UI
legion-orch -c orchestrator.yaml changes                            # list changes
legion-orch -c orchestrator.yaml act <change-id> H-1 accept --approver miles
legion-orch -c orchestrator.yaml act <change-id> H-2 approve --approver miles --payload '{"subjects": ["REQ-1"]}'
legion-orch -c orchestrator.yaml govern <change-id> attest --approver miles \
  --payload '{"kind": "security-evaluation", "verdict": "pass", "sources": ["file:tasks/T1/reviews/security-cycle-1.md@1"]}'
```

Four listeners, one purpose each:

| Listener | Default | Serves | Who reaches it |
| --- | --- | --- | --- |
| `server.host:port` | `127.0.0.1:8765` | `/` (UI), `/api/*` (JSON + SSE, human gates) | operators only; keep it off the sandbox network |
| `server.agent_host:mcp_port` | `0.0.0.0:8766` | `/mcp` | agents, vetted by the gateway as `protocol: mcp` |
| `server.agent_host:git_port` | `0.0.0.0:8767` | `/git/<repo>.git` (push only to the run's branch) | agents and verifiers |
| `server.agent_host:token_port` | `0.0.0.0:8768` | `/oauth/token` (SPIFFE JWT-SVID → task-scoped token) | the OpenShell gateway supervisor |

Sandboxes reach the agent-facing listeners at `server.public_host` (default
`host.openshell.internal`). The UI is served with a Content Security Policy that forbids inline
script, because it displays agent-authored text. The git listener streams with a byte cap
(`limits.git_max_push_bytes`), limits concurrent backends (`limits.git_max_concurrency`), and the hub
repository verifies every pushed object (`receive.fsckObjects`, `receive.maxInputSize`).

No credential is ever placed in a sandbox. Once per OpenShell workspace, run
`legion-orch -c orchestrator.yaml setup-openshell --apply`. Every OpenShell object (sandbox
policies, `sandbox create` flags, provider profile, provider) is rendered from a template in
[`policies/`](policies/README.md) with one shared `${PLACEHOLDER}` vocabulary, and every
`openshell` call targets `sandbox.workspace`. This imports the `legion-controller`
provider profile and creates the provider. The gateway then exchanges each sandbox's SPIFFE
JWT-SVID at `/oauth/token` and injects the bearer into requests to the controller. Configure
`identity.jwks_uri` (and `issuer`) with your SPIRE OIDC discovery endpoint.

### OpenShell

Each agent run gets `openshell sandbox create --detach --name lg-<first name> --from <image>
--provider … --policy <rendered policy> --upload <payload>:/sandbox`. The payload holds only `soul.md`,
`prompt.md`, `mcp.json`, `run.env` and `run-agent.sh`. The agent clones from and pushes to the controller,
and writes documents over MCP. Nothing is downloaded from a sandbox. Deterministic verification runs
in a separate, read-only sandbox through `openshell sandbox exec`. Adjust `policies/*.template.yaml`
to match your image's binary paths. The default agent command is the native `claude` CLI baked into
the image.

The image is the agent's whole toolset: sandboxes reach no package registry, so nothing can be
installed at run time. Claude Code expects `python3` and `node`; `images/agent/Dockerfile` adds them
to an existing Debian/Ubuntu-based agent image (`--build-arg BASE=<image>`). Bake in any packages
the project's agents and verification commands need as well (`sandbox.verifier_image` runs the
verification commands).

### Sandbox environments

With an `environment:` section in the config, planning gains a step: the `environment` agent
writes `plan/environment.json`, naming sandbox **profiles** (tools, network policies, and
memory/vCPU/disk if non-default) and which role runs in which profile. Profiles are composed from
a library in [`environments/`](environments/README.md): tools installed with **mise**, and network
policies that become credentialless OpenShell provider profiles. The spec is critiqued with the
plan and approved at **H-E1**. The controller then provisions it: `podman build` of one image per
profile (`FROM sandbox.image`, or `sandbox.verifier_image` for the verifier) tagged by content and
reused when identical, `openshell profile import` + `provider create` per network policy, and the
resources rendered from `policies/sandbox-resources.template.yaml` into `--driver-config-json`.
Every later sandbox of a mapped role gets its profile's image, providers and resources. Proposed
library entries can be promoted with `legion-orch library promote <change> tool|policy <name>`.
Without the section the workflow is unchanged.

`sandbox.runner: local` runs everything as local processes. It provides **no isolation** and is for
development and tests only.

## Tests

`pytest` runs the unit tests, the git access-control test and a full end-to-end workflow. The e2e test
drives scripted agents through the real MCP and git endpoints, including critique rework, a failed
review, a failed verification and a scope-guard rejection, through to a READY ship verdict.
