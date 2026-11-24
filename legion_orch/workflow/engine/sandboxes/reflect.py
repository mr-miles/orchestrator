"""Reflection: before an agent's sandbox is deleted because its work is done, the agent's session
is resumed for one more turn in which it says what cost it time and where its guidance was missing
or wrong. It still has its whole context, which a later retrospective (on demand, at the end of the
change) does not: the reflections tell that retrospective where to look and what it cost.

Best-effort throughout: a reflection that cannot run, fails or times out is noted and skipped; it
never holds up the change or keeps a sandbox alive past ``retrospective.reflect_timeout_s``.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from ....sandbox import agentlog
from ...model import Run
from ..base.core import ChangeCore
from .turn import resumed_turn

if TYPE_CHECKING:
    from .sandboxes import Sandboxes

PROMPT = """# Reflection on your work

Your part in this change is finished: {outcome}. Before your sandbox is deleted, look back over
this whole session. Your answer is read by whoever improves the instructions, tools and process
you worked with, so that the next agent avoids what slowed you down or misled you.

Your only tool in this turn is the Skill tool: load a legion skill again to check what your
instructions said. Nothing is to change: answer in plain text, from what you remember of this
session. Be specific and honest; name files, commands, instructions and turns.
Estimate cost (turns or minutes) where you can. "Nothing" is a fine answer for a section.

{context}
Answer in markdown with exactly these sections:

## Time sinks
What took the most time or turns, and why (slow or repeated commands, searching for
something, waiting, dead ends, retries). Rank by cost.

## Guidance
Instructions that were missing, unclear, wrong or contradictory: your prompt, your soul
(persona), skills (name the skill), the task contract, the documents you were given. Quote the
instruction and say what it should say instead.

## Tools and environment
Friction with tools, the sandbox, the network policy, the MCP tools, git, the build or test
setup: what failed, what was missing, what you had to work around.

## Mistakes
{mistakes}

## Suggestions
The few changes (to instructions, tools, environment or process) that would have helped you
most, highest value first.
"""

IMPLEMENTER_MISTAKES = """Each finding a reviewer, a verification command or a human raised against
your work (listed above): why you missed it, and what instruction or check would have caught it
before review."""
DEFAULT_MISTAKES = """Anything you got wrong and had to correct (including reports the controller
rejected), and what would have prevented it."""


def _outcome(run: Run) -> str:
    return {"ACCEPTED": "your work was accepted",
            "FAILED": "your run failed"}.get(str(run.status), f"your run ended ({run.status})")


def reflection_prompt(c: ChangeCore, runs: list[Run]) -> str:
    """The reflection prompt for the session behind ``runs`` (one ticket and role, oldest first)."""
    last = runs[-1]
    ticket = c.tickets.t.get(last.ticket)
    lines = [f"You were the `{last.role}` agent of ticket `{last.ticket}`"
             + (f" over {len(runs)} runs (attempts {', '.join(str(r.attempt) for r in runs)})."
                if len(runs) > 1 else ".")]
    told = list(dict.fromkeys(f for r in runs for f in (r.assignment.get("findings") or [])))
    findings = bool(told or (ticket and ticket.findings))
    if findings:
        lines.append("\nFindings your work was told to fix:")
        lines += [f"- {f[:600]}" for f in (told or ticket.findings)[:40]]
    else:
        lines.append("\nNo reviewer, verification or human finding was raised against your work.")
    rejected = [(r.id, v) for r in runs for v in r.violations]
    if rejected:
        lines.append("\nReports of yours the controller rejected (guard ids and reasons):")
        lines += [f"- {rid}: {'; '.join(v)[:400]}" for rid, v in rejected[:20]]
    return PROMPT.format(outcome=_outcome(last), context="\n".join(lines) + "\n",
                         mistakes=IMPLEMENTER_MISTAKES if last.role == "implementer" and findings
                         else DEFAULT_MISTAKES)


def reflection_path(run_id: str) -> str:
    return f"control/reflections/{run_id}.md"


def sandbox_skipped(c: ChangeCore, sandbox: str) -> bool:
    """Only agent sandboxes of roles that reflect."""
    roles = {r.role for r in c.runs.values() if r.sandbox == sandbox}
    return not roles or roles <= set(c.cfg.retrospective.reflect_skip_roles)


class Reflector:
    POLL_S = 1.0

    def __init__(self, core: ChangeCore, sandboxes: Sandboxes):
        self.c, self.sandboxes = core, sandboxes
        self.pending: set[str] = set()       # sandboxes whose reflection has not finished

    def _session_runs(self, sandbox: str) -> list[Run]:
        """The runs whose agent session the sandbox still holds: the latest run with a session and
        the earlier runs of the same ticket and role in this sandbox (a resumed session)."""
        skip = set(self.c.cfg.retrospective.reflect_skip_roles)
        runs = [r for r in self.c.runs.values()
                if r.sandbox == sandbox and r.session_id and r.role not in skip]
        if not runs:
            return []
        last = runs[-1]
        return [r for r in runs if r.ticket == last.ticket and r.role == last.role]

    async def reflect(self, sandbox: str, stopped: bool = False) -> None:
        """Resume the sandbox's agent session for a reflection turn and record the answer."""
        c, cfg = self.c, self.c.cfg.retrospective
        if not cfg.reflect or sandbox_skipped(c, sandbox):
            return
        # the agent may still be ending its turn after its report: its log (and session id) is
        # archived when its process exits
        live = lambda: any(r.sandbox == sandbox and r.id in c.procs  # noqa: E731
                           for r in c.runs.values())
        for _ in range(int(cfg.reflect_timeout_s / self.POLL_S)):
            if not live():
                break
            await asyncio.sleep(self.POLL_S)
        if live():           # never resume a session its own agent is still writing
            c.event("reflection", f"sandbox {sandbox}: the agent has not exited; no reflection")
            return
        async with c.lock:
            runs = self._session_runs(sandbox)
            if not runs:
                c.event("reflection", f"sandbox {sandbox}: no agent session recorded; "
                                      "no reflection")
                return
            if c.latest(reflection_path(runs[-1].id)):
                return                      # already reflected (a release seen twice)
            last = runs[-1]
            prompt = reflection_prompt(c, runs)
        rid = f"{last.id}-reflect"
        c.event("reflection", f"asking {last.role} run {last.id} to reflect", last.ticket, last.id)
        try:
            rc, text = await resumed_turn(c, self.sandboxes, sandbox, last, rid, prompt,
                                          timeout=cfg.reflect_timeout_s, wake=stopped)
            result = agentlog.agent_result(text)
            async with c.lock:
                if result is None or result.is_error or not result.message.strip():
                    c.event("reflection", f"no reflection from run {last.id} (exit {rc})",
                            last.ticket, last.id)
                    return
                header = (f"# Reflection: {last.role} on {last.ticket}\n\n"
                          f"Runs: {', '.join(r.id for r in runs)}; session {last.session_id}\n\n")
                got = agentlog.session_id(text)
                if got and got != last.session_id:     # --resume did not take: a new session
                    c.event("reflection", f"run {last.id}: the agent did not resume session "
                                          f"{last.session_id} (it ran in {got})",
                            last.ticket, last.id)
                    header += (f"> The agent did not resume session {last.session_id}: it ran in "
                               f"a new one ({got}), without this session's history.\n\n")
                c.write_record(reflection_path(last.id), header + result.message.strip() + "\n")
                c.write_record(f"control/logs/{rid}.log", text[-1024 * 1024:])
                c.event("reflection", f"recorded {reflection_path(last.id)}", last.ticket, last.id)
                c.save()
        except Exception as e:                      # best-effort: never blocks the deletion
            c.event("reflection", f"reflection of run {last.id} skipped: {e or type(e).__name__}",
                    last.ticket, last.id)
