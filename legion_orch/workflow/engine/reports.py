"""Agent reports: guards, transitions, review panel, rework.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import asyncio
import contextlib
import copy
from typing import Any

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
from ..roles import Report, handler_for
from .common import WorkflowError, log


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
        locks = self.__dict__.setdefault("_report_locks", {})
        return locks.setdefault(run_id, asyncio.Lock())

    def _checkpoint(self) -> tuple:
        """Snapshot of the change, node and run state (object identity is kept on restore, so
        coroutines holding references keep seeing the live objects)."""
        objs = [self.st.change, *self.st.nodes.values(), *self.st.runs.values()]
        return (self.st.change, dict(self.st.nodes), dict(self.st.runs),
                {id(o): copy.deepcopy(o.__dict__) for o in objs})

    def _restore(self, cp: tuple) -> None:
        change, nodes, runs, states = cp
        self.st.change = change
        self.st.nodes.clear()
        self.st.nodes.update(nodes)
        self.st.runs.clear()
        self.st.runs.update(runs)
        for o in [change, *nodes.values(), *runs.values()]:
            o.__dict__.clear()
            o.__dict__.update(states[id(o)])

    async def handle_report(self, run_id: str, status: str, summary: str, attestations: list[str],
                            data: dict | None, blocked_reason: str | None,
                            missing_decision: str | None) -> dict:
        """Three phases. (1) Under the controller lock: bookkeeping and cheap checks. (2) Without
        it: attestation resolution, git checks and role guards, which can be slow – reports of
        the same run are serialised by a per-run lock and the checks are time-limited. (3) Under
        the lock again: re-check the run is unchanged, then apply the transition transactionally
        (any exception restores the state and escalates)."""
        async with self._run_lock(run_id):
            async with self.lock:
                run = self.runs[run_id]
                if run.status not in LIVE_RUN:
                    return {"accepted": False, "violations": [f"run is {run.status}"]}
                node = self.nodes[run.node_id]
                run.attempts += 1
                attempt = run.attempts
                data = data or {}
                v = guards.report_limits(summary, attestations, data, blocked_reason, missing_decision)
                if not v:
                    attestations = [attest.normalize(a, self.cid) for a in attestations]
                    for f in data.get("findings") or []:
                        if isinstance(f, dict) and isinstance(f.get("attestations"), list):
                            f["attestations"] = [attest.normalize(a, self.cid)
                                                 for a in f["attestations"] if isinstance(a, str)]
                    v = guards.report_shape(status, blocked_reason, missing_decision)
                    if status == ReportStatus.DONE:   # G-R7: structured data matches the role's report schema
                        schema = self.assets.roles[run.role].report_schema
                        v += [f"G-R7: data does not match schema {schema!r}: {x}"
                              for x in self.assets.schemas.validate_data(schema, data)]
                self._save()

            ctx: dict[str, Any] = {}
            if not v:
                try:
                    async with asyncio.timeout(self.cfg.limits.report_check_timeout_s):
                        parsed, v3 = await attest.check_all(attestations, [self.cfg.repo.name],
                                                            self.docs, self.git)
                        v += v3
                        if not v and status == ReportStatus.DONE:
                            v += await self._role_guards(run, node, parsed, data, ctx)
                except TimeoutError:
                    v.append(f"G-R8: report checks exceeded {self.cfg.limits.report_check_timeout_s}s")

            async with self.lock:
                if run.status not in LIVE_RUN or run.attempts != attempt:
                    return {"accepted": False, "violations": [f"run changed during checks ({run.status})"]}
                if v:
                    return self._reject_report(run, node, v)
                checkpoint = self._checkpoint()
                try:
                    run.status = R_REPORTED
                    run.ended_at = now_iso()
                    run.report = RunReport(status=status, summary=summary, attestations=attestations,
                                           data=data, blocked_reason=blocked_reason,
                                           missing_decision=missing_decision, at=run.ended_at)
                    self.event("report", f"{run.role} reported {status}: {summary[:200]}", node.id, run.id)
                    if status == ReportStatus.BLOCKED:
                        self.escalate(node, "blocked", blocked_reason or "",
                                      resume=Resume.REWORK if node.kind == BUILD else Resume.READY,
                                      run=run.id, missing_decision=missing_decision)
                    else:
                        await self._on_done(run, node, data, ctx)
                    self._save()
                except Exception as e:      # never leave a half-applied transition behind
                    log.exception("applying report of %s failed", run.id)
                    self._restore(checkpoint)
                    self._fail_run(run, f"controller error while applying the report: {e!r} "
                                        "(external effects such as git merges may already exist)")
                    with contextlib.suppress(Exception):
                        self._save()
                    return {"accepted": False,
                            "violations": ["controller error while applying the report; escalated"]}
                return {"accepted": True, "next": node.status}

    def _reject_report(self, run: Run, node: Node, v: list[str]) -> dict:
        run.violations.append(v)
        left = self.cfg.limits.max_report_attempts - run.attempts
        self.event("report_rejected", f"{len(v)} violation(s): " + "; ".join(v)[:600],
                   node.id, run.id, data=v[:50])
        if left <= 0:
            self._fail_run(run, "G-R6: too many rejected reports")
        self._save()
        return {"accepted": False, "violations": v, "attempts_left": max(left, 0)}

    async def _role_guards(self, run: Run, node: Node, parsed: list[attest.Attestation],
                           data: dict, ctx: dict) -> list[str]:
        role = self.assets.roles[run.role]
        found, v = guards.required_docs(run.id, run.required_docs, parsed, self.docs)
        sha, vg = guards.required_git(role.git, parsed, self.cfg.repo.name, run.judged_sha)
        v += vg
        if v:
            return v
        ctx["docs"], ctx["sha"] = found, sha
        if role.git == GitMode.COMMIT:
            tip = await self.git.ref_sha(f"refs/heads/{run.branch}")
            if tip != sha:
                return [f"G-B2: {sha} is not the tip of {run.branch} on the controller remote "
                        f"(tip: {tip or 'branch not pushed'}) – push before reporting"]
            if not await self.git.is_ancestor(run.base_sha, sha):
                return [f"G-B2: {sha} does not descend from base {run.base_sha}"]
        if role.git == GitMode.HEAD and sha != self.change.integration_head:
            return [f"G-R5: integration head moved to {self.change.integration_head}"]

        typed = report_data.parse(role.report_schema, data)     # schema-valid (G-R7)
        report = Report(run=run, node=node, parsed=parsed, data=typed, docs=found, sha=sha)
        ctx["report"] = report
        return v + await handler_for(run.role).guards(self, report)

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

    async def _on_done(self, run: Run, node: Node, data: dict, ctx: dict) -> None:
        """Role-specific transition after an accepted DONE report (see workflow/roles/)."""
        await handler_for(run.role).on_done(self, ctx["report"])

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
