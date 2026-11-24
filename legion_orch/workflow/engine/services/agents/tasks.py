"""A run's work task: the orchestrator's A2A task to the agent – the workflow's side of the
agent's inbox (the inbox itself – messages, questions back – is the A2A layer's:
``server/inbox.py``).

A run is an A2A task: the orchestrator's, on the agent's card, in the agent's one inbox.

* Its id is the run's; its first message is the rendered assignment (``Run.instructions``, which
  the prompt pin covers); a rework is a new run, so a new task in the same context (the step).
* An accepted ``report(...)`` records the work – documents, attestations, structured data – and
  **ends the task** with it: ``completed`` for DONE, ``input-required`` for BLOCKED (its ticket
  waits on a human). The agent may still answer what waits in its inbox until its turn ends,
  and gives up with ``a2a_reply(task_id, text, "failed")`` (the run fails: an attempt).
* A turn that ends with no accepted report is followed by another: the orchestrator **asks
  again** – the next message on the task says what is missing. Each ask-again uses one report
  attempt (G-R6): when none is left the run fails.
"""
from __future__ import annotations

from .....util import now_iso
from ....model import LIVE_RUN, R_FAILED, R_REPORTED, FailureSource, Run, TaskReply
from ...base.core import ChangeCore
from ..runs import RunLifecycle

STATES = ("completed", "input-required", "failed", "rejected")


class RunTasks:
    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle):
        self.c, self.lifecycle = core, lifecycle

    # ------------------------------------------------------------------ the record's state
    @staticmethod
    def wanted(run: Run) -> str | None:
        """The reply the run's record asks for: completed (an accepted DONE report),
        input-required (an accepted BLOCKED one), None (nothing accepted yet)."""
        if run.status != R_REPORTED or run.report is None:
            return None
        return "input-required" if str(run.report.status) == "BLOCKED" else "completed"

    @classmethod
    def ended(cls, run: Run) -> bool:
        """The task is over: replied as the record asks, or the run failed."""
        if run.status == R_FAILED:
            return True
        return run.reply is not None and run.reply.state == cls.wanted(run)

    def state(self, run: Run) -> str:
        if run.status == R_FAILED:
            return "failed"
        if self.ended(run):
            return run.reply.state
        return "working"

    # ------------------------------------------------------------------ the agent replies
    def reply(self, run: Run, state: str, text: str) -> dict:
        """Under the lock: the agent's ``a2a_reply`` on its run's task. ``failed`` (or
        ``rejected``) gives up: the run fails. Any other is answered: the task was ended by its
        accepted report already, or – none yet – with what is missing (an ask-again)."""
        if state not in STATES:
            return {"ok": False, "error": f"state must be one of {', '.join(STATES)}"}
        if self.ended(run):
            return {"ok": True, "state": self.state(run), "note": "the task was ended already"}
        if state in ("failed", "rejected"):
            run.reply = TaskReply(state=state, text=text.strip())
            self.c.event("agent", f"the agent ended its task {state}: {text.strip()[:300]}",
                         run.ticket, run.id)
            self.lifecycle.fail_run(run, f"the agent gave up ({state}): {text.strip()[:500]}",
                                    FailureSource.AGENT)
            self.c.save()
            return {"ok": True, "state": "failed"}
        msg = self.ask_again(run, f"You replied {state}, but")
        self.c.save()
        return {"ok": False, "state": self.state(run), "message": msg}

    def ask_again(self, run: Run, opening: str | None = None) -> str | None:
        """Under the lock: the orchestrator's next message on the task – what is missing – using
        one report attempt (None, and the run failed: none is left)."""
        msg = self._missing(run, opening)
        limit = self.c.cfg.limits.max_report_attempts
        if run.attempts >= limit:
            if run.status in LIVE_RUN:
                self.lifecycle.fail_run(run, f"the task was not ended after {limit} attempts: "
                                             + msg, FailureSource.AGENT)
            return None
        run.attempts += 1
        run.asked.append(msg)
        self.c.event("agent", f"asked again ({run.attempts}/{limit}): {msg[:200]}",
                     run.ticket, run.id)
        return msg

    def _missing(self, run: Run, opening: str | None) -> str:
        wanted = self.wanted(run)
        said = f"{opening} " if opening else "Your task is still open: "
        if wanted is None:
            last = "; ".join(run.violations[-1]) if run.violations else ""
            return (said + "this run has no accepted report. File your results with "
                    "report(...) (status DONE, or BLOCKED with blocked_reason and "
                    "missing_decision)" + (f" – your last report was rejected: {last[:800]}"
                                           if last else "") +
                    "; accepted, it ends the task.")
        return (said + f"your accepted report is {run.report.status}: it ended the task.")

    # ------------------------------------------------------------------ the agent's view
    def view(self, run: Run) -> dict:
        """The task as the agent's inbox shows it."""
        return {"task_id": run.id, "from": "orchestrator", "kind": "work",
                "state": self.state(run), "attempt": run.attempt,
                "messages": [{"from": "orchestrator", "text": run.instructions}]
                + [{"from": "orchestrator", "text": m} for m in run.asked]
                + ([{"from": "you", "text": run.reply.text, "state": run.reply.state}]
                   if run.reply else []),
                "how_to_end": "report(...) records your results – accepted, it ends this task –; "
                              "then end your turn. a2a_reply(task_id, text, 'failed') gives up",
                "at": run.created_at, "now": now_iso()}
