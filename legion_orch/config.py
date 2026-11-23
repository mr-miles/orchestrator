"""Controller configuration (YAML)."""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

REMOVED_KEYS = {
    "agent_command": "sandbox.agent_command was removed: the controller builds the agent command, "
                     "because a custom one silently dropped --resume (reworks and reflections "
                     "lost their session). Set sandbox.agent_bin to choose the agent CLI.",
}


class Section(BaseModel):
    """A config section: its fields are checked as read, an unknown key is an error, a removed
    one says what replaced it."""
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _removed(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if True in data:      # YAML 1.1 reads a bare `on:` key as true
                raise ValueError("`on:` is read as `true` in YAML; the key is `items:`")
            if removed := sorted(set(data) & set(REMOVED_KEYS)):
                raise ValueError(" ".join(REMOVED_KEYS[k] for k in removed))
        return data


class WorkspaceConfig(Section):
    """A repository changes are made in. Its name is used in attestations
    (``git:<name>/<path>@<sha>``) and names its hub (``<state_dir>/git/served/<name>.git``)."""
    name: str
    # Source repository (local path or URL) the hub is cloned from. Omitted or blank: the
    # controller starts a new, empty repository (one empty commit on base_branch).
    source: str | None = None
    base_branch: str = "main"
    # A finished change is published as <publish_prefix><change-id> on the source; legion/* is
    # the hub's own (integration and run branches) and is never published.
    publish_prefix: str = "agent/"


WORKSPACE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _valid_ref_prefix(prefix: str) -> bool:
    """``prefix`` + a change id is a valid branch name (``git check-ref-format``)."""
    r = subprocess.run(["git", "check-ref-format", "--branch", f"{prefix}c000000-0000"],
                       capture_output=True)
    return r.returncode == 0


class ServerConfig(Section):
    # Operator listener: web UI + JSON API (human gates). Keep it off the sandbox network.
    host: str = "127.0.0.1"
    port: int = 8765
    # Agent-facing listeners, one protocol per port so the gateway policy can be exact:
    # MCP (/mcp, vetted as protocol: mcp), git smart-HTTP (/git/), token endpoint (/oauth/token).
    agent_host: str = "0.0.0.0"
    mcp_port: int = 8766
    git_port: int = 8767
    token_port: int = 8768
    # Agent-to-agent (A2A v1.0, JSON-RPC): the change's orchestrator card and its steps' cards.
    a2a_port: int = 8769
    # Largest MCP request the OpenShell gateway will inspect (mcp.max_body_bytes in the policy and
    # provider profile). The gateway drops bigger requests without a response (the agent sees a
    # connection reset), so the controller itself refuses requests over half of this with a 413;
    # that must still fit the largest docs_write (2 MiB of content, JSON-escaped). OpenShell's
    # default is 64 KiB.
    mcp_max_body_bytes: int = 8 * 1024 * 1024
    # How long the agent-facing listeners keep an idle connection open. The gateway relays each
    # agent connection to one controller connection, so when the controller closes an idle one
    # the agent's next request on it fails (ECONNRESET) - typically after the model has spent a
    # while writing a large document. uvicorn's default is 5 s.
    agent_keep_alive_s: int = 600
    # How sandboxes reach the agent-facing listeners.
    public_host: str = "host.openshell.internal"
    public_scheme: str = "http"
    # How the OpenShell gateway / host supervisor reaches the token endpoint. The exchange is
    # made host-side, not from the sandbox, and OpenShell requires https for it unless the host
    # is loopback or an in-cluster service: keep loopback when the gateway runs on this host.
    token_host: str = "127.0.0.1"
    token_scheme: str = "http"

    def _public(self, port: int) -> str:
        return f"{self.public_scheme}://{self.public_host}:{port}"

    @property
    def mcp_url(self) -> str:
        return self._public(self.mcp_port) + "/mcp"

    @property
    def git_base_url(self) -> str:
        return self._public(self.git_port) + "/git"

    @property
    def a2a_url(self) -> str:
        return self._public(self.a2a_port) + "/a2a"

    # Optional token required for human actions in the web UI (header X-Operator-Token).
    operator_token: str | None = None

    @property
    def token_url(self) -> str:
        return f"{self.token_scheme}://{self.token_host}:{self.token_port}/oauth/token"


class LimitsConfig(Section):
    max_parallel: int = 4         # sandboxes in use at once (agent runs and verifiers), every change
    review_max_cycles: int = 3
    critique_max_cycles: int = 3   # a real BLOCKER in cycle 1 often needs two more to settle
    fix_rounds: int = 2           # fix rounds a change may plan after integration fails (§5.10a)
    max_report_attempts: int = 3
    run_timeout_s: int = 3600
    setup_timeout_s: int = 900    # mise run setup after a checkout (§5.3c)
    verify_timeout_s: int = 1800
    # verifier sandboxes running at once (within max_parallel): step verifications first, then
    # the checks at the head, then commits verified as they are pushed, then agents' requests
    max_parallel_verifications: int = 2
    verification_requests_per_run: int = 10     # MCP verification_request calls per agent run
    # an A2A answer from a step agent that is not running: its consult turn (§5.15)
    a2a_answer_timeout_s: int = 900
    # git transport: largest accepted request (push) and concurrent git-http-backend processes
    git_max_push_bytes: int = 256 * 1024 * 1024
    git_max_concurrency: int = 8
    # time limit for checking one report (attestations, git checks, role guards)
    report_check_timeout_s: float = 120
    # per-run document write quota (G-W4): versions and total bytes an agent run may write
    max_docs_per_run: int = 200
    max_doc_bytes_per_run: int = 50 * 1024 * 1024


class SandboxConfig(Section):
    runner: str = "openshell"  # openshell | local
    openshell_bin: str = "openshell"
    # OpenShell workspace every sandbox, provider and profile operation targets (passed as the
    # global --workspace flag on every call; OpenShell's own default is "default").
    workspace: str = "default"
    # Optional OpenShell gateway name (global -g flag); None = the CLI's active gateway.
    gateway: str | None = None
    image: str | None = None  # --from for agent sandboxes
    verifier_image: str | None = None  # --from for deterministic verifier sandboxes
    providers: list[str] = Field(default_factory=list)
    # Binaries in the agent image allowed to call the controller's MCP endpoint
    agent_binaries: list[str] = Field(default_factory=lambda: ["/usr/local/bin/claude"])
    # Binaries allowed to reach the controller's A2A endpoint (the a2a CLI, github.com/a2aproject/a2a-cli)
    a2a_binaries: list[str] = Field(default_factory=lambda: ["/usr/local/bin/a2a"])
    # Binaries allowed to reach the controller's git remote. git's HTTP traffic is sent by its
    # remote helper: /usr/lib/git-core on Debian/Ubuntu, /usr/libexec/git-core on Fedora/RHEL/Alpine.
    git_binaries: list[str] = Field(default_factory=lambda: [
        "/usr/bin/git", "/usr/lib/git-core/git-remote-http", "/usr/lib/git-core/git-remote-https"])
    # Folder with the templates for everything created in OpenShell (policies, sandboxes,
    # provider profile, provider); see policies/README.md for the placeholder vocabulary.
    templates_dir: str = "policies"
    keep_failed: bool = True
    # Where the payload and working copy live inside a sandbox (<root>/payload, <root>/work), and
    # the user the sandbox's processes run as: match the agent image (e.g. /home/agent, agent).
    root: str = "/sandbox"
    run_as_user: str = "sandbox"
    run_as_group: str = "sandbox"
    # The agent CLI inside the sandbox (a name on PATH or a path). The controller builds the rest
    # of the command (see agent_command): it has to read the payload, use the controller's MCP
    # config and resume the agent's session on rework and for reflection.
    agent_bin: str = "claude"
    extra_create_args: list[str] = Field(default_factory=list)


class ResolverConfig(Section):
    # Send every escalation to the change's resolver agent (WORKFLOW.md §5.16), which proposes
    # a decision for a human; off: escalations wait for a human with no proposal.
    enabled: bool = True


class RetrospectiveConfig(Section):
    # Before an agent's sandbox is deleted because its work is done, resume its session for one
    # reflection turn (what cost it time, guidance that was missing or wrong), stored in
    # control/reflections/. Best-effort: a failed or timed-out reflection never blocks the change.
    reflect: bool = True
    # Run the retrospective (an agent analysing the change, see WORKFLOW.md §5.13) as soon as
    # a change ends; a decision owner can always ask for another.
    auto: bool = True
    reflect_timeout_s: int = 600
    # Judges are skipped: what they missed shows in later outcomes (findings.json), not in
    # their own account, and the panel would otherwise reflect once per member per cycle.
    reflect_skip_roles: list[str] = Field(default_factory=lambda: [
        "retrospective", "reviewer", "security", "architecture", "critic"])


class EnvironmentConfig(Section):
    """The sandbox-environment stage (§5.3a of WORKFLOW.md): the planner specifies the tools,
    network policies and resources sandboxes need; after approval (H-E1) the controller builds them.
    Leave the ``environment:`` section out of the config to run without the stage."""
    # Library of reusable tools (tools/<name>.yaml) and network policies (policies/<name>.yaml)
    library_dir: str = "environments"
    podman_bin: str = "podman"
    # mise installed into every environment image (pinned for reproducible images)
    mise_version: str = "v2026.11.0"
    # Push built images here (e.g. registry.example.com/legion) for gateways that cannot see
    # this host's local podman images; None = local images (localhost/legion-env:<hash>).
    registry: str | None = None
    build_timeout_s: int = 1800
    # Largest resources a spec may request per sandbox (guard G-E3)
    max_memory_mib: int = 32768
    max_vcpus: int = 16
    max_disk_mib: int = 204800


class IdentityConfig(Section):
    """Workload identity (SPIFFE/SPIRE via the OpenShell gateway). See legion_orch/identity.py."""
    trust_prefix: str = "spiffe://openshell.local/openshell/sandbox/"
    jwks_uri: str | None = None          # SPIRE OIDC discovery JWKS, e.g. https://spire-oidc/keys
    issuer: str | None = None            # expected JWT-SVID iss (None = not checked)
    jwks_ca_file: str | None = None      # CA bundle for the JWKS endpoint
    svid_audience: str = "legion-controller"   # jwt_svid_audience in the provider profile
    token_audience: str = "legion-controller"  # audience in the provider profile
    scope: str = "legion"
    access_ttl_s: int = 300
    subject_field: str = "id"            # field of `openshell sandbox get -o json` in the SPIFFE ID
    dev_spire: bool | None = None        # in-process SPIRE stand-in; defaults to runner == local


# Attention item kinds (legion_orch.workflow.attention.KINDS): blocking gates, escalations and
# agents' questions (A2A ask_human), and the inbox's ended changes and retrospective reports.
ATTENTION_KINDS = ("gate", "escalation", "not_ready", "question", "change_ended", "retrospective")
AttentionKind = Literal["gate", "escalation", "not_ready", "question", "change_ended",
                        "retrospective"]


class NotifySink(Section):
    """Where notifications go. ``webhook``: POST the JSON item to ``url`` (signed with HMAC-SHA256
    in ``X-Legion-Signature`` when ``secret_env`` names an environment variable holding the
    secret). ``ntfy``: publish to the topic URL ``url`` (e.g. https://ntfy.sh/my-topic).
    ``command``: run ``argv`` with the JSON item on stdin (email, chat, anything else)."""
    kind: Literal["webhook", "ntfy", "command"]
    url: str | None = None
    secret_env: str | None = None
    argv: list[str] = Field(default_factory=list)
    items: list[AttentionKind] | None = None   # its item kinds (default: the section's ``items``)
    name: str | None = None             # shown in the timeline (default: the kind)

    @property
    def label(self) -> str:
        return self.name or self.kind

    @model_validator(mode="after")
    def _where(self) -> NotifySink:
        if self.kind in ("webhook", "ntfy") and not (self.url or "").startswith(("http://", "https://")):
            raise ValueError(f"notify sink {self.label}: url must be an http(s) URL")
        if self.kind == "command" and not self.argv:
            raise ValueError(f"notify sink {self.label}: argv is required")
        return self


class NotifyConfig(Section):
    """Outbound notifications for attention items (WORKFLOW.md §2.2). Sent by the controller
    only – sandboxes never reach a sink. Without sinks nothing is sent; the UI shows the items
    either way."""
    # Base URL of the operator UI, for links to the item (e.g. https://legion.example/).
    ui_url: str | None = None
    # Item kinds to notify: gate and escalation (blocking), change_ended, retrospective (inbox).
    items: list[AttentionKind] = ["gate", "escalation", "not_ready", "question"]
    # Repeat a blocking item while it is still waiting (seconds; 0 = never). Inbox items are
    # sent once.
    remind_after_s: int = 3600
    # Also notify when a blocking item is resolved (lets a chat thread or pager close it).
    resolved: bool = False
    timeout_s: int = 10                 # per delivery attempt
    sinks: list[NotifySink] = Field(default_factory=list)



class TelemetryConfig(Section):
    """Opt-in OpenTelemetry spans over OTLP/HTTP. Headers and credentials come only from the
    standard OTEL_EXPORTER_OTLP_* environment variables, never from this file."""
    # True forces telemetry on, False forces it off, None: on only when an OTLP endpoint
    # environment variable (OTEL_EXPORTER_OTLP_[TRACES_]ENDPOINT) is set.
    enabled: bool | None = None
    # OTLP/HTTP base endpoint (e.g. http://localhost:4318); None: the OTEL_* environment decides.
    endpoint: str | None = None
    # service.name resource attribute; None: the exporter's default.
    service_name: str | None = None
    # Export timeout in seconds.
    timeout_s: int = Field(5, ge=1)

    @field_validator("endpoint")
    @classmethod
    def _http(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("telemetry.endpoint must be an http(s) URL")
        return v


class Config(Section):
    project: str
    workspaces: list[WorkspaceConfig] = Field(min_length=1)
    decision_owners: list[str] = Field(min_length=1)       # at least one human approver
    # (relative paths are the config file's directory's, base_dir)
    state_dir: Path = Path(".legion-orch")
    assets_dir: Path = Path("assets")
    workflow_doc: Path = Path("WORKFLOW.md")  # displayed in the UI only; never parsed
    server: ServerConfig = Field(default_factory=ServerConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    identity: IdentityConfig = Field(default_factory=IdentityConfig)
    environment: EnvironmentConfig | None = None    # None: no sandbox-environment stage
    retrospective: RetrospectiveConfig = Field(default_factory=RetrospectiveConfig)
    resolver: ResolverConfig = Field(default_factory=ResolverConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    protected_paths: list[str] = Field(default_factory=list)
    base_dir: Path = Field(default_factory=Path.cwd)

    def workspace(self, name: str) -> WorkspaceConfig:
        for w in self.workspaces:
            if w.name == name:
                return w
        raise KeyError(f"no workspace {name!r} (configured: "
                       f"{', '.join(w.name for w in self.workspaces)})")

    def resolve(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.base_dir / p)

    @model_validator(mode="before")
    @classmethod
    def _sections(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if "repo" in data:
                raise ValueError("repo: was replaced by workspaces: – a list of {name, source, "
                                 "base_branch, publish_prefix}")
            if "environment" in data and data["environment"] is None:   # `environment:` alone
                data = data | {"environment": {}}
        return data

    @model_validator(mode="after")
    def _whole(self) -> Config:
        for name in ("state_dir", "assets_dir", "workflow_doc"):
            setattr(self, name, (self.base_dir / getattr(self, name)).resolve())
        s = self.server
        ports = [s.port, s.mcp_port, s.git_port, s.token_port, s.a2a_port]
        if len(set(ports)) != len(ports):
            raise ValueError(f"server ports must be distinct (operator, mcp, git, token, a2a): {ports}")
        if not self.sandbox.workspace.strip():
            raise ValueError("sandbox.workspace must name an OpenShell workspace")
        if self.identity.dev_spire is None:
            self.identity.dev_spire = self.sandbox.runner == "local"
        if not self.identity.dev_spire and not self.identity.jwks_uri:
            raise ValueError("identity.jwks_uri (SPIRE JWKS) is required unless identity.dev_spire is true")
        if s.operator_token is None:
            s.operator_token = os.environ.get("LEGION_OPERATOR_TOKEN")
        _check_workspaces(self.workspaces, self.base_dir)
        return self


def agent_command(sc: SandboxConfig, *, tools: bool = True) -> str:
    """The command run-agent.sh starts the agent with (via ``bash -lc`` from the working copy).
    $LEGION_PAYLOAD is the payload directory and $LEGION_MCP_CONFIG its mcp.json, under every
    runner; $LEGION_RESUME_SESSION, when set, is the agent session to continue. ``tools=False``
    (a reflection) gives the agent only the Skill tool: it answers from the session, its prompt
    and its skills instead of reading files (the session's own transcript included)."""
    return (f'{shlex.quote(sc.agent_bin)} -p '
            '${LEGION_RESUME_SESSION:+--resume "$LEGION_RESUME_SESSION"} '
            + ('' if tools else '--tools Skill ') +
            '--mcp-config "$LEGION_MCP_CONFIG" --strict-mcp-config '
            '--append-system-prompt "$(cat "$LEGION_PAYLOAD/soul.md")" '
            "--permission-mode bypassPermissions --output-format stream-json --verbose "
            '< "$LEGION_PAYLOAD/prompt.md"')


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    return Config.model_validate((yaml.safe_load(path.read_text()) or {}) | {"base_dir": path.parent})


def _check_workspaces(workspaces: list[WorkspaceConfig], base: Path) -> None:
    names = [w.name for w in workspaces]
    if dup := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"workspace names must be unique: {dup}")
    for w in workspaces:
        if not WORKSPACE_NAME.fullmatch(w.name) or w.name.endswith(".git"):
            raise ValueError(f"workspace name {w.name!r}: letters, digits, '.', '_' and '-' only "
                             "(a path segment), not ending in .git")
        if w.publish_prefix.startswith("legion/") or not _valid_ref_prefix(w.publish_prefix):
            raise ValueError(f"workspace {w.name}: publish_prefix {w.publish_prefix!r} must make "
                             "a valid branch name, outside legion/ (the hub's own branches)")
        w.source = (w.source or "").strip() or None
        if w.source and not Path(w.source).is_absolute() and "://" not in w.source:
            w.source = str((base / w.source).resolve())
