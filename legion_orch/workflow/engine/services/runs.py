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
    FailureSource,
    Run,
    RunFailure,
)
from ...tickets import Ticket
from ..base.common import WorkflowError, _parse_ts
from ..base.core import ChangeCore
from ..sandboxes.plan import CLOSED, held

if TYPE_CHECKING:
    from ..sandboxes.sandboxes import Sandboxes


def log_part(run_id: str, n: int) -> str:
    """A run's archived log, in parts: audit, with its other documents (``control/runs/<run>/``)."""
    return f"control/runs/{run_id}/log" if n == 1 else f"control/runs/{run_id}/log.{n}"


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


def model_matches(asked: str, served: str) -> bool:
    """Whether the model that served a run is the one it asked for: the same name, or an alias
    of it (``opus`` is served as ``claude-opus-…``)."""
    asked, served = (m.lower().split("[")[0] for m in (asked, served))   # opus[1m]: opus
    return asked == served or asked in served.split("-") or served.startswith(asked)


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
        if run.status in LIVE_RUN:                       # working
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
                         "its accepted report: stopped", run.ticket, run.id)
        proc.kill()
        return True

    def release_sandboxes(self) -> bool:
        """Delete the sandboxes of closed tickets (passed, failed, cancelled: nothing reworks
        there any more), a judge's once its judgement is in (its run concluded, nothing waiting
        in its inbox – every round's judge is a fresh one), and – the change ended – every
        sandbox, once no run uses them. Returns whether anything changed."""
        changed = False
        live = {r.sandbox for r in self.c.runs.values() if r.status in LIVE_RUN}
        in_turn = {r.sandbox for r in self.c.runs.values()
                   if r.status == R_REPORTED and not r.concluded}
        for t in self.c.tickets.t.values():
            if t.role == "criterion" and t.status not in CLOSED:
                changed |= self._release_judges(t, live | in_turn)
                continue
            if held(t, self.c.tickets.t):
                continue
            mine = {self.c.runs[r].sandbox for r in t.runs if r in self.c.runs} | (
                {t.sandbox} if t.sandbox else set())
            if t.sandbox and t.sandbox not in live:
                t.sandbox, changed = None, True
            for sb in mine - live - self.sandboxes.retiring - self._ended - {None}:
                if any(r.sandbox == sb and not r.sandbox_deleted for r in self.c.runs.values()) \
                        and not any(held(x, self.c.tickets.t) and x.sandbox == sb
                                    for x in self.c.tickets.t.values()):
                    self.c.event("sandbox", f"releasing {sb}: {t.id} is closed", t.id)
                    self._ended.add(sb)
                    self.sandboxes.delete_later(sb, self._stopped(sb))
                    changed = True
        if self.c.change.status != "ACTIVE":     # the change ended: no sandbox is needed any more
            for r in self.c.runs.values():
                sb = r.sandbox
                if (sb and not r.sandbox_deleted and sb not in self.sandboxes.retiring
                        and sb not in self._ended and sb not in live):
                    self.c.event("sandbox", f"releasing {sb}: the change has ended",
                                 r.ticket, r.id)
                    self._ended.add(sb)
                    self.sandboxes.delete_later(sb)
                    changed = True
        return changed

    def _release_judges(self, t: Ticket, busy: set[str | None]) -> bool:
        """A criterion's judges whose judgement is in: their sandboxes go (after nothing waits
        in the judge's inbox – what does is answered in a turn of its own first)."""
        changed = False
        for rid in t.runs:
            r = self.c.runs.get(rid)
            if r is None or not r.sandbox or r.sandbox_deleted or r.sandbox in busy \
                    or r.sandbox in self.sandboxes.retiring or r.sandbox in self._ended:
                continue
            if self._waiting(cards.card_id(r.role, t.id)):
                continue
            self.c.event("sandbox", f"releasing {r.sandbox}: its judgement is in", t.id, r.id)
            self._ended.add(r.sandbox)
            self.sandboxes.delete_later(r.sandbox, self._stopped(r.sandbox))
            changed = True
        return changed

    def _waiting(self, card: str) -> bool:
        """Whether anything waits in a card's inbox (no A2A listener: nothing can)."""
        try:
            return bool(self.c.host.inbox_messages(self.c.cid, card))
        except WorkflowError:
            return False

    def _stopped(self, sandbox: str) -> bool:
        """Whether a sandbox is stopped (the gateway's word, on the runs in it)."""
        return any(r.sandbox == sandbox and r.sandbox_stopped for r in self.c.runs.values())

    def fail_run(self, run: Run, reason: str, source: FailureSource,
                 exit_code: int | None = None) -> None:
        """A failed run's agent is stopped; how the run ended goes to its ticket (its work's
        outcome: an agent's failure is an attempt, the system's is retried, a timeout exceeds
        the budget). ``source`` says whether the agent failed or what it runs on (see
        :class:`FailureSource`)."""
        run.status = R_FAILED
        run.ended_at = now_iso()
        run.failure = RunFailure(source=source, stage=run.stage, reason=reason, exit_code=exit_code)
        ticket = self.c.ticket(run.ticket)
        where = f" while {run.stage.lower()}" if run.stage and run.stage != "EXITED" else ""
        self.c.event("run_failed", f"{source.lower()} error{where}: {reason}", run.ticket, run.id)
        proc = self.c.procs.get(run.id)
        if proc is not None and proc.returncode is None:
            proc.kill()                      # its exit ends the run's work (AgentRuns.work)
        if ticket is None:
            return
        if ticket.sandbox == run.sandbox and run.sandbox_ref is None and not any(
                r.sandbox == run.sandbox and r.sandbox_ref for r in self.c.runs.values()):
            ticket.sandbox = None            # never created: the next round creates its own
            return
        if not self.c.cfg.sandbox.keep_failed and run.sandbox and run.role != "implementer":
            # a failed run's sandbox is not kept for the next round either: it starts afresh
            if ticket.sandbox == run.sandbox:
                ticket.sandbox = None
            self.c.spawn(self.sandboxes.delete(run.sandbox))

    def why_failed(self, run: Run) -> str:
        """A failed run, in words: who failed, how far it had got, why – and its log's end."""
        f = run.failure
        source = f.source if f else FailureSource.SYSTEM
        where = f" while {f.stage.lower()}" if f and f.stage and f.stage != "EXITED" else ""
        tail = self.log_tail(run.id, 40)
        return (f"{run.role} run {run.id} ({source.lower()} error{where}): "
                f"{f.reason if f else 'failed'}" + (f"\n\nlog tail:\n{tail}" if tail else ""))

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
                                  "session's history", run.ticket, run.id)
        digest = agentlog.digest(text)
        self.c.write_record(f"control/digests/{run.id}.json", digest)
        served = (digest.get("agent") or {}).get("model")
        if served:
            run.model_served = served
            if run.model and not model_matches(run.model, served):
                self.c.event("agent", f"run {run.id} asked for model {run.model} but was served "
                                      f"by {served}", run.ticket, run.id)

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
    def agent_state(self, ticket_id: str, role: str) -> str | None:
        """A ticket's agent as an A2A recipient: "live" (a run is in its turn: deliver to its
        inbox), "idle" (its sandbox exists, stopped or not: a resumed turn answers), or None (it
        has not started, or its sandboxes are gone: nothing answers)."""
        answerer = self.sandboxes.answerer
        if answerer.session_run(ticket_id, role) is None:
            return None
        return "live" if answerer.live(ticket_id, role) else "idle"

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

    def accept_runs(self, ticket: Ticket) -> None:
        """A ticket's reported runs are accepted (its work's outcome went to it); their
        sandboxes go once the ticket is closed (:meth:`release_sandboxes`)."""
        for rid in ticket.runs:
            if (r := self.c.runs.get(rid)) is not None and r.status == R_REPORTED:
                r.status = R_ACCEPTED

