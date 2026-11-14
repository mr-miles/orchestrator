"""Agent reports: guards, transitions, review panel, rework.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from ...store import DocVersion
from ...util import now_iso
from .. import attest, governance, guards, report_data
from ..assets import GitMode
from ..model import (
    BUILD,
    LIVE_RUN,
    MERGING,
    R_ACTIVE,
    R_REPORTED,
    R_STARTING,
    R_SUPERSEDED,
    REWORK,
    Feedback,
    Node,
    Oracle,
    Progress,
    ReportStatus,
    Resume,
    Run,
    RunReport,
    Verdict,
)
from ..results import ReportOutcome
from ..roles import Report, handler_for
from .common import WorkflowError, log


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


class ReportsMixin:
    def docs_write(self, run: Run, path: str, content: str, base_version: int | None) -> DocVersion:
        """Guards G-W1, G-W4 (then the store's G-W2, G-W3). ``path`` may be relative to the
        change namespace."""
        path = self.p(path)
        rel = path[len(self.p("")):]
        controller_owned = rel.startswith("control/") or (
            rel.startswith("tasks/") and (rel.split("/")[2:3] in (["verification.json"], ["feedback"],
                                                                  ["decisions"])))
        if controller_owned or not any(path.startswith(w) for w in run.write_scope):
            raise WorkflowError(f"G-W1: {path} is outside your write scope {run.write_scope}")
        lim, size = self.cfg.limits, len(content.encode())
        if run.docs_written + 1 > lim.max_docs_per_run:
            raise WorkflowError(f"G-W4: write quota exhausted: this run has written "
                                f"{run.docs_written} document versions (limit {lim.max_docs_per_run})")
        if run.doc_bytes + size > lim.max_doc_bytes_per_run:
            raise WorkflowError(f"G-W4: write quota exhausted: this run has written {run.doc_bytes} "
                                f"bytes; this write needs {size} more (limit {lim.max_doc_bytes_per_run})")
        v = self.docs.write(path, content, author=run.id, role=run.role, base_version=base_version)
        run.docs_written += 1
        run.doc_bytes += size
        self.event("doc", f"{v.attestation} written", run.node_id, run.id)
        return v

    async def agent_write(self, run: Run, path: str, content: str,
                          base_version: int | None) -> DocVersion:
        """An agent's ``docs_write``: the guards above, then the store's own (G-W2, G-W3); the
        run's quota counters are saved with it."""
        async with self.lock:
            v = self.docs_write(run, path, content, base_version)
            self._save()
            return v

    def progress(self, run: Run, message: str, percent: float | None) -> None:
        run.progress.append(Progress(message=message[:500], percent=percent))
        run.progress = run.progress[-50:]
        if run.status == R_STARTING:
            run.status = R_ACTIVE
        self.host.touch()

    def _run_lock(self, run_id: str) -> asyncio.Lock:
        return self._report_locks.setdefault(run_id, asyncio.Lock())

    async def handle_report(self, run_id: str, status: str, summary: str, attestations: list[str],
                            data: dict | None, blocked_reason: str | None,
                            missing_decision: str | None) -> ReportOutcome:
        """Three phases. (1) :meth:`_admit_report`, under the controller lock: bookkeeping and
        cheap checks. (2) :meth:`_check_report`, without it: attestation resolution, git checks
        and role guards, which can be slow – reports of the same run are serialised by a per-run
        lock and the checks are time-limited. (3) :meth:`_apply_report`, under the lock again:
        re-check the run is unchanged, then apply the transition transactionally."""
        async with self._run_lock(run_id):
            async with self.lock:
                sub = self._admit_report(run_id, _Submitted(
                    status=status, summary=summary, attestations=attestations, data=data or {},
                    blocked_reason=blocked_reason, missing_decision=missing_decision))
            if isinstance(sub, ReportOutcome):
                return sub
            report = await self._check_report(sub) if not sub.violations else None
            async with self.lock:
                return await self._apply_report(sub, report)

    def _admit_report(self, run_id: str, r: _Submitted) -> _Admitted | ReportOutcome:
        """Phase 1 (lock held): count the attempt, check limits and shape, normalise the
        attestations (here and in review findings) to fully qualified form."""
        run = self.runs[run_id]
        if run.status not in LIVE_RUN:
            return ReportOutcome.reject(f"run is {run.status}")
        run.attempts += 1
        sub = _Admitted(run=run, node=self.nodes[run.node_id], attempt=run.attempts, r=r)
        sub.violations = guards.report_limits(r.summary, r.attestations, r.data, r.blocked_reason,
                                              r.missing_decision)
        if not sub.violations:
            r.attestations = [attest.normalize(a, self.cid) for a in r.attestations]
            for f in r.data.get("findings") or []:
                if isinstance(f, dict) and isinstance(f.get("attestations"), list):
                    f["attestations"] = [attest.normalize(a, self.cid)
                                         for a in f["attestations"] if isinstance(a, str)]
            sub.violations = guards.report_shape(r.status, r.blocked_reason, r.missing_decision)
            if r.status == ReportStatus.DONE:   # G-R7: structured data matches the report schema
                schema = self.assets.roles[run.role].report_schema
                sub.violations += [f"G-R7: data does not match schema {schema!r}: {x}"
                                   for x in self.assets.schemas.validate_data(schema, r.data)]
        self._save()
        return sub

    async def _check_report(self, sub: _Admitted) -> Report | None:
        """Phase 2 (lock not held, time-limited): attestations resolve; for DONE, the role's
        git and document guards. Adds to ``sub.violations``; returns the typed report a DONE
        transition needs."""
        timeout = self.cfg.limits.report_check_timeout_s
        try:
            async with asyncio.timeout(timeout):
                parsed, v = await attest.check_all(sub.r.attestations, [self.cfg.repo.name],
                                                   self.docs, self.git)
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
            with self.st.transaction():
                run.status = R_REPORTED
                run.ended_at = now_iso()
                run.report = RunReport(status=r.status, summary=r.summary,
                                       attestations=r.attestations, data=r.data,
                                       blocked_reason=r.blocked_reason,
                                       missing_decision=r.missing_decision, at=run.ended_at)
                self.event("report", f"{run.role} reported {r.status}: {r.summary[:200]}",
                           node.id, run.id)
                if r.status == ReportStatus.BLOCKED:
                    self.escalate(node, "blocked", r.blocked_reason or "",
                                  resume=Resume.REWORK if node.kind == BUILD else Resume.READY,
                                  run=run.id, missing_decision=r.missing_decision)
                else:   # role-specific transition (see workflow/roles/)
                    await handler_for(run.role).on_done(self, report)
                self._save()
        except Exception as e:
            log.exception("applying report of %s failed", run.id)
            self._fail_run(run, f"controller error while applying the report: {e!r} "
                                "(external effects such as git merges may already exist)")
            with contextlib.suppress(Exception):
                self._save()
            return ReportOutcome.reject("controller error while applying the report; escalated")
        return ReportOutcome(accepted=True, next=node.status)

    def _reject_report(self, run: Run, node: Node, v: list[str]) -> ReportOutcome:
        run.violations.append(v)
        left = self.cfg.limits.max_report_attempts - run.attempts
        self.event("report_rejected", f"{len(v)} violation(s): " + "; ".join(v)[:600],
                   node.id, run.id)
        if left <= 0:
            self._fail_run(run, "G-R6: too many rejected reports")
        self._save()
        return ReportOutcome.reject(*v, attempts_left=max(left, 0))

    async def _role_guards(self, run: Run, node: Node, parsed: list[attest.Attestation],
                           data: dict) -> tuple[list[str], Report | None]:
        """Required documents and git (G-R4/G-R5, G-B2), then the role handler's own guards.
        Returns the violations and, once the generic checks pass, the typed report."""
        role = self.assets.roles[run.role]
        found, v = guards.required_docs(run.id, run.required_docs, parsed, self.docs)
        sha, vg = guards.required_git(role.git, parsed, self.cfg.repo.name, run.judged_sha)
        v += vg
        if v:
            return v, None
        if role.git == GitMode.COMMIT:
            tip = await self.git.ref_sha(f"refs/heads/{run.branch}")
            if tip != sha:
                return [f"G-B2: {sha} is not the tip of {run.branch} on the controller remote "
                        f"(tip: {tip or 'branch not pushed'}) – push before reporting"], None
            if not await self.git.is_ancestor(run.base_sha, sha):
                return [f"G-B2: {sha} does not descend from base {run.base_sha}"], None
        if role.git == GitMode.HEAD and sha != self.change.integration_head:
            return [f"G-R5: integration head moved to {self.change.integration_head}"], None

        typed = report_data.parse(role.report_schema, data)     # schema-valid (G-R7)
        report = Report(run=run, node=node, parsed=parsed, data=typed, docs=found, sha=sha)
        return await handler_for(run.role).guards(self, report), report

    async def protected_unchanged(self, head: str) -> list[str]:
        """Guard G-INT2."""
        v = []
        if not self.change.oracle_sha:
            return v
        approved = governance.all_approved_protected(self)
        orc = self.change.oracle or Oracle()
        for p in orc.acceptance_tests + orc.protected_support:
            if p in approved:
                continue
            if await self.git.blob(head, p) != await self.git.blob(self.change.oracle_sha, p):
                v.append(f"G-INT2: protected acceptance test {p} changed since oracle commit")
        return v

    def panel_complete(self, node: Node) -> None:
        results = [node.review(node.cycle, r) for r in self.change.panel]
        if any(r is None for r in results):
            return
        if all(r.verdict == Verdict.PASS and r.sha == node.candidate_sha for r in results):
            node.status = MERGING
            self._spawn(self._merge_node(node.id))
            return
        high = [f for r in results for f in r.findings if f.high]
        fps = sorted({f.fingerprint for f in high})
        node.finding_history.append(fps)
        fb = [Feedback(cycle=node.cycle, source="review",
                       text=f"{f.severity} ({f.dimension or '?'}): {f.title} – fix: {f.fix[:500]}",
                       attestations=f.attestations) for f in high]
        fb.append(Feedback(cycle=node.cycle, source="review", text="full review documents",
                           attestations=[r.doc for r in results if r.doc]))
        if len(node.finding_history) >= 2 and fps and node.finding_history[-1] == node.finding_history[-2]:
            node.feedback += fb
            self.escalate(node, "stale_loop", "G-RV5: identical HIGH findings in two consecutive "
                           "review cycles", resume=Resume.REWORK_CYCLE)
            return
        self._rework(node, fb, "review")

    def _rework(self, node: Node, feedback: list[Feedback], source: str) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5)."""
        node.feedback += feedback
        limit = self.cfg.limits.review_max_cycles + node.extra_cycles
        cur = self.runs.get(node.current_run) if node.current_run else None
        if cur and cur.status == R_REPORTED:
            cur.status = R_SUPERSEDED
        if node.cycle >= limit:
            self.escalate(node, "cycle_budget",
                           f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                           resume=Resume.REWORK_CYCLE)
            return
        node.cycle += 1
        node.candidate_sha = None
        node.status = REWORK
        self.event("rework", f"{source}: rework cycle {node.cycle}", node.id)
