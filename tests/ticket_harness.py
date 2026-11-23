"""Helpers for the ticket tests: commits made straight in a bare hub (as an agent's push), and a
scripted executor standing in for agents, humans and the controller's services."""
from __future__ import annotations

import asyncio
import os
import subprocess
from collections import defaultdict
from collections.abc import Callable

from legion_orch.git.remote import GitRemote
from legion_orch.workflow.tickets.executors import Done, Judged, Outcome, Work


def commit(hub: GitRemote, parent: str, files: dict[str, str], msg: str) -> str:
    """A commit on ``parent`` changing ``files``, made in the bare hub."""
    env = {**os.environ, "GIT_INDEX_FILE": str(hub.root / f"index-{msg}")}

    def g(*a, input=None):
        return subprocess.run(["git", *a], cwd=hub.repo, env=env, check=True, capture_output=True,
                              text=True, input=input).stdout.strip()
    g("read-tree", parent)
    for path, text in files.items():
        blob = g("hash-object", "-w", "--stdin", input=text)
        g("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
    tree = g("write-tree")
    return g("-c", "user.name=a", "-c", "user.email=a@a", "commit-tree", tree, "-p", parent,
             "-m", msg)


def files_at(hub: GitRemote, ref: str) -> list[str]:
    return subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=hub.repo, check=True,
                          capture_output=True, text=True).stdout.split()


class Script:
    """Every ticket's work, scripted: by default a criterion passes, a code ticket (``CODE``)
    commits ``<id>.txt`` on its base, anything else is done. ``outcomes[id]``: a function of
    the work giving its outcome instead; ``gates[id]``: an event its work waits for;
    :meth:`reached`: until a ticket's work has started."""

    CODE = frozenset({"agent:implementer", "agent:oracle", "agent:baseline"})

    def __init__(self, hub: GitRemote):
        self.hub = hub
        self.calls: list[tuple[str, int]] = []
        self.stopped: list[str] = []
        self.outcomes: dict[str, Callable[[Work], Outcome]] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self._started: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def run(self, work: Work) -> Outcome:
        tid = work.ticket.id
        self.calls.append((tid, work.attempt))
        self._started[tid].set()
        if gate := self.gates.get(tid):
            await gate.wait()
        if f := self.outcomes.get(tid):
            return f(work)
        if work.ticket.role == "criterion":
            return Judged(True)
        if work.ticket.type in self.CODE and work.base:
            return Done(commit(self.hub, work.base, {f"{tid}.txt": f"{tid} {work.attempt}\n"},
                               f"{tid}-{work.attempt}"))
        return Done()

    async def reached(self, ticket: str, timeout: float = 10) -> None:
        await asyncio.wait_for(self._started[ticket].wait(), timeout)

    async def stop(self, ticket: str) -> None:
        self.stopped.append(ticket)

    def ran(self) -> list[str]:
        return [t for t, _ in self.calls]
