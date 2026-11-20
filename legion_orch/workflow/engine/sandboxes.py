"""Sandboxes: the one owner of a change's sandboxes (plan: stages-and-steps.md §2, the sandbox
layer). Creating, preparing, pausing and waking an agent's sandbox; verifier sandboxes; which
sandboxes a node holds for rework; and retiring a sandbox – a reflection turn, then deletion.
When to do which is the run policy's business (:mod:`.runs`, :mod:`.dispatch`)."""
from __future__ import annotations

import asyncio
import contextlib
import shlex
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from ...sandbox import (
    AGENT_PREFIX,
    Runner,
    SandboxError,
    SandboxSpec,
    is_name_clash,
    payload_scripts,
)
from .. import governance
from ..assets import GitMode
from ..environment import VERIFIER
from ..model import LIVE_RUN, CommandResult, Node, Run
from ..setup import SETUP_COMMAND
from .auth import VerifierGrant
from .core import ChangeCore
from .provision import sandbox_env
from .reflect import Reflector
from .steps import step_for


@dataclass
class VerifierSandbox:
    """A live verifier sandbox with the candidate checked out in <sandbox root>/work."""
    name: str
    runner: Runner
    timeout_s: int
    workdir: str
    setup_script: str = ""

    async def setup(self, pin: str, timeout_s: int) -> CommandResult:
        """The repository's setup at the checkout (G-MS1: only the pinned mise.toml)."""
        rc, out = await self.runner.exec(self.name, ["bash", self.setup_script, pin],
                                         workdir=self.workdir, timeout=timeout_s)
        return CommandResult(command=SETUP_COMMAND, exit_code=rc, output=out[-4000:], setup=True)

    async def run(self, cmd: str) -> CommandResult:
        rc, out = await self.runner.exec(self.name, ["bash", "-lc", cmd], workdir=self.workdir,
                                         timeout=self.timeout_s)
        return CommandResult(command=cmd, exit_code=rc, output=out[-4000:])


class Sandboxes:
    def __init__(self, core: ChangeCore):
        self.c = core
        self.reflector = Reflector(core, self)
        self.retiring: set[str] = set()      # sandboxes being reflected on and deleted
        self._locks: dict[str, asyncio.Lock] = {}

    # ------------------------------------------------------------------ the limit
    def has_room(self, *, for_agent: bool = True) -> bool:
        """Another sandbox may be in use: fewer than ``limits.max_parallel`` live agent runs and
        verifier sandboxes across every change (a prepared run counts at once). Verifiers
        waiting for room go first: they finish work in flight, so a new agent run waits for
        them too."""
        used = self.c.host.sandboxes_in_use()
        if for_agent:
            used += self.c.host.verifiers_waiting()
        return used < self.c.cfg.limits.max_parallel

    def reserve(self) -> None:
        """A verification is about to start (under the lock): its verifier holds a place ahead
        of new agent runs until it takes a sandbox (:meth:`slot`) or gives the place up."""
        self.c.host.verifier_waits(True)

    def unreserve(self) -> None:
        self.c.host.verifier_waits(False)

    @contextlib.asynccontextmanager
    async def slot(self, *, reserved: bool = False) -> AsyncIterator[None]:
        """A verifier sandbox's place within the limit: waits (outside the lock) for one.
        ``reserved``: its place was held since its verification started."""
        waiting = reserved
        try:
            if not self.has_room(for_agent=False):
                self.c.event("sandbox", "a verifier waits for room within limits.max_parallel")
                if not waiting:
                    self.c.host.verifier_waits(True)
                    waiting = True
                while not self.has_room(for_agent=False):
                    await self.c.host.room_changed()
        finally:
            if waiting:
                self.c.host.verifier_waits(False)
        self.c.host.verifier_started()
        try:
            yield
        finally:
            self.c.host.verifier_ended()

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

    async def upload(self, name: str, payload: Path) -> None:
        await self.c.runner.upload(name, payload)

    async def start_agent(self, name: str, log_file: Path):
        return await self.c.runner.start_agent(name, log_file)

    async def prepare_steps(self, run: Run, fresh: bool) -> list[tuple[str, list[str], bool]]:
        """The sandbox's preparation, one script per step: (what, argv, must succeed). A role
        that pushes works on its branch – from its tip on the hub if earlier runs pushed it,
        else from its base; every other role gets its commit checked out detached. Roles that
        work with a commit then run the repository's setup (§5.3c)."""
        role = self.c.assets.roles[run.role]
        if not role.repo:
            return []
        p = f"{self.c.cfg.sandbox.root}/payload"
        steps = [("clone the repository", ["bash", f"{p}/clone.sh"], True)] if fresh else []
        if run.branch:
            start = await self.c.git.ref_sha(f"refs/heads/{run.branch}") or run.base_sha
            steps.append((f"check out branch {run.branch} at {start[:12]}",
                          ["bash", f"{p}/branch.sh", run.branch, start], True))
        else:
            sha = run.judged_sha or run.base_sha
            steps.append((f"check out {sha[:12]} (detached)", ["bash", f"{p}/checkout.sh", sha],
                          True))
        pin = self.c.change.setup_pin
        if pin and role.git != GitMode.NONE and not self.c.setup_waits_for_scaffold():
            steps.append((SETUP_COMMAND, ["bash", f"{p}/mise-setup.sh", pin.blob], False))
        return steps

    async def prepare(self, run: Run, log_file: Path, *, fresh: bool) -> None:
        """Run the preparation steps in the sandbox, each logged to the run's agent log. A
        failed checkout fails the launch; a failed setup is logged and the agent still starts
        (it may work around it, and verification runs the setup again)."""
        work = f"{self.c.cfg.sandbox.root}/work"
        for what, argv, required in await self.prepare_steps(run, fresh):
            timeout = (self.c.cfg.limits.setup_timeout_s if argv[1].endswith("mise-setup.sh")
                       else 600)
            rc, out = await self.c.runner.exec(run.sandbox, argv, timeout=timeout,
                                               workdir=None if "clone.sh" in argv[1] else work)
            with open(log_file, "a") as log:
                log.write(f"[legion] {what}: exit {rc}\n{out}")
                if rc and not required:
                    log.write(f"[legion] {what} failed (exit {rc}): the working copy may lack "
                              "its dependencies\n")
            if rc and required:
                raise SandboxError(f"{what} failed (exit {rc}): {out[-800:]}")

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

    # ------------------------------------------------------------------ verifier sandboxes
    @contextlib.asynccontextmanager
    async def verifier(self, name: str, label: str, sha: str, *,
                       reserved: bool = False) -> AsyncIterator[VerifierSandbox]:
        """A non-agent sandbox with a read-only git grant, checked out at ``sha``, once there is
        room for it (:meth:`slot`). On exit the grant is revoked and the sandbox and its payload
        are deleted, however the block ends."""
        async with self.slot(reserved=reserved):
            async with self._verifier(name, label, sha) as box:
                yield box

    @contextlib.asynccontextmanager
    async def _verifier(self, name: str, label: str, sha: str) -> AsyncIterator[VerifierSandbox]:
        base = self.c.svc.tmp / "verify" / name
        payload = base / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "run.env").write_text(f"LEGION_GIT_URL={shlex.quote(self.c.git_url)}\n")
        for script, text in payload_scripts().items():
            (payload / script).write_text(text)
        ref = None
        try:
            env = sandbox_env(self.c.change, VERIFIER)
            policy = self.c.svc.write_policy(base / "policy.yaml", agent=False,
                                             network=env.network if env else None)
            if stale := governance.stale_environment(self.c):
                raise SandboxError(stale)
            ref = await self.c.runner.create(SandboxSpec(
                name=name, payload_dir=payload, policy_file=policy, agent=False,
                change=self.c.cid, task=label, role="verifier", run=name,
                env=env))
            self.c.svc.auth.register_verifier(
                ref, VerifierGrant(id=f"verifier:{name}", change=self.c.cid, task=label))
            root = self.c.cfg.sandbox.root
            # the same steps as an agent's detached checkout: clone, then the commit
            for step in (["clone.sh"], ["checkout.sh", sha]):
                rc, out = await self.c.runner.exec(
                    name, ["bash", f"{root}/payload/{step[0]}", *step[1:]], timeout=600,
                    workdir=None if step[0] == "clone.sh" else f"{root}/work")
                if rc != 0:
                    raise SandboxError(f"verifier {step[0]} failed ({rc}): {out[-1500:]}")
            yield VerifierSandbox(name, self.c.runner, self.c.cfg.limits.verify_timeout_s,
                                  f"{root}/work", f"{root}/payload/mise-setup.sh")
        finally:
            if ref:
                self.c.svc.auth.unregister_verifier(ref)
            shutil.rmtree(base, ignore_errors=True)
            if ref:
                try:
                    await self.c.runner.delete(name)
                except Exception as e:
                    self.c.event("sandbox", f"verifier delete failed: {e}")

    # ------------------------------------------------------------------ retiring
    def delete_later(self, name: str, stopped: bool = False) -> None:
        """Delete a sandbox in the background (its work is done), after its agent has reflected
        on the session (see :mod:`.reflect`; ``stopped``: the sandbox has to be started first)."""
        self.retiring.add(name)
        self.c.spawn(self._retire(name, stopped))

    async def _retire(self, name: str, stopped: bool) -> None:
        self.reflector.pending.add(name)
        try:
            try:
                await self.reflector.reflect(name, stopped)
            finally:
                self.reflector.pending.discard(name)
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
