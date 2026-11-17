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
from datetime import datetime
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


def session_id(text: str) -> str | None:
    """The agent CLI's session id (Claude Code puts it on its init and result events), so a
    later run in the same sandbox can resume the conversation; None if the log has none."""
    found = None
    for _, ev in _events(text):
        if ev is None or not (ev.get("type") == "result"
                              or (ev.get("type") == "system" and ev.get("subtype") == "init")):
            continue
        sid = ev.get("session_id")
        if isinstance(sid, str) and sid and all(c.isalnum() or c in "-_" for c in sid):
            found = sid
    return found


def _ts(ev: dict) -> float | None:
    ts = ev.get("legion_ts")
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None
    except (AttributeError, ValueError):
        return None


def digest(text: str) -> dict[str, Any]:
    """What a run did, for a retrospective to read before (or instead of) its whole log: tool use
    and failures, repeated commands and reads, the slowest tool calls and the longest waits on
    the model (from the controller's ``legion_ts`` on each event), turns and cost."""
    tools: dict[str, dict[str, int]] = {}
    commands: dict[str, int] = {}
    reads: dict[str, int] = {}
    errors: list[dict] = []
    started: dict[str, tuple[str, str, float | None]] = {}     # tool_use id -> (tool, input, ts)
    slow: list[dict] = []
    thinking: list[dict] = []           # gaps before an assistant event: time the model took
    results: list[dict] = []
    first = last = None
    prev_ts = None
    init: dict[str, Any] = {}
    subagent_events = sandbox_lines = 0
    for line, ev in _events(text):
        if ev is None:
            sandbox_lines += bool(line.strip())
            continue
        ts = _ts(ev)
        if ts is not None:
            first = first if first is not None else ts
            last = ts
        if ev.get("parent_tool_use_id"):
            subagent_events += 1
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init" and not init:
            init = {"model": ev.get("model"), "session_id": ev.get("session_id"),
                    "tools": len(ev.get("tools") or [])}
        elif t == "assistant":
            if ts is not None and prev_ts is not None and not ev.get("parent_tool_use_id"):
                thinking.append({"seconds": round(ts - prev_ts, 1), "at": ev.get("legion_ts")})
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name, inp = _tool_name(str(c.get("name", "?"))), c.get("input")
                tools.setdefault(name, {"calls": 0, "errors": 0})["calls"] += 1
                if isinstance(inp, dict) and isinstance(inp.get("command"), str):
                    commands[inp["command"]] = commands.get(inp["command"], 0) + 1
                if name == "Read" and isinstance(inp, dict) and isinstance(inp.get("file_path"), str):
                    reads[inp["file_path"]] = reads.get(inp["file_path"], 0) + 1
                started[str(c.get("id"))] = (name, _clip(_input_text(inp), 300), ts)
        elif t == "user":
            for c in (ev.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_result":
                    continue
                name, inp, t0 = started.get(str(c.get("tool_use_id")), ("?", "", None))
                if ts is not None and t0 is not None:
                    slow.append({"tool": name, "input": inp, "seconds": round(ts - t0, 1)})
                if c.get("is_error"):
                    tools.setdefault(name, {"calls": 0, "errors": 0})["errors"] += 1
                    errors.append({"tool": name, "input": inp, "at": ev.get("legion_ts"),
                                   "error": _clip(_result_text(c.get("content")), 500)})
        elif t == "result":
            results.append({k: ev.get(k) for k in ("subtype", "is_error", "num_turns",
                                                   "duration_ms", "duration_api_ms",
                                                   "total_cost_usd", "usage") if k in ev})
        if ts is not None:
            prev_ts = ts

    def top(d: dict[str, int], n: int, at_least: int) -> list[dict]:
        return [{"value": _clip(k, 300), "count": v}
                for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if v >= at_least][:n]

    return {
        "agent": init,
        "wall_seconds": round(last - first, 1) if first is not None and last is not None else None,
        "results": results[-5:],
        "result_count": len(results),
        "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1]["calls"])),
        "tool_seconds": round(sum(s["seconds"] for s in slow), 1),
        "slowest_tool_calls": sorted(slow, key=lambda s: -s["seconds"])[:15],
        "longest_model_waits": sorted(thinking, key=lambda s: -s["seconds"])[:10],
        "repeated_commands": top(commands, 20, 2),
        "repeated_reads": top(reads, 20, 3),
        "tool_errors": errors[:30],
        "tool_error_count": len(errors),
        "subagent_events": subagent_events,
        "sandbox_lines": sandbox_lines,
    }
