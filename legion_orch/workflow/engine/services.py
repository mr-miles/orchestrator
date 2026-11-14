"""What a ChangeEngine is given: shared resources (:class:`Services`) and the few controller
operations it may call back (:class:`EngineHost`). The engine never sees the Controller itself."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ...config import Config
from ...git import GitRemote
from ...sandbox import Runner, render_policy
from ...store import DocStore
from ..assets import Assets
from .auth import WorkloadAuth


@dataclass
class Services:
    """Resources shared by every change (owned by the Controller)."""
    cfg: Config
    docs: DocStore
    git: GitRemote
    assets: Assets
    runner: Runner
    auth: WorkloadAuth
    tmp: Path                                   # transient payloads and live logs only
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    procs: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)
    # The gateway's MCP tool allowlist, set when the MCP server is built (sandbox policies).
    mcp_tools: list[str] | None = None

    @property
    def git_url(self) -> str:
        return f"{self.cfg.server.git_base_url}/{self.cfg.repo.name}.git"

    @property
    def mcp_url(self) -> str:
        return self.cfg.server.mcp_url

    def write_policy(self, dest: Path, *, agent: bool, push: bool) -> Path:
        """Sandbox policy from policies/{agent,verifier}-policy.template.yaml: MCP vetted by the
        gateway (protocol: mcp, tool allowlist), git push only if the role may push."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.mcp_tools is None:
            raise RuntimeError("MCP server not built: no tool allowlist for the sandbox policy")
        dest.write_text(render_policy(self.cfg, agent=agent, push=push, mcp_tools=self.mcp_tools))
        return dest


class EngineHost(Protocol):
    """The controller operations a ChangeEngine may call."""

    def spawn(self, coro: Coroutine, on_error: Callable[[Exception], Any] | None = None) -> None:
        """Run a background task; failures are logged (and passed to ``on_error``)."""

    def changed(self) -> None:
        """State was saved: refresh the UI and wake the scheduler."""

    def touch(self) -> None:
        """Something visible changed (UI refresh only)."""

    def next_run_id(self) -> str: ...

    def live_count(self) -> int:
        """Live runs across all changes (the parallel limit is global)."""
