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
from ...sandbox import EnvironmentBuilder, NoBuilder, Runner, render_policy
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
    # Builds environment images (podman; no-op locally).
    builder: EnvironmentBuilder = field(default_factory=NoBuilder)
    procs: dict[str, asyncio.subprocess.Process] = field(default_factory=dict)

    @property
    def git_url(self) -> str:
        return f"{self.cfg.server.git_base_url}/{self.cfg.repo.name}.git"

    @property
    def mcp_url(self) -> str:
        return self.cfg.server.mcp_url

    def write_policy(self, dest: Path, *, agent: bool,
                     network: dict[str, dict] | None = None) -> Path:
        """Sandbox policy from policies/{agent,verifier}-policy.template.yaml plus the network
        policies of the sandbox's environment. Controller access is not in it: it comes from the
        attached provider profiles (see legion_orch.sandbox.profile)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(render_policy(self.cfg, agent=agent, network=network))
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

    def new_sandbox_name(self, prefix: str) -> str:
        """A readable sandbox name this controller has never used (agents and verifiers)."""

    def live_count(self) -> int:
        """Live runs across all changes (the parallel limit is global)."""
