"""Run lifecycle: failure, timers, logs, sandbox deletion, acceptance of reported runs."""
from __future__ import annotations

import itertools
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ....sandbox import GatewaySandbox, agentlog
from ....util import now_iso
from ... import cards
from ...model import (
    LIVE_RUN,
    R_ACCEPTED,
    R_FAILED,
    R_REPORTED,
    R_SUPERSEDED,
    FailureSource,
    Node,
    Run,
    RunFailure,
)
from ...results import Escalate
from ...roles import handler_for
from ..base.auth import open_task
from ..base.common import _parse_ts
from ..base.core import ChangeCore

if TYPE_CHECKING:
    from ..sandboxes.sandboxes import Sandboxes


def log_part(run_id: str, n: int) -> str:
    return f"control/logs/{run_id}.log" if n == 1 else f"control/logs/{run_id}.{n}.log"


def split_lines(text: str, limit: int) -> list[str]:
    """``text`` in parts of at most ``limit`` UTF-8 bytes, split after a newline where possible."""
    parts, cur, size = [], [], 0
    for line in text.splitlines(keepends=True):
        b = len(line.encode())
        while b > limit:                         # one huge line: cut it by characters
            head = line[:limit // 4]
            if cur:
                parts.append("".join(cur))
                cur, size = [], 0
            parts.append(head)
            line = line[len(head):]
            b = len(line.encode())
        if size + b > limit and cur:
            parts.append("".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += b
    if cur:
        parts.append("".join(cur))
    return parts or [""]


class RunLifecycle:
    """What happens to runs after dispatch: failure (which escalates – no automatic retries),
    timeouts, the agent log, sandbox deletion once work is accepted."""

    LOG_LIMIT = 64 * 1024 * 1024      # a larger log keeps its first and last 32 MiB
    PART_BYTES = 1024 * 1024         # per stored document (the store's limit is 2 MiB)

    def __init__(self, core: ChangeCore, sandboxes: Sandboxes):
        self.c, self.sandboxes = core, sandboxes
        self._ended: set[str] = set()        # released because the change ended (tried once)

    def adopt_sandboxes(self, found: dict[str, GatewaySandbox], complete: bool) -> None:
        """On start: the runs' sandboxes as the gateway lists them (:meth:`Sandboxes.adopt`)."""
        self.sandboxes.adopt(found, complete)

    # an agent still in its turn this long after its report was accepted is stopped
    END_TURN_GRACE_S = 300

    def time_limit(self, run: Run) -> int:
        """A run's time limit: an answer to an A2A message (a consult; not the resolver's,
        which investigates) ``a2a_answer_timeout_s``, any other run ``run_timeout_s``."""
        lim = self.c.cfg.limits
        return (lim.a2a_answer_timeout_s if run.consult and run.role != cards.RESOLVER
                else lim.run_timeout_s)

    def deadline(self, run: Run) -> float | None:
        """Seconds until the run's agent is stopped (its timer): the run timeout while it is
        live, the end-of-turn grace once its report was accepted; None: no limit applies."""
        now = datetime.now(UTC)
        if run.status in LIVE_RUN or open_task(run):     # working, or yet to end its task
            due = self.time_limit(run) - (now - _parse_ts(run.created_at)).total_seconds()
        elif run.status == R_REPORTED and (run.reply or run.ended_at):
            since = run.reply.at if run.reply else run.ended_at
            due = self.END_TURN_GRACE_S - (now - _parse_ts(since)).total_seconds()
        else:
            return 0.0                       # failed or superseded: its agent stops now
        return max(due, 0.0)

    def stop_if_due(self, run: Run) -> bool:
        """The run's timer is up (under the lock): a live run times out (and fails); an agent
        that has not ended its turn after its accepted report is stopped (its exit concludes the
        run); any other is stopped. Returns whether it stopped it."""
        proc = self.c.procs.get(run.id)
        if proc is None or proc.returncode is not None or (self.deadline(run) or 0) > 0:
            return False
        if run.status in LIVE_RUN:
            limit = self.time_limit(run)
            self.fail_run(run, f"timed out after {limit}s", FailureSource.TIMEOUT)
        elif run.status == R_REPORTED:
            self.c.event("agent", f"the agent did not end its turn {self.END_TURN_GRACE_S}s after "
                         "its accepted report: stopped", run.node_id, run.id)
        proc.kill()
        return True

    def release_sandboxes(self) -> bool:
        """Delete kept sandboxes whose hold has ended (their gate passed, or the change ended)
        once no run uses them. Returns whether anything changed."""
        changed = False
        for node in self.c.nodes.values():
            sb = node.sandbox
            if not sb or self.sandboxes.holds(node) or any(
                    r.sandbox == sb and r.status in LIVE_RUN for r in self.c.runs.values()):
                continue
            stopped = node.sandbox_stopped
            node.sandbox, node.sandbox_stopped, changed = None, False, True
            if any(r.sandbox == sb and not r.sandbox_deleted for r in self.c.runs.values()):
                self.c.event("sandbox", f"releasing {sb}: {node.id} is not reworked any more",
                             node.id)
                self.sandboxes.delete_later(sb, stopped)
        if self.c.change.status != "ACTIVE":     # the change ended: no sandbox is needed any more
            for r in self.c.runs.values():
                sb = r.sandbox
                if (sb and not r.sandbox_deleted and sb not in self.sandboxes.retiring
                        and sb not in self._ended and not any(x.sandbox == sb and x.status in LIVE_RUN
                                    for x in self.c.runs.values())):
                    self.c.event("sandbox", f"releasing {sb}: the change has ended",
                                 r.node_id, r.id)
                    self._ended.add(sb)
                    self.sandboxes.delete_later(sb)
                    changed = True
        return changed

    def _consulted(self, node: Node, roles: set[str] | None = None) -> set[str]:
        """Sandboxes of the node's agents started early (A2A consults) that no run of the
        node took over: they go with the step."""
        taken = {self.c.runs[r].sandbox for r in node.runs if r in self.c.runs}
        return {r.sandbox for r in self.c.runs.values()
                if r.consult and r.node_id == node.id and (not roles or r.role in roles)
                and r.sandbox and not r.sandbox_deleted and r.sandbox not in taken}

    def retire_runs(self, node: Node) -> None:
        """The node starts again with a new run in a new sandbox (an escalation resumed): its
        finished runs are superseded and their sandboxes deleted (after their agents reflect),
        except the one the node holds for rework."""
        retired: set[str] = set()
        for rid in node.runs:
            r = self.c.runs[rid]
            if r.status == R_REPORTED:
                r.status = R_SUPERSEDED
            sb = r.sandbox
            if (not sb or sb in retired or sb in self.sandboxes.retiring or r.sandbox_deleted
                    or sb == node.sandbox
                    or any(x.sandbox == sb and x.status in LIVE_RUN for x in self.c.runs.values())):
                continue
            retired.add(sb)
            self.c.event("sandbox", f"releasing {sb}: {node.id} starts again", node.id, rid)
            self.sandboxes.delete_later(sb)

    def fail_run(self, run: Run, reason: str, source: FailureSource,
                 exit_code: int | None = None) -> None:
        """No automatic retries (principle 5): a failed run escalates to a human – its agent is
        stopped, and the run's work returns :meth:`escalation` to its phase. ``source`` says
        whether the agent failed or what it runs on (see :class:`FailureSource`)."""
        run.status = R_FAILED
        run.ended_at = now_iso()
        run.failure = RunFailure(source=source, stage=run.stage, reason=reason, exit_code=exit_code)
        node = self.c.nodes.get(run.node_id)
        where = f" while {run.stage.lower()}" if run.stage and run.stage != "EXITED" else ""
        self.c.event("run_failed", f"{source.lower()} error{where}: {reason}", run.node_id, run.id)
        proc = self.c.procs.get(run.id)
        if proc is not None and proc.returncode is None:
            proc.kill()                      # its exit ends the run's work (AgentRuns.launch)
        if node is None:
            return
        if not self.c.cfg.sandbox.keep_failed and run.sandbox and run.role != "implementer":
            # a failed run's sandbox is not kept for rework either: the retry starts afresh
            if node.sandbox == run.sandbox:
                node.sandbox, node.sandbox_stopped = None, False
            self.c.spawn(self.sandboxes.delete(run.sandbox))

    def escalation(self, run: Run) -> Escalate:
        """What a failed run means: a human decides (no automatic retries); where the step
        resumes is the step's to say."""
        f = run.failure
        source = f.source if f else FailureSource.SYSTEM
        where = f" while {f.stage.lower()}" if f and f.stage and f.stage != "EXITED" else ""
        who = f"{run.role} run {run.id}" if handler_for(run.role).panel else f"run {run.id}"
        return Escalate("run_failed", f"{who} ({source.lower()} error{where}): "
                        f"{f.reason if f else 'failed'}", None, log_tail=self.log_tail(run.id, 40),
                        run=run.id, source=source)     # where it resumes: its step's decision

    def archive_log(self, run: Run) -> None:
        """Move the whole agent log into the store (agents cannot read control/logs/), in parts
        below the store's document size (``<run>.log``, ``<run>.2.log``, …), and record its
        digest. A log over ``LOG_LIMIT`` keeps its first and last halves."""
        text = self.store_log(run.id)
        if text is None:
            return
        run.session_id = agentlog.session_id(text) or run.session_id
        if run.resumed_session and run.session_id and run.session_id != run.resumed_session:
            self.c.event("agent", f"run {run.id} was to continue session {run.resumed_session} "
                                  f"but ran in {run.session_id}: it started without that "
                                  "session's history", run.node_id, run.id)
        self.c.write_record(f"control/digests/{run.id}.json", agentlog.digest(text))

    def store_log(self, log_id: str) -> str | None:
        """Move a live log (an agent's, a verification's) into the store, as :meth:`archive_log`
        describes; its text (None: there was none)."""
        f = self.c.run_dir(log_id) / "agent.log"
        if not f.exists():
            return None
        data = f.read_bytes()
        if len(data) > self.LOG_LIMIT:
            half = self.LOG_LIMIT // 2
            data = (data[:half] + f"\n[legion: {len(data) - 2 * half} bytes omitted]\n".encode()
                    + data[-half:])
        text = data.decode(errors="replace")
        for n, part in enumerate(split_lines(text, self.PART_BYTES), start=1):
            self.c.write_record(log_part(log_id, n), part)
        shutil.rmtree(self.c.run_dir(log_id), ignore_errors=True)
        return text

    # ------------------------------------------------------------------ A2A (sandboxes/answer.py)
    def agent_state(self, node_id: str, role: str) -> str | None:
        """A step agent as an A2A recipient: "live" (a run is in its turn: deliver to its
        inbox), "idle" (its sandbox exists, stopped or not: a resumed turn answers), "absent"
        (its step is in progress but its agent has not started: a consult starts it) or None
        (no card: the step is not in progress)."""
        answerer = self.sandboxes.answerer
        has_card = any(c.node == node_id and c.role == role for c in self.c.a2a_cards())
        if answerer.session_run(node_id, role) is None:
            return "absent" if has_card else None
        return "live" if answerer.live(node_id, role) else "idle"

    def stored_log(self, run_id: str) -> str:
        """A run's whole archived log ('' if none)."""
        parts = []
        for n in itertools.count(1):
            if not self.c.latest(log_part(run_id, n)):
                break
            parts.append(self.c.doc(log_part(run_id, n)).content)
        return "".join(parts)

    def log_tail(self, run_id: str, lines: int = 200) -> str:
        p = self.c.run_dir(run_id) / "agent.log"
        if p.exists():
            text = p.read_bytes()[-200_000:].decode(errors="replace")
        else:
            n = 1
            while self.c.latest(log_part(run_id, n + 1)):
                n += 1
            if not self.c.latest(log_part(run_id, n)):
                return ""
            text = (self.c.doc(log_part(run_id, n - 1)).content if n > 1 else "") \
                + self.c.doc(log_part(run_id, n)).content
        return "\n".join(text.splitlines()[-lines:])

    def accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        """Mark the node's reported runs ACCEPTED and delete their sandboxes (principle 8),
        except one a node still holds for rework (see :meth:`.Sandboxes.holds`)."""
        sandboxes = set()
        for rid in node.runs:
            r = self.c.runs[rid]
            if roles and r.role not in roles:
                continue
            if r.status == R_REPORTED:
                r.status = R_ACCEPTED
            if r.sandbox and not r.sandbox_deleted and r.status in (R_ACCEPTED, R_SUPERSEDED, R_FAILED):
                sandboxes.add(r.sandbox)
        sandboxes |= self._consulted(node, roles)
        held = self.sandboxes.held()
        for sb in sandboxes - held - self.sandboxes.retiring:     # retiring already: a judge's
            if any(r.sandbox == sb and r.status in LIVE_RUN for r in self.c.runs.values()):
                continue
            stopped = False
            for n in self.c.nodes.values():
                if n.sandbox == sb:
                    stopped = stopped or n.sandbox_stopped
                    n.sandbox, n.sandbox_stopped = None, False
            self.sandboxes.delete_later(sb, stopped)
