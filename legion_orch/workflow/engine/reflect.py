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

from ...config import agent_command
from ...sandbox import RUN_AGENT_SH, agentlog
from ..model import Run
from .core import ChangeCore

PROMPT = """# Reflection on your work

Your part in this change is finished: {outcome}. Before your sandbox is deleted, look back over
this whole session. Your answer is read by whoever improves the instructions, tools and process
you worked with, so that the next agent avoids what slowed you down or misled you.

You have no tools in this turn and nothing is to change: answer in plain text, from what you
remember of this session and from the controller's record of your runs below. Be specific and
honest; name files, commands, instructions and turns, and take costs from the record. "Nothing"
is a fine answer for a section that has nothing, not a way to skip one: the record shows where
time went.

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
before review. Then anything you got wrong and corrected yourself."""
DEFAULT_MISTAKES = """Anything you got wrong and had to correct (including reports the controller
rejected), and what would have prevented it."""


def _outcome(run: Run) -> str:
    return {"ACCEPTED": "your work was accepted",
            "SUPERSEDED": "your work was superseded by a later cycle",
            "FAILED": "your run failed"}.get(str(run.status), f"your run ended ({run.status})")


def _cycles(runs: list[Run]) -> str:
    return ", ".join(str(r.cycle) for r in runs)


def _clip(s: object, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _record(c: ChangeCore, run: Run) -> list[str]:
    """What the controller recorded of a run, from its log's digest (see
    :func:`agentlog.digest`), in a few lines."""
    path = f"control/digests/{run.id}.json"
    d = c.json_doc(path) if c.latest(path) else None
    head = f"- run {run.id}, cycle {run.cycle}, {str(run.status).lower()}"
    if not d:
        return [head + ": no log was recorded"]
    res = (d.get("results") or [{}])[-1]
    facts = [f"{d['wall_seconds'] / 60:.0f} min" if d.get("wall_seconds") else None,
             f"{res['num_turns']} turns" if res.get("num_turns") else None,
             f"{d.get('tool_error_count', 0)} failed tool calls"]
    out = [head + ": " + ", ".join(f for f in facts if f)]
    if tools := list((d.get("tools") or {}).items())[:8]:
        out.append("  tools: " + ", ".join(f"{t} ×{n.get('calls', 0)}" for t, n in tools))
    if slow := [s for s in d.get("slowest_tool_calls") or [] if s.get("seconds", 0) >= 30][:5]:
        out.append("  slowest: " + "; ".join(
            f"{s['tool']} `{_clip(s['input'], 120)}` ({s['seconds']:.0f}s)" for s in slow))
    if rep := (d.get("repeated_commands") or [])[:5]:
        out.append("  repeated: " + "; ".join(
            f"`{_clip(r['value'], 120)}` ×{r['count']}" for r in rep))
    if errs := (d.get("tool_errors") or [])[:5]:
        out.append("  failed: " + "; ".join(
            f"{e['tool']} `{_clip(e['input'], 100)}`: {_clip(e['error'], 160)}" for e in errs))
    return out


def reflection_prompt(c: ChangeCore, runs: list[Run]) -> str:
    """The reflection prompt for the session behind ``runs`` (one node and role, oldest first).
    The role's earlier runs on the node were separate sessions (an implementer starts afresh
    each cycle): the prompt carries the controller's record of them instead."""
    last = runs[-1]
    node = c.nodes.get(last.node_id)
    ids = {r.id for r in runs}
    earlier = [r for r in c.runs.values() if r.node_id == last.node_id and r.role == last.role
               and r.id not in ids and r.cycle <= last.cycle]
    lines = [f"You were the `{last.role}` agent for `{last.node_id}`. This session covers "
             + (f"{len(runs)} runs (cycles {_cycles(runs)})." if len(runs) > 1
                else f"run {last.id} (cycle {last.cycle}).")]
    if earlier:
        lines.append(f"\nEarlier cycles of this task ({_cycles(earlier)}) ran in separate sessions "
                     "that you cannot remember: rely on the record below for those.")
    lines.append("\nWhat the controller recorded of each run, from its log (use it to check and "
                 "quantify what you remember, and say where you rely on it):")
    for r in (earlier[-6:] + runs)[-10:]:
        lines += _record(c, r)
    if node and node.feedback:
        lines.append("\nFeedback your work received (cycle, source: finding):")
        lines += [f"- cycle {f.cycle}, {f.source}: {f.text[:600]}" for f in node.feedback[:40]]
    else:
        lines.append("\nNo reviewer, verification or human finding was raised against your work.")
    rejected = [(r.id, v) for r in earlier + runs for v in r.violations]
    if rejected:
        lines.append("\nReports of yours the controller rejected (guard ids and reasons):")
        lines += [f"- {rid}: {'; '.join(v)[:400]}" for rid, v in rejected[:20]]
    return PROMPT.format(outcome=_outcome(last), context="\n".join(lines) + "\n",
                         mistakes=IMPLEMENTER_MISTAKES if last.role == "implementer"
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
        """The runs of the agent session the sandbox still holds, oldest first: the latest run
        with a session and the runs it continued (``resumed_from``). A run that started a new
        session (an implementer's next cycle) is not part of it."""
        skip = set(self.c.cfg.retrospective.reflect_skip_roles)
        runs = [r for r in self.c.runs.values()
                if r.sandbox == sandbox and r.session_id and r.role not in skip]
        if not runs:
            return []
        chain = [runs[-1]]
        while (prev := self.c.runs.get(chain[0].resumed_from or "")) is not None \
                and prev not in chain:
            chain.insert(0, prev)
        return chain

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
               "LEGION_AGENT_COMMAND": agent_command(c.cfg.sandbox, tools=False),
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
                if result is None or result.is_error or not result.message.strip():
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
                               f"a new one ({got}), without this session's history, so this "
                               "answer rests on the controller's record in its prompt.\n\n")
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
