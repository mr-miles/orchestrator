# Legion Orchestrator

A Python controller that runs a [legion-ascended](https://github.com/9thLevelSoftware/legion-ascended)-style
guarded delivery workflow with LLM agents in [NVIDIA OpenShell](https://github.com/NVIDIA/OpenShell) sandboxes.

* **[WORKFLOW.md](WORKFLOW.md)** is the specification: stages, guards, risk tiers, ship gates, and
  which soul, skills and prompt each task uses.
* **[assets/](assets/)** holds the agent-facing text, one file per asset: `roles.yaml`,
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

```
start (intake) -> H-1 -> plan <-> critique -> oracle -> [baseline] -> H-2 -> build DAG
   (verify -> review panel -> rework loop -> merge) -> integration -> [release] -> H-3 -> ship
```

## Run

```bash
pip install -e '.[test]'
cp orchestrator.example.yaml orchestrator.yaml   # set repo, decision_owners, sandbox image/providers
legion-orch -c orchestrator.yaml check            # validate the assets folder
legion-orch -c orchestrator.yaml serve            # UI at http://<host>:8765/
legion-orch -c orchestrator.yaml start "Add a /health endpoint"   # or use the UI
legion-orch -c orchestrator.yaml act H-1 accept --approver miles
```

Endpoints served on one port: `/` (UI), `/api/*` (UI JSON + SSE), `/mcp/` (agent MCP tools,
bearer = run token) and `/git/<repo>.git` (git smart-HTTP; push limited to the run's branch).

### OpenShell

Each agent run gets `openshell sandbox create --detach --name lg-<change>-<run> --from <image>
--provider … --policy <rendered policy> --upload <payload>:/sandbox`. The payload holds only `soul.md`,
`prompt.md`, `mcp.json`, `run.env` and `run-agent.sh`. The agent clones from and pushes to the controller,
and writes documents over MCP. Nothing is downloaded from a sandbox. Deterministic verification runs
in a separate, read-only sandbox through `openshell sandbox exec`. Adjust `policies/*.template.yaml`
to match your image's binary paths. The default agent command is the native `claude` CLI baked into
the image.

`sandbox.runner: local` runs everything as local processes. It provides **no isolation** and is for
development and tests only.

## Tests

`pytest` runs the unit tests, the git access-control test and a full end-to-end workflow. The e2e test
drives scripted agents through the real MCP and git endpoints, including critique rework, a failed
review, a failed verification and a scope-guard rejection, through to a READY ship verdict.
