"""Sandboxes: the one owner of a change's sandboxes (plan: stages-and-steps.md §2, the sandbox
layer). The sessions work executes in (:mod:`.work`) – an agent's sandbox, created or woken and
paused after; a verifier's, created and deleted – and the commands that prepare a working copy;
which sandboxes a node holds for rework; and retiring a sandbox – a reflection turn, then
deletion. When to do which is the run policy's business (:mod:`.runs`, :mod:`.agent_runs`)."""
from __future__ import annotations

import asyncio
import shlex
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from ...sandbox import (
    AGENT_PREFIX,
    VERIFIER_PREFIX,
    SandboxError,
    SandboxSpec,
    is_name_clash,
    payload_scripts,
)
from .. import governance
from ..assets import GitMode
from ..environment import VERIFIER
from ..model import LIVE_RUN, Node, Run, RunStage
from ..setup import SETUP_COMMAND
from .auth import VerifierGrant
from .core import ChangeCore
from .provision import sandbox_env
from .reflect import Reflector
from .steps import sandbox_plan, step_for
from .work import COMMAND, PREPARE, SETUP, Command, Script, Shell, Work


class AgentSession:
    """An agent run's sandbox: created for the run, or the node's kept one woken and given the
    new payload. On close the payload goes, and a sandbox kept for its session is paused."""

    def __init__(self, sandboxes: Sandboxes, run: Run, fresh: bool,
                 stage: Callable[[RunStage, str], Awaitable[None]]):
        self.sb, self.run, self.fresh, self.stage = sandboxes, run, fresh, stage
        root = sandboxes.c.cfg.sandbox.root
        self.workdir, self.payload = f"{root}/work", f"{root}/payload"
        self._dir = sandboxes.c.run_dir(run.id)

    @property
    def name(self) -> str:
        return self.run.sandbox

    async def open(self) -> None:
        c, run, payload = self.sb.c, self.run, self._dir / "payload"
        if not self.fresh:
            async with self.sb.lock(run.sandbox):
                node = c.nodes.get(run.node_id)
                if node and node.sandbox == run.sandbox and node.sandbox_stopped:
                    await self.stage(RunStage.PREPARING, f"starting sandbox {run.sandbox}")
                    await self.sb.wake(run.sandbox)
                    node.sandbox_stopped = False
                await self.stage(RunStage.LAUNCHING, f"reusing sandbox {run.sandbox}")
                await self.sb.upload(run.sandbox, payload)
            return
        await self.stage(RunStage.PREPARING, f"creating sandbox {run.sandbox}")
        env = sandbox_env(c.change, run.role)
        policy = c.svc.write_policy(self._dir / "policy.yaml", agent=True,
                                    network=env.network if env else None)
        run.sandbox_ref = await self.sb.create(run, payload, policy,
                                               push=sandbox_plan(run.role).pushes)
        await self.stage(RunStage.LAUNCHING, f"sandbox {run.sandbox} created")

    async def exec(self, argv: list[str], *, timeout: int, workdir: str | None) -> tuple[int, str]:
        return await self.sb.c.runner.exec(self.run.sandbox, argv, timeout=timeout,
                                           workdir=workdir)

    async def close(self) -> None:
        shutil.rmtree(self._dir / "payload", ignore_errors=True)
        (self._dir / "policy.yaml").unlink(missing_ok=True)
        if sandbox_plan(self.run.role).keeps_session:
            await self.sb.pause(self.run)


class VerifierSession:
    """A non-agent sandbox with a read-only git grant, from a fresh payload. A name the gateway
    already has (another controller) is replaced and the sandbox created again. On close the
    grant is revoked and the sandbox and its payload are deleted, however the work ended."""

    def __init__(self, sandboxes: Sandboxes, name: str, label: str):
        self.sb, self.name, self.label = sandboxes, name, label
        root = sandboxes.c.cfg.sandbox.root
        self.workdir, self.payload = f"{root}/work", f"{root}/payload"
        self._ref: str | None = None

    @property
    def _base(self) -> Path:
        return self.sb.c.svc.tmp / "verify" / self.name

    async def open(self) -> None:
        c = self.sb.c
        for attempt in range(3):
            payload = self._base / "payload"
            payload.mkdir(parents=True, exist_ok=True)
            (payload / "run.env").write_text(f"LEGION_GIT_URL={shlex.quote(c.git_url)}\n")
            for script, text in payload_scripts().items():
                (payload / script).write_text(text)
            env = sandbox_env(c.change, VERIFIER)
            policy = c.svc.write_policy(self._base / "policy.yaml", agent=False,
                                        network=env.network if env else None)
            if stale := governance.stale_environment(c):
                raise SandboxError(stale)
            try:
                self._ref = await c.runner.create(SandboxSpec(
                    name=self.name, payload_dir=payload, policy_file=policy, agent=False,
                    change=c.cid, task=self.label, role="verifier", run=self.name, env=env))
            except Exception as e:
                if not is_name_clash(e) or attempt == 2:
                    raise
                shutil.rmtree(self._base, ignore_errors=True)
                self.name = c.host.new_sandbox_name(VERIFIER_PREFIX)
                continue
            c.svc.auth.register_verifier(self._ref, VerifierGrant(
                id=f"verifier:{self.name}", change=c.cid, task=self.label))
            self.sb.verifiers += 1
            return

    async def exec(self, argv: list[str], *, timeout: int, workdir: str | None) -> tuple[int, str]:
        return await self.sb.c.runner.exec(self.name, argv, timeout=timeout, workdir=workdir)

    async def close(self) -> None:
        c = self.sb.c
        if self._ref:
            c.svc.auth.unregister_verifier(self._ref)
            self.sb.verifiers -= 1
        shutil.rmtree(self._base, ignore_errors=True)
        if self._ref:
            try:
                await c.runner.delete(self.name)
            except Exception as e:
                c.event("sandbox", f"verifier delete failed: {e}")


class Sandboxes:
    def __init__(self, core: ChangeCore):
        self.c = core
        self.reflector = Reflector(core, self)
        self.retiring: set[str] = set()      # sandboxes being reflected on and deleted
        self.to_suspend: set[str] = set()    # to stop once nothing live uses them
        self.verifiers = 0                   # verifier sandboxes in use
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, name: str) -> asyncio.Lock:
        """Serialises what happens to one sandbox (a rework's launch against a pause)."""
        return self._locks.setdefault(name, asyncio.Lock())

    # ------------------------------------------------------------------ which are held
    def holds(self, node: Node) -> bool:
        """The node's producer still keeps ``node.sandbox`` for rework, as its step's sandbox
        plan says (until a gate is accepted, or until the node is). Nothing is held once the
        change is no longer active."""
        if not node.sandbox or self.c.change.status != "ACTIVE":
            return False
        plan = step_for(node).sandbox
        return plan is not None and plan.held(self.c, node)

    def held(self) -> set[str]:
        return {n.sandbox for n in self.c.nodes.values() if self.holds(n)}

    # ------------------------------------------------------------------ agent sandboxes
    async def create(self, run: Run, payload: Path, policy: Path, *, push: bool,
                     attempts: int = 3) -> str:
        """Create the run's sandbox. If the gateway already has a sandbox of that name (another
        controller on the same gateway), take a new name and try again."""
        for attempt in range(attempts):
            try:
                return await self.c.runner.create(SandboxSpec(
                    name=run.sandbox, payload_dir=payload, policy_file=policy, agent=True, push=push,
                    change=self.c.cid, task=run.node_id, role=run.role, run=run.id,
                    env=sandbox_env(self.c.change, run.role)))
            except Exception as e:
                if not is_name_clash(e) or attempt == attempts - 1:
                    raise
                old, run.sandbox = run.sandbox, self.c.host.new_sandbox_name(AGENT_PREFIX)
                node = self.c.nodes.get(run.node_id)
                if node and node.sandbox == old:
                    node.sandbox = run.sandbox
                self.c.event("sandbox", f"name {old} is taken on the gateway; using {run.sandbox}",
                             run.node_id, run.id)
        raise AssertionError("unreachable")

    async def wake(self, name: str) -> None:
        """Start a stopped (kept) sandbox again."""
        await self.c.runner.start(name)
        for r in self.c.runs.values():
            if r.sandbox == name:
                r.sandbox_stopped = False

    def stopped(self, name: str) -> bool:
        return any(r.sandbox == name and r.sandbox_stopped for r in self.c.runs.values()) \
            or any(n.sandbox == name and n.sandbox_stopped for n in self.c.nodes.values())

    # ------------------------------------------------------------------ suspending
    def suspend_when_idle(self, names: set[str]) -> None:
        """A step's reservation was released: its sandboxes are stopped once nothing live uses
        them (:meth:`suspend_due`, on each save). A kept one is woken by its next session; any
        other stays stopped until it is retired (started again for its reflection)."""
        self.to_suspend |= names

    def suspend_due(self) -> None:
        """Under the lock, on a save: stop the sandboxes waiting to be suspended that nothing
        live uses any more (gone ones are dropped)."""
        for name in sorted(self.to_suspend):
            runs = [r for r in self.c.runs.values() if r.sandbox == name]
            if not runs or name in self.retiring or all(r.sandbox_deleted for r in runs) \
                    or self.stopped(name):
                self.to_suspend.discard(name)
            elif not any(r.status in LIVE_RUN for r in runs):
                self.to_suspend.discard(name)
                self.c.spawn(self.suspend(name))

    async def suspend(self, name: str) -> None:
        """Stop a sandbox no live run uses (best effort: one that cannot stop stays running)."""
        async with self.lock(name):
            async with self.c.lock:
                runs = [r for r in self.c.runs.values() if r.sandbox == name]
                if not runs or name in self.retiring or self.stopped(name) \
                        or any(r.sandbox_deleted or r.status in LIVE_RUN for r in runs):
                    return
            try:
                await self.c.runner.stop(name)
            except Exception as e:
                async with self.c.lock:
                    self.c.event("sandbox", f"could not stop {name}; it stays running: {e}",
                                 runs[-1].node_id, runs[-1].id)
                    self.c.save()
                return
            async with self.c.lock:
                for r in runs:
                    r.sandbox_stopped = True
                for n in self.c.nodes.values():
                    if n.sandbox == name:
                        n.sandbox_stopped = True
                self.c.event("sandbox", f"stopped {name}: its step has no work until it is "
                             "resumed", runs[-1].node_id, runs[-1].id)
                self.c.save()

    async def upload(self, name: str, payload: Path) -> None:
        await self.c.runner.upload(name, payload)

    async def start_agent(self, name: str, log_file: Path):
        return await self.c.runner.start_agent(name, log_file)

    async def checkout(self, run: Run, fresh: bool) -> list[Command]:
        """The commands preparing an agent's working copy. A role that pushes works on its
        branch – from its tip on the hub if earlier runs pushed it, else from its base; every
        other role gets its commit checked out detached. Roles that work with a commit then run
        the repository's setup (§5.3c), which may fail: the agent still starts (it may work
        around it, and verification runs the setup again)."""
        role = self.c.assets.roles[run.role]
        if not role.repo:
            return []
        cmds: list[Command] = [Script("clone the repository", PREPARE, True, "clone.sh",
                                      in_work=False)] if fresh else []
        if run.branch:
            start = await self.c.git.ref_sha(f"refs/heads/{run.branch}") or run.base_sha
            cmds.append(Script(f"check out branch {run.branch} at {start[:12]}", PREPARE, True,
                               "branch.sh", (run.branch, start)))
        else:
            sha = run.judged_sha or run.base_sha
            cmds.append(Script(f"check out {sha[:12]} (detached)", PREPARE, True, "checkout.sh",
                               (sha,)))
        pin = self.c.change.setup_pin
        if pin and role.git != GitMode.NONE and not self.c.setup_waits_for_scaffold():
            cmds.append(self.setup(pin.blob, required=False))
        return cmds

    def setup(self, pin: str, *, required: bool) -> Script:
        """The repository's setup at the checkout (G-MS1: only the pinned mise.toml)."""
        return Script(SETUP_COMMAND, SETUP, required, "mise-setup.sh", (pin,),
                      timeout=self.c.cfg.limits.setup_timeout_s)

    def verifier_work(self, name: str, label: str, sha: str, commands: tuple[str, ...]) -> Work:
        """A verification: a fresh verifier sandbox checked out at ``sha`` (the same steps as an
        agent's detached checkout), the repository's setup (§5.3c, G-V3: a failure stops here),
        then each command – all of them, whatever their exit codes."""
        timeout = self.c.cfg.limits.verify_timeout_s
        pin = self.c.change.setup_pin
        cmds: list[Command] = [Script("clone.sh", PREPARE, True, "clone.sh", in_work=False),
                               Script("checkout.sh", PREPARE, True, "checkout.sh", (sha,))]
        if pin:
            cmds.append(self.setup(pin.blob, required=True))
        cmds += [Shell(cmd, COMMAND, False, timeout=timeout) for cmd in commands]
        return Work(VerifierSession(self, name, label), cmds)

    def _should_pause(self, run: Run) -> bool:
        node = self.c.nodes.get(run.node_id)
        return (node is not None and node.sandbox == run.sandbox and not node.sandbox_stopped
                and self.holds(node)
                and not any(r.sandbox == run.sandbox and r.status in LIVE_RUN
                            for r in self.c.runs.values()))

    async def pause(self, run: Run) -> None:
        """Stop a kept sandbox until its node is reworked (best effort: if the runner cannot
        stop it, it stays running and is reused as it is). Under the sandbox lock, so a rework
        prepared meanwhile either finds it running (and this skips) or stopped (and starts it)."""
        async with self.lock(run.sandbox):
            async with self.c.lock:
                if not self._should_pause(run):
                    return
            try:
                await self.c.runner.stop(run.sandbox)
            except Exception as e:
                async with self.c.lock:
                    self.c.event("sandbox", f"could not stop {run.sandbox}; it stays running: {e}",
                                 run.node_id, run.id)
                    self.c.save()
                return
            async with self.c.lock:
                node = self.c.nodes.get(run.node_id)
                if node and node.sandbox == run.sandbox:
                    node.sandbox_stopped = True
                    self.c.event("sandbox", f"stopped {run.sandbox} until {node.id} is reworked",
                                 run.node_id, run.id)
                self.c.save()

    # ------------------------------------------------------------------ retiring
    def delete_later(self, name: str, stopped: bool = False) -> None:
        """Delete a sandbox in the background (its work is done), after its agent has reflected
        on the session (see :mod:`.reflect`; ``stopped``: the sandbox has to be started first)."""
        self.retiring.add(name)
        self.to_suspend.discard(name)
        self.c.spawn(self._retire(name, stopped or self.stopped(name)))

    async def _retire(self, name: str, stopped: bool) -> None:
        self.reflector.pending.add(name)
        try:
            try:
                await self.reflector.reflect(name, stopped)
            finally:
                self.reflector.pending.discard(name)
                self.c.notify()             # a retrospective may wait for the reflections
            await self.delete(name)
        finally:
            self.retiring.discard(name)

    async def delete(self, name: str) -> bool:
        """Delete a sandbox; False (and an event) if OpenShell refused."""
        try:
            await self.c.runner.delete(name)
        except Exception as e:
            self.c.event("sandbox", f"delete of {name} failed: {e}")
            return False
        async with self.c.lock:
            for r in self.c.runs.values():
                if r.sandbox == name:
                    r.sandbox_deleted = True
            self.c.event("sandbox", f"sandbox {name} deleted")
            self.c.save()
        return True
