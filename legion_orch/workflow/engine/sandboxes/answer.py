"""Answering another agent (A2A) for a step whose agent is not running.

A step's card stays registered for as long as its agent's sandbox exists, also while it is
stopped between turns. A message for a step with a live run is delivered to its agent's inbox
(the MCP ``a2a_inbox`` tool); otherwise the controller queues it here: it starts the sandbox if it
was stopped, resumes the agent's session for one turn on the message, sends back what the agent
answers, and stops the sandbox again if it found it stopped. One turn at a time per sandbox,
under its lock, so a rework launched meanwhile waits for the answer (and an answer never
resumes a session a live run is writing)."""
from __future__ import annotations

import asyncio
import itertools
from typing import TYPE_CHECKING

from ....sandbox import agentlog
from ...model import LIVE_RUN, Run
from ..base.common import AgentBusy, NoAgent, WorkflowError
from ..base.core import ChangeCore
from .turn import resumed_turn

if TYPE_CHECKING:
    from .sandboxes import Sandboxes

PROMPT = """# A message from another agent

`{sender}` – another agent working on this change – sent you this message over A2A
(agent-to-agent) while you were not running. You are the `{role}` agent for `{node}`:

<message from="{sender}">
{text}
</message>

Answer it from what you know of your work in this session: your answer is sent back to
`{sender}` verbatim, and nothing else you do in this turn is seen. Your only tool in this turn is
the Skill tool; nothing is to change (no files, no reports). If you cannot answer, say so and why.
"""


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

    async def answer(self, node_id: str, role: str, sender: str, text: str) -> str:
        """One resumed turn of the ``role`` agent of ``node_id`` on ``text``; its answer. Raises
        :class:`AgentBusy` while it has a live run, :class:`NoAgent` without a session to
        resume, and :class:`WorkflowError` when the turn gives no answer."""
        async with self.lock(node_id, role):
            return await self.answer_locked(node_id, role, sender, text)

    async def answer_locked(self, node_id: str, role: str, sender: str, text: str) -> str:
        """:meth:`answer`, the step agent's A2A lock held."""
        c = self.c
        async with c.lock:
            run = self.session_run(node_id, role)
        if run is None:
            raise NoAgent(f"{role}@{node_id} has no sandbox")
        name = run.sandbox
        async with self.sandboxes.lock(name):
            async with c.lock:
                if self.live(node_id, role):
                    raise AgentBusy(f"{role}@{node_id} is running")
                run = self.session_run(node_id, role)
                if run is None or run.sandbox != name or name in self.sandboxes.retiring:
                    raise NoAgent(f"{role}@{node_id} finished its work without answering (its "
                                  "sandbox is being retired)")
                if not run.session_id:
                    raise NoAgent(f"{role}@{node_id} has no agent session recorded to resume")
                stopped_runs = [r for r in c.runs.values() if r.sandbox == name and r.sandbox_stopped]
                stopped_nodes = [n for n in c.nodes.values() if n.sandbox == name and n.sandbox_stopped]
                stopped = bool(stopped_runs or stopped_nodes)
                turn = f"{run.id}-a2a{next(self._ids)}"
                prompt = PROMPT.format(sender=sender, role=run.role, node=node_id, text=text)
                c.event("a2a", f"{role}@{node_id} is not running: "
                               + (f"starting {name} and " if stopped else "")
                               + f"resuming run {run.id}'s session to answer {sender}",
                        node_id, run.id)
                c.save()
            try:
                rc, log = await resumed_turn(c, self.sandboxes, name, run, turn, prompt,
                                             timeout=c.cfg.limits.a2a_answer_timeout_s,
                                             wake=stopped)
            finally:
                if stopped:
                    await self._stop_again(name, stopped_runs, stopped_nodes)
            result = agentlog.agent_result(log)
            async with c.lock:
                c.write_record(f"control/logs/{turn}.log", log[-1024 * 1024:])
                if result is None or result.is_error or not result.message.strip():
                    c.event("a2a", f"{role}@{node_id} gave no answer to {sender} (exit {rc})",
                            node_id, run.id)
                    c.save()
                    raise WorkflowError(f"{role}@{node_id} gave no answer (exit {rc})")
                c.event("a2a", f"{role}@{node_id} answered {sender}", node_id, run.id)
                c.save()
            return result.message.strip()

    async def _stop_again(self, name: str, runs: list, nodes: list) -> None:
        """Leave the sandbox as it was found: stopped (best effort)."""
        c = self.c
        try:
            await c.runner.stop(name)
        except Exception as e:
            async with c.lock:
                c.event("sandbox", f"could not stop {name} again after answering; it stays "
                                   f"running: {e}")
                c.save()
            return
        async with c.lock:
            for r in runs:
                r.sandbox_stopped = True
            for n in nodes:
                if n.sandbox == name:
                    n.sandbox_stopped = True
            c.save()
