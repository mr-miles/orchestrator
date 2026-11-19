"""Human actions: the H-1/H-E1/H-2/H-3 gates, escalation resolution, change-level governance."""
from __future__ import annotations

from .. import governance, guards, risk, setup
from ..environment import EnvironmentSpec, Kind, Library, LibraryError, promote
from ..model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BASELINE,
    BUILD,
    ESCALATED,
    FAILED,
    H_CHANGE,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTAKE,
    INTEGRATION,
    LIVE_RUN,
    MERGING,
    ORACLE,
    PENDING,
    PLAN,
    PROVISION,
    R_REPORTED,
    R_SUPERSEDED,
    READY,
    RETRO,
    REVIEWING,
    REWORK,
    RUNNING,
    SHIP,
    VERIFYING,
    Approval,
    ApprovalState,
    ChangeEnd,
    ChangeStatus,
    Feedback,
    FixRound,
    Node,
    Override,
    Resume,
    TierOverride,
)
from ..results import ActionResult
from .acceptance import Acceptance
from .common import ENV_SPEC, WorkflowError, _slug
from .core import ChangeCore
from .graph import StageGraph
from .runs import RunLifecycle


class HumanGates:
    """What a decision owner may do to a change, checked and applied under the lock."""

    def __init__(self, core: ChangeCore, graph: StageGraph, acceptance: Acceptance,
                 lifecycle: RunLifecycle):
        self.c, self.graph, self.acceptance, self.lifecycle = core, graph, acceptance, lifecycle

    def _check_approver(self, approver: str) -> None:
        if approver not in self.c.cfg.decision_owners:
            raise WorkflowError(f"approver {approver!r} is not a decision owner "
                                f"({', '.join(self.c.cfg.decision_owners)})")

    async def human_action(self, node_id: str, action: str, approver: str,
                           payload: dict | None = None) -> ActionResult:
        payload = payload or {}
        async with self.c.lock:
            self._check_approver(approver)
            node = self.c.nodes.get(node_id)
            if node is None:
                raise WorkflowError(f"unknown node {node_id}")
            if node.status == ESCALATED:
                res = await self._resolve_escalation(node, action, approver, payload)
            elif node.kind == HUMAN and node.status == AWAITING_HUMAN:
                res = await self._human_gate(node, action, approver, payload)
            elif node.kind == SHIP and action == "recheck" and node.status in (ACCEPTED, FAILED):
                node.status = RUNNING
                self.c.spawn(self.acceptance.ship())
                res = ActionResult()
            else:
                raise WorkflowError(f"action {action!r} not available for {node_id} in {node.status}")
            self.c.event("human_action", f"{approver}: {action}", node_id)
            self.c.save()
            res.status = node.status
            return res

    def _feedback_doc(self, node: Node, approver: str, text: str, kind: str) -> str:
        n = len([f for f in node.feedback if f.source == kind]) + 1
        path = f"tasks/{node.task.id}/{kind}/{n}.md" if node.kind == BUILD \
            else f"control/{kind}/{_slug(node.id)}-{n}.md"
        return self.c.write_record(path, f"# {kind} for {node.id}\n\nBy: {approver}\n\n{text}\n").attestation

    # ------------------------------------------------------------------ human gates
    async def _human_gate(self, node: Node, action: str, approver: str, payload: dict) -> ActionResult:
        handler = self.GATE_ACTIONS.get((node.id, action))
        if handler is None:
            valid = [a for g, a in self.GATE_ACTIONS if g == node.id]
            raise WorkflowError(f"action {action!r} not valid for {node.id} (one of {valid})")
        return await getattr(self, handler)(node, approver, payload)

    # (gate, action) -> method(node, approver, payload)
    GATE_ACTIONS = {
        (H_REQUIREMENTS, "accept"): "_accept_requirements",
        (H_REQUIREMENTS, "revise"): "_revise_requirements",
        (H_SPEC, "approve"): "_approve_spec",
        (H_SPEC, "revise"): "_revise_spec",
        (H_SPEC, "restart"): "_restart_plan",
        (H_ENV, "approve"): "_approve_environment",
        (H_ENV, "revise"): "_revise_environment",
        (H_CHANGE, "accept"): "_accept_change",
        (H_CHANGE, "reject"): "_reject_change",
    }

    def _send_back(self, target: Node, approver: str, payload: dict) -> None:
        """A human sends ``target``'s work back with feedback: a new cycle, reworked."""
        text = (payload.get("feedback") or "").strip()
        if not text:
            raise WorkflowError("feedback is required")
        att = self._feedback_doc(target, approver, text, "feedback")
        target.cycle += 1
        target.feedback.append(Feedback(cycle=target.cycle, source="feedback", text=text,
                                        attestations=[att]))
        target.status = REWORK

    async def _accept_requirements(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-1: accept requirements and the risk tier (raising it is free; lowering needs a
        reason). Fixes the change's requirements and review panel and creates the stage nodes."""
        ch = self.c.change
        pins = {self.c.p(p): self.c.latest(p) for p in ("intake/requirements.md", "intake/risk.json")}
        derived = ch.risk.tier
        tier = payload.get("tier") or derived
        if tier not in risk.TIERS:
            raise WorkflowError(f"unknown tier {tier}")
        if risk.rank(tier) < risk.rank(derived) and not risk.valid_reason(payload.get("reason")):
            raise WorkflowError("lowering the tier needs a reason of ≥24 chars and more than one word")
        if tier != derived:
            ch.tier_override = TierOverride(from_tier=derived, to_tier=tier,
                                            reason=payload.get("reason"), approver=approver)
        ch.tier = tier
        ch.requirements = guards.requirements_ids(self.c.doc("intake/requirements.md").content)
        ch.panel = risk.review_panel(tier, ch.risk.scores)
        rec = Approval(gate=H_REQUIREMENTS, approver=approver, pins=pins, tier=tier)
        rec.attestation = self.c.write_record("control/approvals/requirements.json", rec).attestation
        ch.approvals[H_REQUIREMENTS] = rec
        node.status = ACCEPTED
        self.graph.create_stage_nodes()
        return ActionResult(tier=tier, panel=ch.panel)

    async def _revise_requirements(self, node: Node, approver: str, payload: dict) -> ActionResult:
        self._send_back(self.c.nodes[INTAKE], approver, payload)
        node.status = PENDING
        return ActionResult()

    async def _approve_spec(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-2: each delta spec, the plan and the oracle are separate decisions (possibly by
        different approvers); the gate completes once every subject is approved and fresh (and,
        at R3, the independent baseline is attested)."""
        subjects = payload.get("subjects") or "all"
        everything = subjects == "all" or subjects == ["all"]
        base = governance.baseline_subject(self.c)
        attest = base is not None and base.status != ApprovalState.APPROVED and (
            everything or governance.BASELINE_SUBJECT in subjects)
        if not everything:
            subjects = [s for s in subjects if s != governance.BASELINE_SUBJECT]
        if attest and (base.note or not base.sources):
            raise WorkflowError(f"independent-baseline: {base.note or 'no baseline evidence'}")
        try:
            done = await governance.approve_subjects(self.c, subjects, approver) \
                if everything or subjects else []
            if attest:
                # R3: the baseline attestation is decided here with the other subjects, citing
                # the baseline report and the controller's own run of its commands (G-AT1)
                await governance.record_attestation(self.c, approver, {
                    "kind": governance.BASELINE_SUBJECT, "verdict": "pass",
                    "sources": base.sources})
        except governance.GovernanceError as e:
            raise WorkflowError(str(e)) from None
        if attest:
            done.append(governance.BASELINE_SUBJECT)
        pending = [x.subject for x in governance.h2_status(self.c) if x.status != ApprovalState.APPROVED]
        base = governance.baseline_subject(self.c)
        if base is not None and base.status != ApprovalState.APPROVED:
            pending.append(governance.BASELINE_SUBJECT)
        if pending:
            return ActionResult(approved=done, pending=pending)
        node.status = ACCEPTED
        return ActionResult(approved=done, pending=[])

    async def _revise_spec(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-2 revise. In a fix round, the round goes back to the planner (its build tasks are
        withdrawn; earlier rounds stand). Otherwise the whole plan restarts (Restart plan)."""
        ch = self.c.change
        if ch.round == 1:
            return await self._restart_plan(node, approver, payload)
        self._send_back(self.c.nodes[PLAN], approver, payload)
        self.graph.remove_round_builds(ch.round)
        node.status = PENDING
        return ActionResult(note=f"fix round {ch.round} goes back to the planner; Restart plan "
                                 "restarts the whole plan")

    async def _restart_plan(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-2 restart: the plan is reworked from the start (and critiqued again); everything
        derived from it starts over (oracle, baseline, every round's build nodes, the
        integration branch and surface pins)."""
        ch = self.c.change
        plan = self.c.nodes[PLAN]
        self._send_back(plan, approver, payload)
        if ch.round > 1:          # restarted from a fix round: the rounds' feedback is history
            plan.round, plan.round_start_cycle = 1, plan.cycle
        ch.round, ch.rounds = 1, []
        self.graph.reset_environment()
        for nid in (ORACLE, BASELINE):
            if nid in self.c.nodes:
                n = self.c.nodes[nid]
                n.status, n.cycle = PENDING, n.cycle + 1
        for b in self.c.build_nodes():
            del self.c.nodes[b.id]
        await self.c.git.init_integration(ch.id, ch.base_sha)
        ch.integration_head, ch.oracle_sha, ch.oracle = ch.base_sha, None, None
        ch.setup_pin = await setup.pin_at_base(self.c.git, ch.base_sha)   # the base's again
        ch.surface_pins = {}
        node.status = PENDING
        return ActionResult()

    async def _approve_environment(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-E1: approve the sandbox environment at its current version; provisioning follows."""
        version = self.c.latest(ENV_SPEC)
        if version is None:
            raise WorkflowError(f"no {ENV_SPEC} to approve")
        note = (payload.get("feedback") or "").strip() or None
        rec = Approval(gate=H_ENV, approver=approver, pins={self.c.p(ENV_SPEC): version}, note=note)
        rec.attestation = self.c.write_record("control/approvals/environment.json", rec).attestation
        self.c.change.approvals[H_ENV] = rec
        await self._adopt_mise(EnvironmentSpec.model_validate(self.c.json_doc(ENV_SPEC, version)))
        node.status = ACCEPTED
        return ActionResult()

    async def _adopt_mise(self, spec: EnvironmentSpec) -> None:
        """§5.3c: the approved spec's mise.toml goes onto the integration branch (a controller
        commit, before any build) and is pinned as the setup that runs (G-MS1)."""
        ch = self.c.change
        if spec.mise is None:
            return
        head = await self.c.git.commit_file(ch.id, setup.MISE_TOML, spec.mise.content,
                                            f"legion {ch.id}: mise.toml approved at H-E1")
        if head != ch.integration_head:
            self.c.event("environment", f"committed the approved mise.toml -> {head[:12]}", H_ENV)
        ch.integration_head = head
        ch.setup_pin = setup.pin_for(spec.mise.content, H_ENV)

    async def _revise_environment(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-E1 revise: the planner reworks the plan's environment with the feedback; the plan
        is then critiqued again."""
        self._send_back(self.c.nodes[PLAN], approver, payload)
        node.status = PENDING
        return ActionResult()

    async def _accept_change(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-3: accept the whole change at the current integration head."""
        ch = self.c.change
        head = await self.c.git.integration_head(ch.id)
        if head != ch.integration_head:
            raise WorkflowError("integration head moved; refresh")
        note = (payload.get("feedback") or "").strip() or None
        # the gates only a human attestation satisfies, attested with the acceptance (one step)
        rows = {r.gate: r for r in governance.h3_attestations(self.c)}
        wanted = payload.get("attestations") or []
        allowed = {k for r in rows.values() if r.status != "attested" for k in r.kinds}
        bad = [a.get("kind") for a in wanted if not isinstance(a, dict) or a.get("kind") not in allowed]
        if bad:
            raise WorkflowError(f"H-3: attestation kind(s) {bad} not needed; choose from "
                                f"{sorted(allowed)}")
        waivable = {k for r in rows.values() if r.waivable for k in r.kinds}
        for a in wanted:
            if a.get("verdict", "pass") not in ("pass", "not_applicable") or (
                    a.get("verdict") == "not_applicable" and a["kind"] not in waivable):
                raise WorkflowError(f"H-3: {a['kind']} can only be attested pass here")
        try:
            for a in wanted:
                await governance.record_attestation(self.c, approver, {
                    "kind": a["kind"], "verdict": a.get("verdict", "pass"),
                    "sources": a.get("sources") or [],
                    **({"waiver_reason": a.get("waiver_reason")}
                       if a.get("verdict") == "not_applicable" else {})})
        except governance.GovernanceError as e:
            raise WorkflowError(str(e)) from None
        missing = [r.gate for r in governance.h3_attestations(self.c) if r.status != "attested"]
        rec = Approval(gate=H_CHANGE, approver=approver, head=head, note=note)
        rec.attestation = self.c.write_record("control/approvals/change.json", rec).attestation
        ch.approvals[H_CHANGE] = rec
        node.status = ACCEPTED
        return ActionResult(warnings=[f"{g} has no passing attestation: the ship check will be "
                                      "NOT_READY until one is recorded" for g in missing])

    async def _reject_change(self, node: Node, approver: str, payload: dict) -> ActionResult:
        return self._end_change(node, approver, payload, "reject")

    def _end_change(self, node: Node, approver: str, payload: dict, action: str) -> ActionResult:
        """Abandon the change (at an escalation, or rejected at H-3): record who, where and
        the operator's reason, and run the retrospective on what the change produced so far
        (unless ``retrospective`` is false), whether or not retrospectives run automatically."""
        c = self.c
        reason = (payload.get("reason") or payload.get("feedback") or "").strip() or None
        rec = ChangeEnd(by=approver, node=node.id, action=action, reason=reason)
        rec.attestation = c.write_record("control/ended.json", rec).attestation
        c.change.ended = rec
        c.change.status = ChangeStatus.ABANDONED
        node.status = FAILED
        verb = "abandoned the change" if action == "abandon" else "rejected the change"
        c.event("abandoned", f"{approver} {verb} at {node.id}: {reason or '(no reason given)'}",
                node.id)
        if payload.get("retrospective", True) is not False and RETRO not in c.nodes:
            self.graph.add_retrospective()
            c.event("retrospective", "the change was abandoned: retrospective queued", RETRO)
        return ActionResult(attestation=rec.attestation)

    # ------------------------------------------------------------------ escalations
    async def _resolve_escalation(self, node: Node, action: str, approver: str,
                                  payload: dict) -> ActionResult:
        if action == "abandon":
            return self._end_change(node, approver, payload, "abandon")
        if action == "override_accept":
            return self._override_accept(node, approver, payload)
        if action == "plan_fix":
            return self._plan_fix(node, approver, payload)
        if action == "revise_plan":
            return await self._revise_plan(node, approver, payload)
        if action not in ("retry", "answer"):
            raise WorkflowError(f"unknown escalation action {action!r}")
        if node.escalation and node.escalation.retrying:
            raise WorkflowError("a retry is already in progress (removing the old sandbox)")
        resume = node.escalation.resume if node.escalation else Resume.READY
        if action == "retry":
            old = self._sandbox_to_remove(node)
            if old:
                node.escalation.retrying = True
                self.c.spawn(self._retry_after_removing(node.id, old))
                return ActionResult(note=f"removing sandbox {old}, then retrying")
        if action == "answer":
            text = (payload.get("answer") or payload.get("feedback") or "").strip()
            if not text:
                raise WorkflowError("an answer is required")
            att = self._feedback_doc(node, approver, text, "decisions")
            node.feedback.append(Feedback(cycle=node.cycle, source="decisions",
                                          text=f"human decision: {text}", attestations=[att],
                                          head=self.c.change.integration_head))
        node.escalation = None
        self._resume(node, resume)
        return ActionResult(status=node.status)

    async def _revise_plan(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """An oracle or baseline escalation the plan caused (an oracle whose commands cannot
        work, say): the plan goes back to the planner with the human's feedback, as Request
        revision does at H-2 – which this stage never reaches while it is escalated."""
        ch = self.c.change
        if node.kind not in (ORACLE, BASELINE) or self.c.nodes[H_SPEC].status == ACCEPTED:
            raise WorkflowError("revise_plan is only available for an oracle or baseline "
                                "escalation before H-2 is approved")
        text = (payload.get("feedback") or payload.get("answer") or "").strip()
        if not text:
            raise WorkflowError("feedback for the planner is required")
        node.escalation = None
        if ch.round == 1:
            node.status = PENDING        # the restart resets the oracle and baseline
        elif node.kind == BASELINE:
            # a fix round reruns the baseline once its revised plan passes critique; until then
            # it stands as before the round (an oracle waits for the plan anyway)
            node.status = ACCEPTED
        else:
            node.status, node.cycle = PENDING, node.cycle + 1
        self.c.event("escalation", f"{approver}: plan sent back to the planner from {node.id}",
                     node.id)
        return await self._revise_spec(self.c.nodes[H_SPEC], approver, {"feedback": text})

    def _plan_fix(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """Plan a fix (§5.10a): integration failed late, so a fix round plans new work on top of
        the integration head – planner (fix mode), critique, H-2, new build tasks and
        integration again. Accepted tasks are never reopened."""
        ch, limit = self.c.change, self.c.cfg.limits.fix_rounds
        if node.kind != INTEGRATION or not node.escalation \
                or node.escalation.kind != "integration_failed":
            raise WorkflowError("plan_fix is only available when integration failed")
        if len(ch.rounds) >= limit:
            raise WorkflowError(f"limits.fix_rounds ({limit}) reached: answer, override at the "
                                "gates or abandon the change")
        run = self.c.runs.get(node.current_run) if node.current_run else None
        ver = node.verification.get("integration")
        failure = list(dict.fromkeys(
            [a for a in (run.report.attestations if run and run.report else [])
             if a.startswith("file:")] + ([ver.attestation] if ver and ver.attestation
                                         and not ver.ok else [])))
        n = ch.round + 1
        rec = FixRound(number=n, approver=approver, head=ch.integration_head, failure=failure,
                       comment=(payload.get("comment") or payload.get("feedback") or "").strip()
                       or None)
        rec.attestation = self.c.write_record(f"control/rounds/{n}.json", rec).attestation
        ch.round = n
        ch.rounds.append(rec)
        node.escalation = None
        self.lifecycle.retire_runs(node)
        # integration waits for the round's H-2 and (added at its critique PASS) its tasks
        node.status, node.cycle = PENDING, node.cycle + 1
        node.depends_on = list(dict.fromkeys([*node.depends_on, H_SPEC]))
        plan = self.c.nodes[PLAN]
        plan.cycle += 1
        plan.round, plan.round_start_cycle, plan.status = n, plan.cycle, READY
        h2 = self.c.nodes[H_SPEC]
        h2.status = PENDING
        waits = [PLAN] + ([PROVISION] if PROVISION in self.c.nodes else [])
        h2.depends_on = list(dict.fromkeys([*waits, *h2.depends_on]))
        self.c.event("fix_round", f"{approver}: fix round {n} planned on top of "
                     f"{(ch.integration_head or '')[:12]}", node.id)
        return ActionResult(note=f"fix round {n}: the planner plans it on top of the integration head")

    def _sandbox_to_remove(self, node: Node) -> str | None:
        """The escalated run's sandbox, if it still exists and no live run uses it."""
        run = self.c.runs.get(node.escalation.run) if node.escalation and node.escalation.run else None
        if not run or not run.sandbox or run.sandbox_deleted:
            return None
        if any(r.sandbox == run.sandbox and r.status in LIVE_RUN for r in self.c.runs.values()):
            return None
        return run.sandbox

    async def _retry_after_removing(self, node_id: str, sandbox: str) -> None:
        """Retry: delete the failed run's sandbox first (a failed delete is reported and the
        retry goes ahead in a new sandbox), then resume where the escalation said."""
        ok = await self.lifecycle.delete_sandbox(sandbox) if sandbox else True
        async with self.c.lock:
            node = self.c.nodes.get(node_id)
            esc = node.escalation if node else None
            if node is None or node.status != ESCALATED or esc is None or not esc.retrying:
                return                  # abandoned or otherwise resolved meanwhile
            if not ok:
                self.c.event("sandbox", f"could not remove {sandbox}; retrying in a new sandbox",
                             node_id)
            if node.sandbox == sandbox:
                node.sandbox = None     # the next run gets a fresh sandbox
            node.escalation = None
            self._resume(node, esc.resume)
            self.c.event("human_action", f"retry: resumed {esc.resume}", node_id)
            self.c.save()

    def resume_retries(self) -> None:
        """After a restart: finish retries that were removing their old sandbox."""
        for node in self.c.nodes.values():
            esc = node.escalation
            if node.status == ESCALATED and esc and esc.retrying:
                run = self.c.runs.get(esc.run) if esc.run else None
                self.c.spawn(self._retry_after_removing(node.id, run.sandbox if run else ""))

    def _override_accept(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """Accept a build task without a full panel PASS. Deterministic verification at the
        candidate is still required (it is not waivable); the gate reports human judgement."""
        if node.kind != BUILD:
            raise WorkflowError("override is only available for build tasks")
        if not risk.valid_reason(payload.get("reason")):
            raise WorkflowError("override needs a reason of ≥24 chars and more than one word")
        ver = node.current_verification()
        if not node.candidate_sha or not ver or not ver.ok or ver.sha != node.candidate_sha:
            raise WorkflowError("override requires passing deterministic verification at the "
                                "candidate (deterministic_verification is not waivable)")
        node.override = Override(approver=approver, reason=payload["reason"], sha=node.candidate_sha)
        node.escalation = None
        node.status = MERGING
        self.c.spawn(self.acceptance.merge_node(node.id))
        return ActionResult()

    def _resume(self, node: Node, resume: Resume) -> None:
        """Continue the workflow where the resolved escalation said it should."""
        if resume == Resume.CRITIQUE_BUDGET:          # one more critique cycle: rework the plan
            node.extra_cycles += 1
            node.cycle += 1
            node.status = REWORK
            self.graph.reset_environment()
        elif resume == Resume.REWORK_CYCLE:           # one more review cycle
            node.extra_cycles += 1
            cur = self.c.runs.get(node.current_run) if node.current_run else None
            if cur and cur.status == R_REPORTED:
                cur.status = R_SUPERSEDED
            node.cycle += 1
            node.candidate_sha = None
            node.status = REWORK
        elif resume == Resume.VERIFYING:
            node.status = VERIFYING
            self.c.spawn(self.acceptance.verify_node(node.id))
        elif resume == Resume.REVIEWING:              # re-dispatch the missing panel member(s)
            node.status = REVIEWING
        elif resume == Resume.MERGING:                # merge the accepted candidate again
            node.status = MERGING
            self.c.spawn(self.acceptance.merge_node(node.id))
        elif resume == Resume.REWORK:
            node.status = REWORK
        else:                                         # a new run, in a new sandbox
            self.lifecycle.retire_runs(node)
            node.status = READY

    def _promote(self, approver: str, payload: dict) -> str:
        """Copy a proposal of the H-E1-approved spec version into the library."""
        appr = self.c.change.approvals.get(H_ENV)
        version = appr.pins.get(self.c.p(ENV_SPEC)) if appr else None
        if version is None:
            raise WorkflowError("the environment spec is not approved (H-E1)")
        try:
            kind = Kind(payload.get("kind"))
        except ValueError:
            raise WorkflowError(f"kind must be one of {[k.value for k in Kind]}") from None
        spec = EnvironmentSpec.model_validate(self.c.json_doc(ENV_SPEC, version))
        source = f"file:{self.c.p(ENV_SPEC)}@{version} (approved by {appr.approver}, " \
                 f"promoted by {approver})"
        try:
            dest = promote(Library.for_config(self.c.cfg, self.c.assets.schemas), spec, kind,
                           str(payload.get("name") or ""), source)
            Library.for_config(self.c.cfg, self.c.assets.schemas)     # still valid as a whole
        except LibraryError as e:
            raise WorkflowError(str(e)) from None
        self.c.event("library", f"{approver} promoted {kind} {payload.get('name')} into {dest}")
        return f"wrote {dest}"

    def _start_retrospective(self, approver: str, payload: dict) -> str:
        """Ask for (another) retrospective of an ended change: an agent analyses the
        change's records, logs and reflections and proposes improvements to the assets and the
        process. Asking again runs a new cycle; ``focus`` optionally says what to look at."""
        c = self.c
        if c.change.status == ChangeStatus.ACTIVE:
            raise WorkflowError("a retrospective needs an ended change (it is still active)")
        node = c.nodes.get(RETRO)
        if node is not None and node.status not in (ACCEPTED, FAILED, ESCALATED):
            raise WorkflowError(f"a retrospective is already {node.status}")
        node = self.graph.add_retrospective()
        focus = (payload.get("focus") or payload.get("feedback") or "").strip()
        if focus:
            att = self._feedback_doc(node, approver, focus, "focus")
            node.feedback.append(Feedback(cycle=node.cycle, source="focus", text=focus,
                                          attestations=[att]))
        c.event("retrospective", f"{approver} asked for a retrospective (cycle {node.cycle})", RETRO)
        return f"retrospective queued (cycle {node.cycle})"

    def _reprovision(self, approver: str, payload: dict) -> str:
        """Build the provisioned environment again from the approved spec against the current
        library (G-PV1), e.g. after a library entry was fixed. The spec and its H-E1 approval
        stay; to change what the spec asks for, revise the plan instead. New runs of mapped roles
        wait from now on; provisioning starts once their live runs end and nothing is verifying
        or merging (the old build stays in place until the new one is accepted)."""
        c = self.c
        prov = c.nodes.get(PROVISION)
        if prov is None or c.change.environment is None or prov.status != ACCEPTED:
            raise WorkflowError("there is no provisioned environment to rebuild")
        if stale := governance.stale_environment(c):
            raise WorkflowError(f"{stale}; approve the new spec at H-E1 instead")
        reason = payload.get("reason")
        if not risk.valid_reason(reason):
            raise WorkflowError("a reason of ≥24 chars and more than one word is required")
        prov.status = PENDING
        c.event("provision", f"{approver} requested reprovisioning: {reason}", PROVISION)
        return ("provisioning again from the approved spec and the current library once running "
                "agents of mapped roles finish; their new runs wait until then")

    async def change_action(self, action: str, approver: str, payload: dict | None = None) -> ActionResult:
        """Change-level governance: attest, override, approve_protected_paths, approve_surface,
        promote_proposal (an approved environment proposal into the library), reprovision
        (rebuild the environment against the current library), retrospective (of an ended
        change)."""
        payload = payload or {}
        async with self.c.lock:
            self._check_approver(approver)
            try:
                if action == "attest":
                    a = await governance.record_attestation(self.c, approver, payload)
                    res = ActionResult(attestation=a.attestation, warnings=a.warnings)
                elif action == "override":
                    o = await governance.record_override(self.c, approver, payload)
                    res = ActionResult(attestation=o.attestation)
                elif action == "approve_protected_paths":
                    p = governance.record_protected_approval(self.c, approver, payload)
                    res = ActionResult(attestation=p.attestation,
                                       note="applies only to runs of this task that start after now")
                elif action == "promote_proposal":
                    res = ActionResult(note=self._promote(approver, payload))
                elif action == "reprovision":
                    res = ActionResult(note=self._reprovision(approver, payload))
                elif action == "retrospective":
                    res = ActionResult(note=self._start_retrospective(approver, payload))
                elif action == "approve_surface":
                    if not risk.valid_reason(payload.get("reason")):
                        raise governance.GovernanceError("a reason of ≥24 chars is required to re-affirm")
                    res = ActionResult(surfaces=await governance.pin_surfaces(
                        self.c, approver, payload.get("surface"), payload["reason"]))
                else:
                    raise WorkflowError(f"unknown change action {action!r}")
            except governance.GovernanceError as e:
                raise WorkflowError(str(e)) from None
            self.c.event("human_action", f"{approver}: {action}")
            self.c.save()
            return res
