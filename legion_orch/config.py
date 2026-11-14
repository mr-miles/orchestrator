"""Controller configuration (YAML)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class RepoConfig:
    name: str
    # Source repository (local path or URL) the hub is cloned from. Omitted or blank: the
    # controller starts a new, empty repository (one empty commit on base_branch).
    path: str | None = None
    base_branch: str = "main"


@dataclass
class ServerConfig:
    # Operator listener: web UI + JSON API (human gates). Keep it off the sandbox network.
    host: str = "127.0.0.1"
    port: int = 8765
    # Agent-facing listeners, one protocol per port so the gateway policy can be exact:
    # MCP (/mcp, vetted as protocol: mcp), git smart-HTTP (/git/), token endpoint (/oauth/token).
    agent_host: str = "0.0.0.0"
    mcp_port: int = 8766
    git_port: int = 8767
    token_port: int = 8768
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
    def token_url(self) -> str:
        return f"{self.token_scheme}://{self.token_host}:{self.token_port}/oauth/token"
    # Optional token required for human actions in the web UI (header X-Operator-Token).
    operator_token: str | None = None


@dataclass
class LimitsConfig:
    max_parallel: int = 4
    review_max_cycles: int = 3
    critique_max_cycles: int = 2
    max_report_attempts: int = 3
    run_timeout_s: int = 3600
    verify_timeout_s: int = 1800
    # git transport: largest accepted request (push) and concurrent git-http-backend processes
    git_max_push_bytes: int = 256 * 1024 * 1024
    git_max_concurrency: int = 8
    # time limit for checking one report (attestations, git checks, role guards)
    report_check_timeout_s: int = 120
    # per-run document write quota (G-W4): versions and total bytes an agent run may write
    max_docs_per_run: int = 200
    max_doc_bytes_per_run: int = 50 * 1024 * 1024


@dataclass
class SandboxConfig:
    runner: str = "openshell"  # openshell | local
    openshell_bin: str = "openshell"
    # OpenShell workspace every sandbox, provider and profile operation targets (passed as the
    # global --workspace flag on every call; OpenShell's own default is "default").
    workspace: str = "default"
    # Optional OpenShell gateway name (global -g flag); None = the CLI's active gateway.
    gateway: str | None = None
    image: str | None = None  # --from for agent sandboxes
    verifier_image: str | None = None  # --from for deterministic verifier sandboxes
    providers: list[str] = field(default_factory=list)
    # Binaries in the agent image allowed to call the controller's MCP endpoint
    agent_binaries: list[str] = field(default_factory=lambda: ["/usr/local/bin/claude"])
    # Binaries allowed to reach the controller's git remote. git's HTTP traffic is sent by its
    # remote helper: /usr/lib/git-core on Debian/Ubuntu, /usr/libexec/git-core on Fedora/RHEL/Alpine.
    git_binaries: list[str] = field(default_factory=lambda: [
        "/usr/bin/git", "/usr/lib/git-core/git-remote-http", "/usr/lib/git-core/git-remote-https"])
    # Folder with the templates for everything created in OpenShell (policies, sandboxes,
    # provider profile, provider); see policies/README.md for the placeholder vocabulary.
    templates_dir: str = "policies"
    keep_failed: bool = True
    # Command run inside the sandbox to start the agent. Executed via `bash -lc` from
    # /sandbox/work, with the payload in /sandbox/payload.
    agent_command: str = (
        "claude -p --mcp-config /sandbox/payload/mcp.json --strict-mcp-config "
        "--append-system-prompt \"$(cat /sandbox/payload/soul.md)\" "
        "--permission-mode bypassPermissions --output-format stream-json --verbose "
        "< /sandbox/payload/prompt.md"
    )
    extra_create_args: list[str] = field(default_factory=list)


@dataclass
class IdentityConfig:
    """Workload identity (SPIFFE/SPIRE via the OpenShell gateway). See legion_orch/identity.py."""
    # OpenShell provider created once per workspace from policies/legion-provider-profile.template.yaml
    provider: str = "legion-controller"
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


@dataclass
class Config:
    project: str
    repo: RepoConfig
    decision_owners: list[str]
    state_dir: Path
    assets_dir: Path
    workflow_doc: Path  # displayed in the UI only; never parsed
    server: ServerConfig = field(default_factory=ServerConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    protected_paths: list[str] = field(default_factory=list)
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, p: str | Path) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (self.base_dir / p)


def _build(cls, data: dict[str, Any] | None):
    data = dict(data or {})
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return cls(**data)


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = yaml.safe_load(path.read_text()) or {}
    base = path.parent
    owners = raw.get("decision_owners") or []
    if not owners:
        raise ValueError("decision_owners must name at least one human approver")
    cfg = Config(
        project=raw["project"],
        repo=_build(RepoConfig, raw["repo"]),
        decision_owners=list(owners),
        state_dir=(base / raw.get("state_dir", ".legion-orch")).resolve(),
        assets_dir=(base / raw.get("assets_dir", "assets")).resolve(),
        workflow_doc=(base / raw.get("workflow_doc", "WORKFLOW.md")).resolve(),
        server=_build(ServerConfig, raw.get("server")),
        limits=_build(LimitsConfig, raw.get("limits")),
        sandbox=_build(SandboxConfig, raw.get("sandbox")),
        identity=_build(IdentityConfig, raw.get("identity")),
        protected_paths=list(raw.get("protected_paths") or []),
        base_dir=base,
    )
    ports = [cfg.server.port, cfg.server.mcp_port, cfg.server.git_port, cfg.server.token_port]
    if len(set(ports)) != len(ports):
        raise ValueError(f"server ports must be distinct (operator, mcp, git, token): {ports}")
    if not (cfg.sandbox.workspace or "").strip():
        raise ValueError("sandbox.workspace must name an OpenShell workspace")
    if cfg.identity.dev_spire is None:
        cfg.identity.dev_spire = cfg.sandbox.runner == "local"
    if not cfg.identity.dev_spire and not cfg.identity.jwks_uri:
        raise ValueError("identity.jwks_uri (SPIRE JWKS) is required unless identity.dev_spire is true")
    if cfg.server.operator_token is None:
        cfg.server.operator_token = os.environ.get("LEGION_OPERATOR_TOKEN")
    cfg.repo.path = (cfg.repo.path or "").strip() or None
    if cfg.repo.path and not Path(cfg.repo.path).is_absolute() and "://" not in cfg.repo.path:
        cfg.repo.path = str((base / cfg.repo.path).resolve())
    return cfg
