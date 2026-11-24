"""Verification as a service: a commit's commands run in a fresh verifier sandbox once per key.

A verification's **key** is everything that decides its outcome – the commit, the commands, the
repository's setup pin (§5.3c), the verifier image and the timeout. A passed or failed run is the
key's result for good: whoever asks again – the step's Verify phase, an agent over MCP, a later
check at an unchanged head – gets it without a new sandbox. An infrastructure error is not a
result: the next request runs again (a new attempt). Requests for a key that is queued or
running join that run.

Who asks: the **Verify phase** (and the checks at the head: preflight, baseline, integration),
the **git remote** as a run pushes its branch (every pushed tip is verified, so a report usually
finds its candidate verified), and **agents** (``verification_request``). At most
``limits.max_parallel_verifications`` verifier sandboxes run at once, served by priority – steps
first, then the head's checks, then pushes, then agents – and in request order within one; a push
of a newer tip cancels its branch's verification that has not started yet.

The service returns results; what they mean is each caller's (a phase reworks, escalates,
expects failures...). Runs are recorded on the change (``Change.verifications``, compact) and in
full, command output included, at ``control/verifications/<id>.json``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ....sandbox import VERIFIER_PREFIX
from ....util import now_iso
from ...environment import VERIFIER
from ...model import V_FINAL, Verification, VerificationRun, VerificationState
from ..base.core import ChangeCore
from ..base.dispatch import Dispatcher, PriorityCap
from ..base.work import COMMAND, PREPARE, SETUP, Outcome
from ..sandboxes.sandboxes import Sandboxes, sandbox_env

if TYPE_CHECKING:
    from .runs import RunLifecycle

# priorities: lower first
STEP, HEAD_CHECK, PUSH, AGENT = 0, 1, 2, 3


@dataclass(frozen=True)
class VerificationKey:
    sha: str
    commands: tuple[str, ...]
    setup_pin: str | None
    image: str | None
    timeout_s: int

    @property
    def hash(self) -> str:
        blob = json.dumps([self.sha, list(self.commands), self.setup_pin, self.image,
                           self.timeout_s])
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


def verification_of(sha: str, outcome: Outcome) -> Verification:
    """The verification record of a verifier's work: the setup's and the commands' results; a
    sandbox that could not be made ready (created, cloned, checked out) is an infrastructure
    error."""
    res = Verification(sha=sha, sandbox=outcome.sandbox,
                       results=outcome.results(SETUP, COMMAND))
    stop = outcome.stopped
    if outcome.error:
        res.infra_error = outcome.error
    elif stop is not None and stop.command.kind == PREPARE:
        res.infra_error = (f"verifier {stop.command.label} failed ({stop.result.exit_code}): "
                           f"{stop.result.output[-1500:]}")
    res.ok = res.infra_error is None and all(r.exit_code == 0 for r in res.results)
    return res


def record_path(run_id: str) -> str:
    """A verification's whole record (its log is ``control/runs/<sandbox>/log``): audit, under
    its unique id."""
    return f"control/verifications/{run_id}/record.json"


class Verifications:
    """The change's verification service (one per change; the cap is the controller's, shared
    by every change)."""

    def __init__(self, core: ChangeCore, sandboxes: Sandboxes, dispatcher: Dispatcher,
                 lifecycle: RunLifecycle, cap: PriorityCap):
        self.c, self.sandboxes, self.dispatcher, self.lifecycle = core, sandboxes, dispatcher, lifecycle
        self.cap = cap
        self._futures: dict[str, asyncio.Future] = {}     # run id -> its result, while live
        self._watchers: dict[str, list[Callable[[str], None]]] = {}   # run id -> told its sandbox
        self._inner: dict[str, asyncio.Future] = {}       # run id -> its wait for a slot
        self._started: set[str] = set()                   # past the cap: no longer cancelled

    # ------------------------------------------------------------------ keys
    def key(self, sha: str, commands: tuple[str, ...] | list[str]) -> VerificationKey:
        """The key of ``commands`` at ``sha`` as the change stands now."""
        ch, sc = self.c.change, self.c.cfg.sandbox
        env = sandbox_env(ch, VERIFIER)
        image = (env.image if env and env.image else None) or sc.verifier_image or sc.image
        return VerificationKey(sha=sha, commands=tuple(commands),
                               setup_pin=ch.setup_pin.blob if ch.setup_pin else None,
                               image=image, timeout_s=self.c.cfg.limits.verify_timeout_s)

    def latest(self, key: VerificationKey) -> VerificationRun | None:
        """The key's latest run (a final one is its result)."""
        runs = [r for r in self.c.change.verifications.values() if r.key == key.hash]
        return max(runs, key=lambda r: r.attempt, default=None)

    def result(self, key: VerificationKey) -> VerificationRun | None:
        """The key's result: its passed or failed run (None: not verified yet)."""
        r = self.latest(key)
        return r if r is not None and r.state in V_FINAL else None

    # ------------------------------------------------------------------ requests
    def request(self, key: VerificationKey, by: str, priority: int, *, node: str | None = None,
                branch: str | None = None, force: bool = False) -> VerificationRun:
        """Under the lock: the key's result, the run it joins, or a new run (started in the
        background). Never waits. ``force`` (a human's decision only): a new attempt even if the
        key has a result."""
        cur = self.latest(key)
        if cur is not None and (cur.id in self._futures
                                or (cur.state in V_FINAL and not force)):
            if by not in cur.requested_by:
                cur.requested_by.append(by)
            if cur.state == VerificationState.QUEUED and priority < cur.priority:
                cur.priority = priority
            return cur
        attempt = cur.attempt + 1 if cur is not None else 1
        run = VerificationRun(id=f"v-{key.hash}" + (f"-{attempt}" if attempt > 1 else ""),
                              key=key.hash, sha=key.sha, commands=list(key.commands),
                              setup_pin=key.setup_pin, image=key.image, timeout_s=key.timeout_s,
                              node=node, branch=branch, attempt=attempt, requested_by=[by],
                              priority=priority)
        self.c.change.verifications[run.id] = run
        if branch:
            self._supersede(branch, run)
        self._futures[run.id] = asyncio.get_running_loop().create_future()
        self.c.event("verify", f"verification {run.id} of {key.sha[:12]} queued ({by})", node)
        self.c.spawn(self._run(run, key))
        return run

    async def verify(self, key: VerificationKey, by: str, priority: int, *, node: str | None = None,
                     on_sandbox: Callable[[str], None] | None = None,
                     force: bool = False) -> VerificationRun:
        """The key's result, verifying it if needed (joins a live run). ``on_sandbox``: told
        the verifier sandbox once it has one (a live log to show)."""
        async with self.c.lock:
            run = self.request(key, by, priority, node=node, force=force)
            fut = self._futures.get(run.id)
            if fut is not None and on_sandbox is not None:
                if run.sandbox:
                    on_sandbox(run.sandbox)
                self._watchers.setdefault(run.id, []).append(on_sandbox)
        if fut is not None:
            await asyncio.shield(fut)        # a caller that goes away leaves the run to finish
        return self.c.change.verifications[run.id]

    def _supersede(self, branch: str, newer: VerificationRun) -> None:
        """A newer tip of ``branch``: its queued verifications only pushes asked for go."""
        for r in self.c.change.verifications.values():
            if (r is not newer and r.branch == branch and r.state == VerificationState.QUEUED
                    and r.id not in self._started
                    and all(b.startswith("push:") for b in r.requested_by)):
                r.state, r.ended_at = VerificationState.CANCELLED, now_iso()
                self._cancel(r)
                self.c.event("verify", f"verification {r.id} of {r.sha[:12]} cancelled: "
                                       f"{branch} was pushed again", r.node)

    # ------------------------------------------------------------------ running
    async def _run(self, run: VerificationRun, key: VerificationKey) -> None:
        """Run it: a dispatcher slot, then a place in the verification cap – taken only inside a
        slot, so a holder always gets on – then the verifier."""
        fut = self._futures[run.id]
        try:
            if not key.commands and not key.setup_pin:
                res = Verification(sha=key.sha, infra_error="no commands to verify")
            else:
                res = await self._dispatched(run, key)
            if res is None:                              # cancelled: a newer push
                return
            async with self.c.lock:
                full = res.model_copy(update={"run": run.id})
                run.result = full.model_copy(update={
                    "results": [r.model_copy(update={"output": ""}) for r in res.results]})
                run.state = (VerificationState.INFRA_ERROR if res.infra_error
                             else VerificationState.PASSED if res.ok else VerificationState.FAILED)
                run.ended_at = now_iso()
                run.result.attestation = self.c.write_record(record_path(run.id), full).attestation
                self.c.event("verify", f"verification {run.id} of {run.sha[:12]}: {run.state}",
                             run.node)
                self.c.save()
        except Exception as e:                       # never strands its waiters
            async with self.c.lock:
                run.state, run.ended_at = VerificationState.INFRA_ERROR, now_iso()
                run.result = Verification(sha=run.sha, infra_error=f"{type(e).__name__}: {e}",
                                          run=run.id)
                self.c.save()
        finally:
            for d in (self._futures, self._watchers, self._inner):
                d.pop(run.id, None)
            self._started.discard(run.id)
            if not fut.done():
                fut.set_result(None)

    async def _dispatched(self, run: VerificationRun, key: VerificationKey) -> Verification | None:
        c = self.c
        log_state: dict = {}
        capped = _Capped(self, run, key, log_state)
        inner = asyncio.ensure_future(self.dispatcher.run(capped))
        self._inner[run.id] = inner
        try:
            outcome = await inner
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise                                # the controller stops
            return None                              # cancelled: a newer push
        res = verification_of(key.sha, outcome)
        name, write = log_state["name"], log_state["write"]
        if res.infra_error:
            write(f"[legion] {res.infra_error}\n")
        write(f"[legion] verification {'passed' if res.ok else 'failed'}\n")
        async with c.lock:
            stored = self.lifecycle.store_log(name) is not None
            c.save()
        res.log = name if stored else None
        return res

    def _cancel(self, run: VerificationRun) -> None:
        inner = self._inner.get(run.id)
        if inner is not None and not inner.done() and run.id not in self._started:
            inner.cancel()


    # ------------------------------------------------------------------ restart
    def recover(self) -> None:
        """After a restart, under the lock: runs that were queued or running are gone (an
        infrastructure error: asked for again, they run again)."""
        for r in self.c.change.verifications.values():
            if r.state in (VerificationState.QUEUED, VerificationState.RUNNING):
                r.state, r.ended_at = VerificationState.INFRA_ERROR, now_iso()
                r.result = Verification(sha=r.sha, run=r.id,
                                        infra_error="the controller restarted during it")


class _Capped:
    """A verification's work for the dispatcher: in its slot it takes its place in the
    verification cap, then the verifier sandbox and its commands, its log written as each
    starts and ends (live, then stored)."""

    def __init__(self, svc: Verifications, run: VerificationRun, key: VerificationKey,
                 log_state: dict):
        self.svc, self.run, self.key, self.log_state = svc, run, key, log_state

    def can_start(self) -> bool:
        """Once the verifier's environment is built (no sandbox from the old build)."""
        return self.svc.sandboxes.environment_built(VERIFIER)

    async def execute(self):
        svc, run, key, c = self.svc, self.run, self.key, self.svc.c
        await svc.cap.acquire(run.priority)     # cancelled meanwhile: superseded, or adopted
        svc._started.add(run.id)                # from here on it runs where it is
        try:
            async with c.lock:
                name = c.host.new_sandbox_name(VERIFIER_PREFIX)
                run.state, run.started_at, run.sandbox = VerificationState.RUNNING, now_iso(), name
                for tell in svc._watchers.get(run.id, []):
                    tell(name)
                c.save()
            work = svc.sandboxes.verifier_work(name, run.node or run.id, key.sha, key.commands)
            log = c.run_dir(name) / "agent.log"
            log.parent.mkdir(parents=True, exist_ok=True)

            def write(text: str) -> None:
                with open(log, "a") as f:
                    f.write(text)
                c.host.touch()                           # a live log's viewers read it again

            def ran(cmd, r) -> None:
                out = r.output if not r.output or r.output.endswith("\n") else r.output + "\n"
                write(f"{out}[legion] {cmd.label}: exit {r.exit_code}\n")
            self.log_state.update(name=name, write=write)
            work.begun, work.log = (lambda cmd: write(f"[legion] {cmd.label} …\n")), ran
            write(f"[legion] verifying {key.sha[:12]} ({run.id}, for "
                  f"{', '.join(run.requested_by)}) in {name}: {len(work.commands)} step(s)\n")
            return await work.execute()
        finally:
            svc.cap.release()
