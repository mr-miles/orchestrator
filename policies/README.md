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
| `sandbox-resources.template.yaml` | `--driver-config-json` for a sandbox whose environment profile requests disk (see below) |

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
| `${MCP_HOST}` | MCP listener host as seen from sandboxes (server.public_host) |
| `${MCP_PORT}` | MCP listener port (server.mcp_port) |
| `${MCP_PATH}` | MCP endpoint path, e.g. /mcp |
| `${MCP_VERSIONS}` | MCP protocol revisions the gateway accepts |
| `${MCP_MAX_BODY_BYTES}` | largest MCP request the gateway inspects (`server.mcp_max_body_bytes`); larger ones are dropped with a connection reset |
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
| `${AGENT_IMAGE}` | image for agent sandboxes: its environment image, else sandbox.image (null for the gateway default) |
| `${VERIFIER_IMAGE}` | image for verifier sandboxes: its environment image, else sandbox.verifier_image or sandbox.image |
| `${SANDBOX_PROVIDERS}` | providers attached to agent sandboxes: PROVIDER + sandbox.providers |
| `${VERIFIER_PROVIDERS}` | providers attached to verifier sandboxes: PROVIDER |
| `${SANDBOX_CPU}` | `--cpu`: vCPUs requested by the sandbox's environment profile (null: driver default) |
| `${SANDBOX_MEMORY}` | `--memory`: memory requested by the sandbox's environment profile, `<n>Mi` (null: driver default) |
| `${DRIVER_CONFIG}` | `--driver-config-json` for the sandbox's disk (the rendered `sandbox-resources` template), null for the driver default |
| `${DISK_MIB}` | disk in MiB requested by the sandbox's environment profile (`sandbox-resources` template only) |
| `${AGENT_BINARIES}` | policy binaries [{path}] of the agent CLI (sandbox.agent_binaries) |
| `${GIT_BINARIES}` | policy binaries [{path}] of git and its HTTP remote helpers |
| `${PROFILE_BINARIES}` | plain paths of agent and git binaries, for the provider profile |
| `${SANDBOX_NAME}` | readable sandbox name: lg-<first name> (agents), lgv-<first name> (verifiers) |
| `${POLICY_FILE}` | path of the rendered sandbox policy |
| `${PAYLOAD_UPLOAD}` | upload spec <local payload dir>:<sandbox.root> |
| `${SANDBOX_WRITABLE}` | paths the sandbox may write: sandbox.root, /tmp, /dev/null |
| `${RUN_AS_USER}` | user the sandbox processes run as (sandbox.run_as_user) |
| `${RUN_AS_GROUP}` | group the sandbox processes run as (sandbox.run_as_group) |
| `${CHANGE_ID}` | change id the sandbox works for |
| `${RUN_ID}` | run id that created the sandbox (verifiers: the verifier name) |
| `${ROLE}` | role the sandbox serves (implementer, reviewer, ..., verifier) |
| `${TASK_ID}` | node / task id the sandbox serves (sanitised automatically in labels) |

## Environments

With the sandbox-environment stage (`environment:` in the config, WORKFLOW.md §5.3a) a sandbox
of a role that the approved spec maps to a profile gets that profile's image (`${AGENT_IMAGE}` /
`${VERIFIER_IMAGE}`), its network policies and its resources (`${SANDBOX_CPU}`,
`${SANDBOX_MEMORY}`, `${DRIVER_CONFIG}`). The network policies come from
`environments/policies/`; they carry no credentials, so the controller adds them to the rendered
sandbox policy as `network_policies` entries named `env_<name>` after the template's own. The
policy templates here need no change, but must not use `env_*` keys themselves.

CPU and memory are `sandbox create` flags (`--cpu`, `--memory`). Disk has no flag:
`sandbox-resources.template.yaml` is rendered with `${DISK_MIB}`; null values are dropped and an
empty result passes no flag. Its top-level key is the compute
driver the gateway runs (the default is `firecracker`); match the field names to your driver.
