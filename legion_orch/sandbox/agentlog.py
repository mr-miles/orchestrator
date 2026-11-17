"""Reading an agent log: the sandbox's own output (setup script, git, shell errors) interleaved
with the agent CLI's stream-json events (Claude Code ``--output-format stream-json``).

:func:`entries` turns a log into display entries for the UI; :func:`agent_result` finds how the
agent itself ended, which tells an agent error (it ran and failed: API error, turn limit) from a
system error (it never ran, or the process died: no result event at all).

A ``result`` event ends a *turn*, not necessarily the run. When the agent ends a turn while its own
background tasks (``Monitor``, ``Bash`` with ``run_in_background``) are still running, Claude Code
emits a ``result`` and keeps the process alive; when a task finishes it starts a new turn with the
notification. So a ``result`` only ends the run if no task is outstanding and no turn follows it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_TEXT = 4000          # per entry; the raw log keeps everything
# task_updated statuses after which a background task no longer runs
TASK_DONE = {"completed", "failed", "killed", "stopped", "cancelled", "error"}


@dataclass
class AgentResult:
    is_error: bool
    message: str
    subtype: str = ""
    # descriptions of the agent's background tasks still running when this turn ended
    pending: tuple[str, ...] = ()


class _Tasks:
    """The agent's outstanding background tasks, following the CLI's task events."""

    def __init__(self) -> None:
        self.running: dict[str, str] = {}      # task id -> description

    def track(self, ev: dict) -> bool:
        """Update from ``ev``; True if it was a task event."""
        if ev.get("type") != "system":
            return False
        sub, tid = ev.get("subtype"), str(ev.get("task_id") or "")
        if sub == "background_tasks_changed" and isinstance(ev.get("tasks"), list):
            # a full snapshot: authoritative, and repairs a log tail that missed task_started
            self.running = {str(t.get("task_id")): str(t.get("description") or t.get("task_id"))
                            for t in ev["tasks"] if isinstance(t, dict) and t.get("task_id")}
        elif sub == "task_started" and tid:
            self.running[tid] = str(ev.get("description") or tid)
        elif sub == "task_notification" and tid:
            self.running.pop(tid, None)
        elif sub == "task_updated" and tid:
            if str((ev.get("patch") or {}).get("status")) in TASK_DONE:
                self.running.pop(tid, None)
        else:
            return False
        return True

    def pending(self) -> tuple[str, ...]:
        return tuple(self.running.values())


def _events(text: str):
    """(raw line, parsed JSON object or None) per line."""
    for line in text.splitlines():
        s = line.strip()
        obj = None
        if s.startswith("{"):
            try:
                obj = json.loads(s)
            except ValueError:
                obj = None
        yield line, obj if isinstance(obj, dict) and "type" in obj else None


def _clip(s: str, n: int = MAX_TEXT) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [{len(s) - n} more characters]"


def _tool_name(name: str) -> str:
    """``mcp__legion__report`` -> ``legion.report``."""
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        return f"{server}.{tool}" if tool else server
    return name


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) and c.get("type") == "text"
                         else json.dumps(c) for c in content)
    return json.dumps(content)


def _input_text(inp: Any) -> str:
    """Tool input: a single obvious argument as itself (command, path), else compact JSON."""
    if isinstance(inp, dict):
        for key in ("command", "file_path", "path", "pattern", "url"):
            if isinstance(inp.get(key), str) and len(inp) <= 2:
                return inp[key]
        return json.dumps(inp, indent=1, ensure_ascii=False)
    return json.dumps(inp, ensure_ascii=False)


def _waiting_text(pending: tuple[str, ...]) -> str:
    n = len(pending)
    return (f"waiting on {n} background task{'s' if n != 1 else ''}: " + "; ".join(pending))


def _from_event(ev: dict, tasks: _Tasks) -> list[dict]:
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "task_started":
        return [{"kind": "task", "title": "started", "text": str(ev.get("description") or "")}]
    if t == "system" and ev.get("subtype") == "task_notification":
        return [{"kind": "task", "title": str(ev.get("status") or "finished"),
                 "error": ev.get("status") not in ("completed", "stopped"),
                 "text": str(ev.get("summary") or "")}]
    if t == "system" and ev.get("subtype") == "init":
        servers = ev.get("mcp_servers") or []
        bad = [s for s in servers if isinstance(s, dict) and s.get("status") != "connected"]
        mcp = ", ".join(f"{s.get('name')} {s.get('status')}" for s in servers if isinstance(s, dict))
        return [{"kind": "init", "error": bool(bad),
                 "text": f"model {ev.get('model', '?')} · {len(ev.get('tools') or [])} tools"
                         + (f" · MCP: {mcp}" if mcp else " · no MCP servers")}]
    if t in ("assistant", "user"):
        out = []
        for c in (ev.get("message") or {}).get("content") or []:
            if not isinstance(c, dict):
                continue
            ct = c.get("type")
            if ct == "text" and c.get("text", "").strip():
                out.append({"kind": "text" if t == "assistant" else "prompt",
                            "text": _clip(c["text"])})
            elif ct == "thinking" and c.get("thinking", "").strip():
                out.append({"kind": "thinking", "text": _clip(c["thinking"])})
            elif ct == "tool_use":
                out.append({"kind": "tool_call", "title": _tool_name(str(c.get("name", "?"))),
                            "text": _clip(_input_text(c.get("input")), 1500)})
            elif ct == "tool_result":
                out.append({"kind": "tool_result", "error": bool(c.get("is_error")),
                            "text": _clip(_result_text(c.get("content")), 1500)})
        return out
    if t == "result":
        r = _result(ev, tasks.pending())
        stats = " · ".join(x for x in (
            f"{ev['num_turns']} turns" if "num_turns" in ev else "",
            f"{ev['duration_ms'] / 1000:.0f}s" if isinstance(ev.get("duration_ms"), int | float) else "",
            f"${ev['total_cost_usd']:.2f}" if isinstance(ev.get("total_cost_usd"), int | float) else "",
        ) if x)
        if r.pending:      # the turn ended, the run did not: the CLI resumes when a task ends
            return [{"kind": "waiting", "error": r.is_error, "title": r.subtype or "result",
                     "text": _waiting_text(r.pending) + "\n" + _clip(r.message)}]
        return [{"kind": "result", "error": r.is_error, "title": r.subtype or "result",
                 "text": _clip(r.message) + (f"\n({stats})" if stats else "")}]
    return [{"kind": "event", "title": f"{t}{'/' + ev['subtype'] if ev.get('subtype') else ''}",
             "text": ""}]


def _result(ev: dict, pending: tuple[str, ...] = ()) -> AgentResult:
    sub = str(ev.get("subtype") or "")
    is_error = bool(ev.get("is_error")) or sub.startswith("error")
    msg = ev.get("result")
    if not isinstance(msg, str) or not msg:
        errs = ev.get("errors")
        msg = "; ".join(map(str, errs)) if isinstance(errs, list) and errs else sub or "finished"
    return AgentResult(is_error=is_error, message=msg, subtype=sub, pending=pending)


def entries(text: str) -> list[dict]:
    """Display entries, in log order. Consecutive non-JSON lines form one ``sandbox`` entry."""
    out: list[dict] = []
    tasks = _Tasks()
    for line, ev in _events(text):
        if ev is None:
            if not line.strip():
                continue
            if out and out[-1]["kind"] == "sandbox":
                out[-1]["text"] += "\n" + line
            else:
                out.append({"kind": "sandbox", "text": line})
        else:
            tasks.track(ev)
            out.extend(_from_event(ev, tasks))
    for e in out:
        e.setdefault("error", False)
        if e["kind"] == "sandbox":
            e["text"] = _clip(e["text"])
    return out


def agent_result(text: str) -> AgentResult | None:
    """How the agent's last turn ended, or None if no turn ended: it never ran, or the process
    died mid-turn (including a turn the CLI started after a background task finished). A result
    with ``pending`` tasks ended a turn that was waiting on them, so the run had not finished."""
    last, tasks = None, _Tasks()
    for _, ev in _events(text):
        if ev is None or tasks.track(ev):
            continue
        if ev.get("type") == "result":
            last = _result(ev, tasks.pending())
        elif ev.get("type") == "assistant":
            last = None              # a later turn began: that result did not end the run
    return last


def last_output(text: str, lines: int = 3) -> str:
    """The last few non-JSON lines (what the shell or setup script said before exiting)."""
    raw = [line for line, ev in _events(text) if ev is None and line.strip()]
    return "\n".join(raw[-lines:])
