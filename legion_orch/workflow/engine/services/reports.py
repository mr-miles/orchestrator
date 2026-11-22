"""Agent writes and reports: write guards (G-W1, G-W4), report guards and transitions."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ....store import DocCheck, DocVersion
from ....util import now_iso
from ... import attest, guards, report_data
from ...assets import GitMode
from ...model import (
    LIVE_RUN,
    R_ACTIVE,
    R_REPORTED,
    R_STARTING,
    FailureSource,
    Node,
    Progress,
    ReportStatus,
    Run,
    RunReport,
    RunStage,
)
from ...results import Escalate, PhaseResult, ReportOutcome
from ...roles import Report, handler_for
from ...view import RoleContext
from ..base.common import WorkflowError, log
from ..base.core import ChangeCore
from .runs import RunLifecycle


@dataclass
class _Submitted:
    """The arguments of one ``report`` call."""
    status: str
    summary: str
    attestations: list[str]
    data: dict
    blocked_reason: str | None
    missing_decision: str | None


@dataclass
class _Admitted:
    """A report that passed phase 1, carried through the checks to the apply phase."""
    run: Run
    node: Node
    attempt: int                 # run.attempts when admitted; a change means it was superseded
    r: _Submitted
    violations: list[str] = field(default_factory=list)


END_TURN = ("Accepted. End your turn now: the controller takes your work when your turn ends, "
            "and nothing you do after this counts.")


class Reports:
    """Everything an agent sends: document writes, progress and the final report. Role-specific
    guards and transitions are the role handlers' (workflow/roles/), given ``roles``."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, roles: RoleContext):
        self.c, self.lifecycle, self.roles = core, lifecycle, roles
        # run -> its checked report, from its acceptance until the agent's turn ends
        self._accepted: dict[str, Report | None] = {}
        self._report_locks: dict[str, asyncio.Lock] = {}     # serialise reports of one run

    def docs_write(self, run: Run, path: str, content: str, base_version: int | None,
                   dry_run: bool = False) -> DocVersion | DocCheck:
        """Guards G-W1, G-W4 (then the store's G-W2, G-W3). ``path`` may be relative to the
        change namespace. ``dry_run``: every check, nothing written or counted."""
        path = self.c.p(path)
        rel = path[len(self.c.p("")):]
        controller_owned = rel.startswith("control/") or (
            rel.startswith("tasks/") and (rel.split("/")[2:3] in (["verification.json"], ["feedback"],
                                                                  ["decisions"])))
        if controller_owned or not any(path.startswith(w) for w in run.write_scope):
            raise WorkflowError(f"G-W1: {path} is outside your write scope {run.write_scope}")
        lim, size = self.c.cfg.limits, len(content.encode())
        if run.docs_written + 1 > lim.max_docs_per_run:
            raise WorkflowError(f"G-W4: write quota exhausted: this run has written "
                                f"{run.docs_written} document versions (limit {lim.max_docs_per_run})")
        if run.doc_bytes + size > lim.max_doc_bytes_per_run:
            raise WorkflowError(f"G-W4: write quota exhausted: this run has written {run.doc_bytes} "
                                f"bytes; this write needs {size} more (limit {lim.max_doc_bytes_per_run})")
        if dry_run:
            return self.c.docs.check(path, content, base_version=base_version)
        v = self.c.docs.write(path, content, author=run.id, role=run.role, base_version=base_version)
        if v.version == base_version:            # identical to the latest: no new version
            if v.author != run.id and v.attestation not in run.confirmed_docs:
                run.confirmed_docs.append(v.attestation)
            self.c.event("doc", f"{v.attestation} unchanged", run.node_id, run.id)
            return v
        run.docs_written += 1
        run.doc_bytes += size
        self.c.event("doc", f"{v.attestation} written", run.node_id, run.id)
        return v

    async def agent_write(self, run: Run, path: str, content: str, base_version: int | None,
                          dry_run: bool = False) -> DocVersion | DocCheck:
        """An agent's ``docs_write``: the guards above, then the store's own (G-W2, G-W3); the
        run's quota counters are saved with it. A dry run only checks."""
        async with self.c.lock:
            v = self.docs_write(run, path, content, base_version, dry_run)
            if not dry_run:
                self.c.save()
            return v

    def connected(self, run: Run) -> None:
        """Any MCP call from the run's sandbox: the agent is up and reaches the controller.
        (In memory like progress; the next save persists it.)"""
        if run.status in LIVE_RUN and run.stage != RunStage.RUNNING:
            run.stage = RunStage.RUNNING
            if run.status == R_STARTING:
                run.status = R_ACTIVE
            self.c.event("sandbox", f"agent connected from sandbox {run.sandbox}", run.node_id, run.id)

    def progress(self, run: Run, message: str, percent: float | None) -> None:
        run.progress.append(Progress(message=message[:500], percent=percent))
        run.progress = run.progress[-50:]
        if run.status == R_STARTING:
            run.status = R_ACTIVE
        self.c.host.touch()

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._report_locks.setdefault(run_id, asyncio.Lock())

    async def handle_report(self, run_id: str, status: str, summary: str, attestations: list[str],
                            data: dict | None, blocked_reason: str | None,
                            missing_decision: str | None, dry_run: bool = False) -> ReportOutcome:
        """Three phases. (1) :meth:`_admit_report`, under the controller lock: bookkeeping and
        cheap checks. (2) :meth:`_check_report`, without it: attestation resolution, git checks
        and role guards, which can be slow – reports of the same run are serialised by a per-run
        lock and the checks are time-limited. (3) :meth:`_apply_report`, under the lock again:
        re-check the run is unchanged, then apply the transition transactionally.

        ``dry_run``: the same checks, but no attempt is counted (G-R6) and nothing is recorded or
        applied; the outcome says whether the report would be accepted."""
        async with self._run_lock(run_id):
            async with self.c.lock:
                sub = self._admit_report(run_id, _Submitted(
                    status=status, summary=summary, attestations=attestations, data=data or {},
                    blocked_reason=blocked_reason, missing_decision=missing_decision), dry_run)
            if isinstance(sub, ReportOutcome):
                return sub
            report = await self._check_report(sub) if not sub.violations else None
            if dry_run:
                return self._dry_run_outcome(sub)
            async with self.c.lock:
                return await self._apply_report(sub, report)

    def _dry_run_outcome(self, sub: _Admitted) -> ReportOutcome:
        run, v = sub.run, sub.violations
        left = self.c.cfg.limits.max_report_attempts - run.attempts
        self.c.event("report_checked", f"dry run: {len(v)} violation(s)"
                     + (": " + "; ".join(v)[:600] if v else ""), sub.node.id, run.id)
        return ReportOutcome(accepted=False, dry_run=True, would_accept=not v, violations=v,
                             attempts_left=max(left, 0))

    def _admit_report(self, run_id: str, r: _Submitted,
                      dry_run: bool = False) -> _Admitted | ReportOutcome:
        """Phase 1 (lock held): count the attempt (not for a dry run), check limits and shape,
        normalise the attestations (here and in review findings) to fully qualified form."""
        run = self.c.runs[run_id]
        if run.consult:
            return ReportOutcome.reject("this turn only answers an A2A message: your answer is "
                                        "your final message; your work on the step comes later")
        if run.status not in LIVE_RUN:
            return ReportOutcome.reject(f"run is {run.status}")
        if not dry_run:
            run.attempts += 1
        sub = _Admitted(run=run, node=self.c.nodes[run.node_id], attempt=run.attempts, r=r)
        sub.violations = guards.report_limits(r.summary, r.attestations, r.data, r.blocked_reason,
                                              r.missing_decision)
        if not sub.violations:
            r.attestations = [attest.normalize(a, self.c.cid) for a in r.attestations]
            for f in r.data.get("findings") or []:
                if isinstance(f, dict) and isinstance(f.get("attestations"), list):
                    f["attestations"] = [attest.normalize(a, self.c.cid)
                                         for a in f["attestations"] if isinstance(a, str)]
            sub.violations = guards.report_shape(r.status, r.blocked_reason, r.missing_decision)
            if r.status == ReportStatus.DONE:   # G-R7: structured data matches the report schema
                schema = self.c.assets.roles[run.role].report_schema
                sub.violations += [f"G-R7: data does not match schema {schema!r}: {x}"
                                   for x in self.c.assets.schemas.validate_data(schema, r.data)]
        if not dry_run:
            self.c.save()
        return sub

    async def _check_report(self, sub: _Admitted) -> Report | None:
        """Phase 2 (lock not held, time-limited): attestations resolve; for DONE, the role's
        git and document guards. Adds to ``sub.violations``; returns the typed report a DONE
        transition needs."""
        timeout = self.c.cfg.limits.report_check_timeout_s
        try:
            async with asyncio.timeout(timeout):
                parsed, v = await attest.check_all(sub.r.attestations, [self.c.workspace],
                                                   self.c.docs, self.c.git)
                sub.violations += v
                if sub.violations or sub.r.status != ReportStatus.DONE:
                    return None
                v, report = await self._role_guards(sub.run, sub.node, parsed, sub.r.data)
                sub.violations += v
                return report
        except TimeoutError:
            sub.violations.append(f"G-R8: report checks exceeded {timeout}s")
            return None

    async def _apply_report(self, sub: _Admitted, report: Report | None) -> ReportOutcome:
        """Phase 3 (lock held): reject, or record the report and run the transition. Any
        exception restores the state and escalates – never a half-applied transition."""
        run, node, r = sub.run, sub.node, sub.r
        if run.status not in LIVE_RUN or run.attempts != sub.attempt:
            return ReportOutcome.reject(f"run changed during checks ({run.status})")
        if sub.violations:
            return self._reject_report(run, node, sub.violations)
        try:
            with self.c.st.transaction():
                run.status = R_REPORTED
                run.ended_at = now_iso()
                run.report = RunReport(status=r.status, summary=r.summary,
                                       attestations=r.attestations, data=r.data,
                                       blocked_reason=r.blocked_reason,
                                       missing_decision=r.missing_decision, at=run.ended_at)
                self.c.event("report", f"{run.role} reported {r.status}: {r.summary[:200]}",
                             node.id, run.id)
                # what it means is decided when the agent's turn ends (conclude)
                self._accepted[run.id] = report
                self.c.save()
        except Exception as e:
            log.exception("applying report of %s failed", run.id)
            self.lifecycle.fail_run(run, f"controller error while applying the report: {e!r} "
                                    "(external effects such as git merges may already exist)",
                                    FailureSource.CONTROLLER)
            with contextlib.suppress(Exception):
                self.c.save()
            return ReportOutcome.reject("controller error while applying the report; escalated")
        return ReportOutcome(accepted=True, next=node.status, instruction=END_TURN)

    async def conclude(self, run: Run) -> PhaseResult | None:
        """The agent's turn ended (the lock is held). After an accepted report, its role handler
        judges what it means – a BLOCKED report escalates; the result goes back with the run's
        work to its phase. (Without one, the run already failed.) Any exception restores the
        state and fails the run."""
        node = self.c.nodes.get(run.node_id)
        if run.status != R_REPORTED or run.concluded or node is None or run.report is None:
            return None
        try:
            with self.c.st.transaction():
                if run.report.status == ReportStatus.BLOCKED:
                    return Escalate(
                        "blocked", run.report.blocked_reason or "", None,  # the step resumes it
                        run=run.id, missing_decision=run.report.missing_decision)
                # what the role's report means (see workflow/roles/)
                report = self._accepted.pop(run.id, None) or await self._recheck(run, node)
                return await handler_for(run.role).on_done(self.roles, report)
        except Exception as e:
            log.exception("concluding run %s failed", run.id)
            self.lifecycle.fail_run(run, f"controller error while applying the report: {e!r} "
                                    "(external effects such as git merges may already exist)",
                                    FailureSource.CONTROLLER)
            return self.lifecycle.escalation(run)

    async def _recheck(self, run: Run, node: Node) -> Report:
        """The typed report again, from the run's record (the controller restarted since it
        was accepted); raises if it no longer holds."""
        parsed, v = await attest.check_all(run.report.attestations, [self.c.workspace],
                                           self.c.docs, self.c.git)
        if not v:
            v, report = await self._role_guards(run, node, parsed, run.report.data)
        if v or report is None:
            raise ValueError("the accepted report no longer holds: " + "; ".join(v))
        return report

    def _reject_report(self, run: Run, node: Node, v: list[str]) -> ReportOutcome:
        run.violations.append(v)
        left = self.c.cfg.limits.max_report_attempts - run.attempts
        self.c.event("report_rejected", f"{len(v)} violation(s): " + "; ".join(v)[:600],
                     node.id, run.id)
        if left <= 0:
            self.lifecycle.fail_run(run, "G-R6: too many rejected reports", FailureSource.AGENT)
        self.c.save()
        return ReportOutcome.reject(*v, attempts_left=max(left, 0))

    async def _role_guards(self, run: Run, node: Node, parsed: list[attest.Attestation],
                           data: dict) -> tuple[list[str], Report | None]:
        """Required documents and git (G-R4/G-R5, G-B2), then the role handler's own guards.
        Returns the violations and, once the generic checks pass, the typed report."""
        role = self.c.assets.roles[run.role]
        found, v = guards.required_docs(run.id, run.required_docs, parsed, self.c.docs,
                                        run.confirmed_docs)
        sha, vg = guards.required_git(role.git, parsed, self.c.workspace, run.judged_sha)
        v += vg
        if v:
            return v, None
        if role.git == GitMode.COMMIT:
            tip = await self.c.git.ref_sha(f"refs/heads/{run.branch}")
            if tip != sha:
                return [f"G-B2: {sha} is not the tip of {run.branch} on the controller remote "
                        f"(tip: {tip or 'branch not pushed'}) – push before reporting"], None
            if not await self.c.git.is_ancestor(run.base_sha, sha):
                return [f"G-B2: {sha} does not descend from base {run.base_sha}"], None
        if role.git == GitMode.HEAD and sha != self.c.change.integration_head:
            return [f"G-R5: integration head moved to {self.c.change.integration_head}"], None

        typed = report_data.parse(role.report_schema, data)     # schema-valid (G-R7)
        report = Report(run=run, node=node, parsed=parsed, data=typed, docs=found, sha=sha)
        return await handler_for(run.role).guards(self.roles, report), report
