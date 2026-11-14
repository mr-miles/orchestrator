# OpenShell templates

Everything legion-orch creates in OpenShell is rendered from a template in this folder
(`sandbox.templates_dir`). Edit these files to adapt to your images, binaries and policies; the
controller only fills in placeholders.

| Template | Creates |
| --- | --- |
| `agent-policy.template.yaml` | network/filesystem policy of every agent sandbox (`sandbox create --policy`) |
| `verifier-policy.template.yaml` | policy of deterministic verifier sandboxes (git fetch only) |
| `agent-sandbox.template.yaml` | `sandbox create` flags for an agent sandbox |
| `verifier-sandbox.template.yaml` | `sandbox create` flags for a verifier sandbox |
| `legion-provider-profile.template.yaml` | provider profile imported once per workspace (`profile import -f`) |
| `legion-provider.template.yaml` | `provider create` flags, once per workspace |

## Rendering rules

* Placeholders are `${NAME}` from the vocabulary below – the same names in every template.
  A placeholder is always a **complete value**, never part of a string. Fixed values are written
  literally instead (e.g. git paths use the wildcard `/git/*.git/git-upload-pack`).
* Every value is inserted **JSON-encoded**, which is always valid YAML: strings are quoted,
  numbers bare, lists `[...]`, maps `{...}`, `null` when unset. Write `host: ${MCP_HOST}`, not
  `host: "${MCP_HOST}"`.
* An unknown placeholder, or one with no value for that template, fails the render.
* `*-sandbox` and `legion-provider` templates are **flag specs** for CLI-only objects: each key is
  a flag (`no_tty` → `--no-tty`); lists repeat the flag, maps become `KEY=VALUE`, `true` is a bare
  flag, `null`/`false` omit it. Every `openshell` call is prefixed with
  `--workspace ${WORKSPACE}` (and `-g <gateway>` when `sandbox.gateway` is set).
* OpenShell gives deny rules precedence over allows, so the git endpoint always allows
  `git-receive-pack` and `${GIT_PUSH_DENY_RULES}` removes it for roles that do not push.
* `label` values are sanitised to OpenShell's rules (≤63 chars of `[A-Za-z0-9._-]`, e.g.
  `build:T1` → `build.T1`); put exact values in `annotation` if they matter.
* Do not put credentials in templates: controller access is injected by the gateway (SPIFFE token
  grant in the provider profile), model access by the model providers.

## Vocabulary

| Placeholder | Value |
| --- | --- |
| `${GIT_HOST}` | git listener host as seen from sandboxes (server.public_host) |
| `${GIT_PORT}` | git listener port (server.git_port) |
| `${GIT_TLS}` | "none" for a plain HTTP git listener, "" for TLS |
| `${MCP_HOST}` | MCP listener host as seen from sandboxes (server.public_host) |
| `${MCP_PORT}` | MCP listener port (server.mcp_port) |
| `${MCP_PATH}` | MCP endpoint path, e.g. /mcp |
| `${MCP_TLS}` | "none" for a plain HTTP MCP endpoint, "" for TLS |
| `${MCP_VERSIONS}` | MCP protocol revisions the gateway accepts |
| `${MCP_TOOLS}` | tool names the controller's MCP server registers (the tools/call allowlist) |
| `${GIT_PUSH_DENY_RULES}` | deny rules for git-receive-pack: [] for roles that push, else a POST deny |
| `${TOKEN_ENDPOINT}` | the controller's OAuth token endpoint (server.token_port, /oauth/token) |
| `${TOKEN_AUDIENCE}` | audience requested for controller access tokens |
| `${SVID_AUDIENCE}` | audience of the sandbox JWT-SVID presented to the token endpoint |
| `${SCOPE}` | OAuth scope requested for controller access tokens |
| `${TOKEN_CACHE_TTL}` | seconds the gateway may cache a controller access token |
| `${PROVIDER}` | OpenShell provider name for controller access (identity.provider) |
| `${PROFILE_ID}` | OpenShell provider profile id / provider type (legion-controller) |
| `${WORKSPACE}` | OpenShell workspace (sandbox.workspace) |
| `${AGENT_IMAGE}` | image for agent sandboxes (sandbox.image), null for the gateway default |
| `${VERIFIER_IMAGE}` | image for verifier sandboxes (sandbox.verifier_image, else sandbox.image) |
| `${SANDBOX_PROVIDERS}` | providers attached to agent sandboxes: PROVIDER + sandbox.providers |
| `${AGENT_BINARIES}` | policy binaries [{path}] of the agent CLI (sandbox.agent_binaries) |
| `${GIT_BINARIES}` | policy binaries [{path}] of git and its HTTP remote helpers |
| `${PROFILE_BINARIES}` | plain paths of agent and git binaries, for the provider profile |
| `${SANDBOX_NAME}` | sandbox name (lg-<change>-<run>) |
| `${POLICY_FILE}` | path of the rendered sandbox policy |
| `${PAYLOAD_UPLOAD}` | upload spec <local payload dir>:/sandbox |
| `${CHANGE_ID}` | change id the sandbox works for |
| `${RUN_ID}` | run id that created the sandbox (verifiers: the verifier name) |
| `${ROLE}` | role the sandbox serves (implementer, reviewer, ..., verifier) |
| `${TASK_ID}` | node / task id the sandbox serves (sanitised automatically in labels) |
