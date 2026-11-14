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
    path: str  # source repository (local path or URL) the mirror is cloned from
    base_branch: str = "main"


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    # Base URL agents use from inside the sandbox to reach the controller.
    # MCP is served at <public_url>/mcp/ and git at <public_url>/git/<repo>.git
    public_url: str = "http://host.openshell.internal:8765"
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


@dataclass
class SandboxConfig:
    runner: str = "openshell"  # openshell | local
    openshell_bin: str = "openshell"
    image: str | None = None  # --from for agent sandboxes
    verifier_image: str | None = None  # --from for deterministic verifier sandboxes
    providers: list[str] = field(default_factory=list)
    # Templates: __CONTROLLER_HOST__ / __CONTROLLER_PORT__ are substituted per sandbox.
    policy_template: str = "policies/agent-policy.template.yaml"
    verifier_policy_template: str = "policies/verifier-policy.template.yaml"
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
        protected_paths=list(raw.get("protected_paths") or []),
        base_dir=base,
    )
    if cfg.server.operator_token is None:
        cfg.server.operator_token = os.environ.get("LEGION_OPERATOR_TOKEN")
    if not Path(cfg.repo.path).is_absolute() and "://" not in cfg.repo.path:
        cfg.repo.path = str((base / cfg.repo.path).resolve())
    return cfg
