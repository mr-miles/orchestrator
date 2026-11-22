"""Who answers for a step agent between its runs: its latest sandbox and session
(:meth:`Answerer.session_run`), whether a run of it is in its turn (:meth:`Answerer.live`), and
one lock per agent so its out-of-step turns – a consult, ``AgentRuns.consult`` – take turns.

(The turn itself is a consult run: started if the agent has not, else resumed in place. The
separate resumed "answer" turn this module used to run is gone.)
"""
from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING

from ...model import LIVE_RUN, Run
from ..base.core import ChangeCore

if TYPE_CHECKING:
    from .sandboxes import Sandboxes



class Answerer:
    def __init__(self, core: ChangeCore, sandboxes: Sandboxes):
        self.c, self.sandboxes = core, sandboxes
        self._ids = itertools.count(1)
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    def lock(self, node_id: str, role: str) -> asyncio.Lock:
        """One A2A turn at a time per step agent (a consult and a resumed turn alike)."""
        return self._locks.setdefault((node_id, role), asyncio.Lock())

    def session_run(self, node_id: str, role: str) -> Run | None:
        """The step agent's latest run whose sandbox still exists (its card's), preferring one
        with a session to resume."""
        runs = [r for r in self.c.runs.values() if r.node_id == node_id and r.role == role
                and r.sandbox and r.sandbox_ref and not r.sandbox_deleted]
        if not runs:
            return None
        same = [r for r in runs if r.sandbox == runs[-1].sandbox and r.session_id]
        return (same or runs)[-1]

    def live(self, node_id: str, role: str) -> bool:
        """A run of the step agent is in its turn (its own A2A turns aside)."""
        return any(r.node_id == node_id and r.role == role and not r.consult
                   and (r.status in LIVE_RUN or r.id in self.c.procs)
                   for r in self.c.runs.values())
