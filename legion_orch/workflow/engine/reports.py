"""Agent reports: guards, transitions, review panel, rework.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

from typing import Any

from ...store import DocVersion
from ...util import now_iso
from .. import attest, governance, guards, risk
from ..model import (
    ACCEPTED,
    BUILD,
    CRITIQUE,
    LIVE_RUN,
    MERGING,
    PENDING,
    PLAN,
    R_ACCEPTED,
    R_ACTIVE,
    R_REPORTED,
    R_STARTING,
    R_SUPERSEDED,
    READY,
    REWORK,
    VERIFYING,
    Node,
    Run,
)
from .common import PANEL_ROLES, PLAN_DOCS, WorkflowError


class ReportsMixin:
    def docs_write(self, run: Run, path: str, content: str, base_version: int | None) -> DocVersion:
        """Guards G-W1 and G-W2. ``path`` may be relative to the change namespace."""
        path = self.p(path)
        rel = path[len(self.p("")):]
        controller_owned = rel.startswith("control/") or (
            rel.startswith("tasks/") and (rel.split("/")[2:3] in (["verification.json"], ["feedback"],
                                                                  ["decisions"])))
        if controller_owned or not any(path.startswith(w) for w in run.write_scope):
            raise WorkflowError(f"G-W1: {path} is outside your write scope {run.write_scope}")
        v = self.docs.write(path, content, author=run.id, role=run.role, base_version=base_version)
        self._event("doc", f"{v.attestation} written", run.node_id, run.id)
        return v

    def progress(self, run: Run, message: str, percent: float | None) -> None:
        run.progress.append({"at": now_iso(), "message": message[:500], "percent": percent})
        run.progress = run.progress[-50:]
        if run.status == R_STARTING:
            run.status = R_ACTIVE
        self.ctl.version += 1

    async def handle_report(self, run_id: str, status: str, summary: str, attestations: list[str],
                            data: dict | None, blocked_reason: str | None,
                            missing_decision: str | None) -> dict:
        async with self.lock:
            run = self.runs[run_id]
            if run.status not in LIVE_RUN:
                return {"accepted": False, "violations": [f"run is {run.status}"]}
            node = self.nodes[run.node_id]
            run.attempts += 1
            data = data or {}
            attestations = [attest.normalize(a, self.cid) for a in attestations]
            for f in data.get("findings") or []:
                if isinstance(f, dict) and isinstance(f.get("attestations"), list):
                    f["attestations"] = [attest.normalize(a, self.cid) for a in f["attestations"]
                                         if isinstance(a, str)]
            v = guards.report_shape(status, blocked_reason, missing_decision)
            if status == "DONE":   # G-R7: structured data matches the role's report schema
                schema = self.assets.roles[run.role].report_schema
                v += [f"G-R7: data does not match schema {schema!r}: {x}"
                      for x in self.assets.schemas.validate_data(schema, data)]
            parsed, v3 = await attest.check_all(attestations, [self.cfg.repo.name], self.docs, self.git)
            v += v3
            ctx: dict[str, Any] = {}
            if not v and status == "DONE":
                v += await self._role_guards(run, node, parsed, data, ctx)
            if v:
                run.violations.append(v)
                left = self.cfg.limits.max_report_attempts - run.attempts
                self._event("report_rejected", f"{len(v)} violation(s): " + "; ".join(v)[:600],
                            node.id, run.id, data=v)
                if left <= 0:
                    self._fail_run(run, "G-R6: too many rejected reports")
                self._save()
                return {"accepted": False, "violations": v, "attempts_left": max(left, 0)}
            run.status = R_REPORTED
            run.ended_at = now_iso()
            run.report = {"status": status, "summary": summary, "attestations": attestations,
                          "data": data, "blocked_reason": blocked_reason,
                          "missing_decision": missing_decision, "at": run.ended_at}
            self._event("report", f"{run.role} reported {status}: {summary[:200]}", node.id, run.id)
            if status == "BLOCKED":
                self._escalate(node, "blocked", blocked_reason or "",
                               resume=REWORK if node.kind == BUILD else READY,
                               run=run.id, missing_decision=missing_decision)
            else:
                await self._on_done(run, node, data, ctx)
            self._save()
            return {"accepted": True, "next": node.status}

    async def _role_guards(self, run: Run, node: Node, parsed: list[attest.Attestation],
                           data: dict, ctx: dict) -> list[str]:
        role = self.assets.roles[run.role]
        found, v = guards.required_docs(run.id, run.required_docs, parsed, self.docs)
        sha, vg = guards.required_git(role.git, parsed, self.cfg.repo.name, run.judged_sha)
        v += vg
        if v:
            return v
        ctx["docs"], ctx["sha"] = found, sha
        repo_atts = {str(a) for a in parsed}
        if role.git == "commit":
            tip = await self.git.ref_sha(f"refs/heads/{run.branch}")
            if tip != sha:
                return [f"G-B2: {sha} is not the tip of {run.branch} on the controller remote "
                        f"(tip: {tip or 'branch not pushed'}) – push before reporting"]
            if not await self.git.is_ancestor(run.base_sha, sha):
                return [f"G-B2: {sha} does not descend from base {run.base_sha}"]
        if role.git == "head" and sha != self.change.integration_head:
            return [f"G-R5: integration head moved to {self.change.integration_head}"]

        if run.role == "intake":
            req_path = self.p("intake/requirements.md")
            req = self.docs.read(req_path, found[req_path].version)
            rj, e = guards.load_json(self.docs, found[self.p("intake/risk.json")], "G-I2")
            if e:
                return e
            q = self.doc("intake/questions.md").content if self.latest("intake/questions.md") else None
            v += guards.intake(req.content, rj, q, "DONE")
        elif run.role == "planner":
            tg, e1 = guards.load_json(self.docs, found[self.p("plan/taskgraph.json")], "G-P1")
            orc, e2 = guards.load_json(self.docs, found[self.p("plan/oracle.json")], "G-P1")
            if e1 or e2:
                return e1 + e2
            v += guards.plan(tg, orc, self.change.requirements, self.change.tier,
                             self.cfg.protected_paths, self.assets.roles["implementer"].souls_allowed)
            _, vs = guards.required_docs(run.id, [self.p(f"plan/specs/{r}.md")
                                                  for r in self.change.requirements], parsed, self.docs)
            v += [x.replace("G-R4", "G-P9") for x in vs]
            ctx["taskgraph"], ctx["oracle"] = tg, orc
        elif run.role == "critic":
            plan_atts = {a.path: a.version for a in parsed if a.kind == "file"}
            latest = {self.p(p): self.latest(p) for p in PLAN_DOCS}
            v += guards.critique(data, plan_atts, latest, repo_atts)
        elif run.role == "oracle":
            orc = self.json_doc("plan/oracle.json")
            changed = await self.git.changed_paths(run.base_sha, sha)
            v += guards.scoped_diff(changed, orc.get("acceptance_tests", []), [],
                                    self.cfg.protected_paths, "G-O1")
            for p in orc.get("acceptance_tests", []):
                if not await self.git.has_object(sha, p):
                    v.append(f"G-O2: acceptance test {p} missing at {sha[:12]}")
        elif run.role == "baseline":
            orc = self.json_doc("plan/oracle.json")
            v += guards.results_cover(data, orc.get("acceptance_commands", [])
                                      + orc.get("integration_commands", []), "G-BL1")
        elif run.role == "implementer":
            t = node.task
            changed = await self.git.changed_paths(run.base_sha, sha)
            approved = governance.approved_protected_for(self, t["id"], run.created_at)  # G-PP1
            protected = [p for p in governance.protected_paths(self) if p not in approved]
            v += guards.scoped_diff(changed, t["files_modified"] + approved,
                                    t.get("files_forbidden", []), protected, "G-B3")
            ev = self.docs.read(run.required_docs[0], found[run.required_docs[0]].version).content
            v += guards.evidence_mentions(ev, t["verification"])
        elif run.role in PANEL_ROLES:
            v += guards.findings(data, repo_atts)
            v += guards.verdict(data, pass_word="PASS", fail_word="FAIL", gid="G-RV3",
                                cycle=run.cycle, require_checked=True)
        elif run.role == "integration":
            orc = self.change.oracle or {}
            v += guards.verdict(data, pass_word="PASS", fail_word="FAIL", gid="G-INT1",
                                cycle=2, require_checked=False)
            v += [x.replace("G-RV2", "G-INT1") for x in guards.findings(data, repo_atts)]
            v += guards.results_cover(data, self._oracle_commands(), "G-INT1")
            v += await self._protected_unchanged(sha)
        elif run.role == "release":
            v += guards.release_docs(self.doc("release/plan.md").content,
                                     self.doc("release/rollback.md").content,
                                     self.change.base_sha)
        return v

    async def _protected_unchanged(self, head: str) -> list[str]:
        """Guard G-INT2."""
        v = []
        if not self.change.oracle_sha:
            return v
        approved = governance.all_approved_protected(self)
        for p in (self.change.oracle or {}).get("acceptance_tests", []):
            if p in approved:
                continue
            if await self.git.blob(head, p) != await self.git.blob(self.change.oracle_sha, p):
                v.append(f"G-INT2: protected acceptance test {p} changed since oracle commit")
        return v

    async def _on_done(self, run: Run, node: Node, data: dict, ctx: dict) -> None:
        ch = self.change
        role = run.role
        if role == "intake":
            node.status = ACCEPTED
            rj = self.json_doc("intake/risk.json", ctx["docs"][self.p("intake/risk.json")].version)
            ch.risk = risk.derive_tier(rj)
            self._event("risk", f"derived tier {ch.risk['tier']} (score {ch.risk['total']})", node.id)
            self._accept_runs(node)
        elif role == "planner":
            node.status = ACCEPTED
            self._accept_runs(node)
            crit = self.nodes[CRITIQUE]
            crit.status = PENDING
        elif role == "critic":
            self._accept_runs(node)
            if data["verdict"] == "PASS":
                node.status = ACCEPTED
                self._materialize_builds(self.json_doc("plan/taskgraph.json"))
                self._event("plan", f"plan accepted by critic; {len(self.build_nodes())} build task(s)")
            else:
                plan = self.nodes[PLAN]
                limit = self.cfg.limits.critique_max_cycles + node.extra_cycles
                fb = {"cycle": plan.cycle, "source": "critique", "text": "critique requested rework: "
                      + "; ".join(f"{f.get('severity')}: {f.get('title')}" for f in data.get("findings", [])),
                      "attestations": [str(a) for a in ctx["docs"].values()]}
                if plan.cycle >= limit:
                    self._escalate(node, "critique_budget",
                                   f"G-C2: {plan.cycle} critique cycles without PASS",
                                   resume="critique_budget")
                    node.feedback.append(fb)
                else:
                    plan.cycle += 1
                    plan.feedback.append(fb)
                    plan.status = REWORK
                    node.cycle += 1
                    node.status = PENDING
        elif role == "oracle":
            node.candidate_sha = ctx["sha"]
            await self.git.pin(run.id, ctx["sha"])
            ok, head = await self.git.merge(ch.id, ctx["sha"],
                                            f"legion {ch.id}: oracle (protected acceptance tests)")
            if not ok:
                self._escalate(node, "merge", f"oracle merge failed: {head[:400]}", resume=READY)
                return
            ch.oracle_sha = ctx["sha"]
            ch.integration_head = head
            ch.oracle = self.json_doc("plan/oracle.json")
            node.accepted_sha = ctx["sha"]
            node.status = ACCEPTED
            self._accept_runs(node)
        elif role == "baseline":
            node.status = ACCEPTED
            self._accept_runs(node)
        elif role == "implementer":
            node.candidate_sha = ctx["sha"]
            await self.git.pin(run.id, ctx["sha"])
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif role in PANEL_ROLES:
            node.reviews[f"{run.cycle}:{role}"] = {
                "verdict": data["verdict"], "sha": run.judged_sha, "run": run.id,
                "findings": data.get("findings", []), "checked": data.get("checked", []),
                "doc": str(ctx["docs"][run.required_docs[0]])}
            run.status = R_ACCEPTED
            self._spawn(self._delete_sandbox(run.sandbox))
            self._panel_complete(node)
        elif role == "integration":
            node.reviews["integration"] = {"verdict": data["verdict"], "sha": ctx["sha"],
                                           "findings": data.get("findings", []), "run": run.id}
            if data["verdict"] != "PASS":
                self._escalate(node, "integration_failed", "integration verifier reported FAIL",
                               resume=READY, run=run.id)
                return
            node.candidate_sha = ctx["sha"]
            node.status = VERIFYING
            self._spawn(self._verify_node(node.id))
        elif role == "release":
            node.status = ACCEPTED
            self._accept_runs(node)

    def _panel_complete(self, node: Node) -> None:
        results = [node.reviews.get(f"{node.cycle}:{r}") for r in self.change.panel]
        if any(r is None for r in results):
            return
        if all(r["verdict"] == "PASS" and r["sha"] == node.candidate_sha for r in results):
            node.status = MERGING
            self._spawn(self._merge_node(node.id))
            return
        high = [f for r in results for f in guards.high_findings(r)]
        fps = sorted({guards.fingerprint(f) for f in high})
        node.finding_history.append(fps)
        docs = [r["doc"] for r in results]
        fb = [{"cycle": node.cycle, "source": "review",
               "text": f"{f['severity']} ({f.get('dimension', '?')}): {f['title']} – fix: {f['fix']}",
               "attestations": f.get("attestations", [])} for f in high]
        fb.append({"cycle": node.cycle, "source": "review", "text": "full review documents",
                   "attestations": docs})
        if len(node.finding_history) >= 2 and fps and node.finding_history[-1] == node.finding_history[-2]:
            node.feedback += fb
            self._escalate(node, "stale_loop", "G-RV5: identical HIGH findings in two consecutive "
                           "review cycles", resume="rework_cycle")
            return
        self._rework(node, fb, "review")

    def _rework(self, node: Node, feedback: list[dict], source: str) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5)."""
        node.feedback += feedback
        limit = self.cfg.limits.review_max_cycles + node.extra_cycles
        cur = self.runs.get(node.current_run) if node.current_run else None
        if cur and cur.status == R_REPORTED:
            cur.status = R_SUPERSEDED
        if node.cycle >= limit:
            self._escalate(node, "cycle_budget",
                           f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                           resume="rework_cycle")
            return
        node.cycle += 1
        node.candidate_sha = None
        node.status = REWORK
        self._event("rework", f"{source}: rework cycle {node.cycle}", node.id)
