"""Reflection: before an agent's sandbox is deleted because its work is done, the agent's session
is resumed for one more turn in which it says what cost it time and where its guidance was missing
or wrong. It still has its whole context, which a later retrospective (on demand, at the end of the
change) does not: the reflections tell that retrospective where to look and what it cost.

Best-effort throughout: a reflection that cannot run, fails or times out is noted and skipped; it
never holds up the change or keeps a sandbox alive past ``retrospective.reflect_timeout_s``.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import shutil

from ...sandbox import RUN_AGENT_SH, agentlog
from ..model import Run
from .core import ChangeCore

PROMPT = """# Reflection on your work

Your part in this change is finished: {outcome}. Before your sandbox is deleted, look back over
this whole session. Your answer is read by whoever improves the instructions, tools and process
you worked with, so that the next agent avoids what slowed you down or misled you.

Do not call any tools and do not change anything: answer in plain text, from what you remember
of this session. Be specific and honest; name files, commands, instructions and turns. Estimate
cost (turns or minutes) where you can. "Nothing" is a fine answer for a section.

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
            "SUPERSEDED": "your work was superseded by a later cycle",
            "FAILED": "your run failed"}.get(str(run.status), f"your run ended ({run.status})")


def reflection_prompt(c: ChangeCore, runs: list[Run]) -> str:
    """The reflection prompt for the session behind ``runs`` (one node and role, oldest first)."""
    last = runs[-1]
    node = c.nodes.get(last.node_id)
    lines = [f"You were the `{last.role}` agent for `{last.node_id}`"
             + (f" over {len(runs)} runs (cycles {', '.join(str(r.cycle) for r in runs)})."
                if len(runs) > 1 else ".")]
    findings = bool(node and node.feedback)
    if node and node.feedback:
        lines.append("\nFeedback your work received (cycle, source: finding):")
        lines += [f"- cycle {f.cycle}, {f.source}: {f.text[:600]}" for f in node.feedback[:40]]
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

    def __init__(self, core: ChangeCore):
        self.c = core
        self.pending: set[str] = set()       # sandboxes whose reflection has not finished

    def _session_runs(self, sandbox: str) -> list[Run]:
        """The runs whose agent session the sandbox still holds: the latest run with a session and
        the earlier runs of the same node and role in this sandbox (a resumed session)."""
        skip = set(self.c.cfg.retrospective.reflect_skip_roles)
        runs = [r for r in self.c.runs.values()
                if r.sandbox == sandbox and r.session_id and r.role not in skip]
        if not runs:
            return []
        last = runs[-1]
        return [r for r in runs if r.node_id == last.node_id and r.role == last.role]

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
            soul = c.assets.soul(last.soul) if last.soul in c.assets.souls_ else ""
        rid = f"{last.id}-reflect"
        d = c.run_dir(rid)
        payload, log_file = d / "payload", d / "agent.log"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "prompt.md").write_text(prompt)
        (payload / "soul.md").write_text(soul)
        (payload / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        env = {"LEGION_RUN_ID": rid, "LEGION_ROLE": last.role, "LEGION_NEEDS_REPO": "0",
               "LEGION_AGENT_COMMAND": c.cfg.sandbox.agent_command,
               "LEGION_RESUME_SESSION": last.session_id, "LEGION_REQUIRE_SESSION": "1"}
        (payload / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n"
                                                 for k, v in env.items()))
        (payload / "run-agent.sh").write_text(RUN_AGENT_SH)
        c.event("reflection", f"asking {last.role} run {last.id} to reflect", last.node_id, last.id)
        proc = None
        try:
            if stopped:
                await c.runner.start(sandbox)
            await c.runner.upload(sandbox, payload)
            proc = await c.runner.start_agent(sandbox, log_file)
            rc = await asyncio.wait_for(proc.wait(), cfg.reflect_timeout_s)
            text = log_file.read_text(errors="replace") if log_file.exists() else ""
            result = agentlog.agent_result(text)
            async with c.lock:
                if result is None or result.is_error:
                    c.event("reflection", f"no reflection from run {last.id} (exit {rc})",
                            last.node_id, last.id)
                    return
                header = (f"# Reflection: {last.role} on {last.node_id}\n\n"
                          f"Runs: {', '.join(r.id for r in runs)}; session {last.session_id}\n\n")
                got = agentlog.session_id(text)
                if got and got != last.session_id:     # --resume did not take: a new session
                    c.event("reflection", f"run {last.id}: the agent did not resume session "
                                          f"{last.session_id} (it ran in {got})",
                            last.node_id, last.id)
                    header += (f"> The agent did not resume session {last.session_id}: it ran in "
                               f"a new one ({got}), without this session's history.\n\n")
                c.write_record(reflection_path(last.id), header + result.message.strip() + "\n")
                c.write_record(f"control/logs/{rid}.log", text[-1024 * 1024:])
                c.event("reflection", f"recorded {reflection_path(last.id)}", last.node_id, last.id)
                c.save()
        except Exception as e:                      # best-effort: never blocks the deletion
            if proc is not None and proc.returncode is None:
                proc.kill()
            c.event("reflection", f"reflection of run {last.id} skipped: {e or type(e).__name__}",
                    last.node_id, last.id)
        finally:
            shutil.rmtree(d, ignore_errors=True)
