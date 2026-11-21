"""Human actions: the H-1/H-E1/H-2/H-3 gates, escalation resolution, change-level governance."""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import governance, guards, risk, setup
from ..environment import EnvironmentSpec, Kind, Library, LibraryError, promote
from ..flow import CHANGE
from ..model import (
    ACCEPTED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    H_CHANGE,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    LIVE_RUN,
    MERGING,
    PROVISION,
    R_REPORTED,
    R_SUPERSEDED,
    READY,
    RETRO,
    REVIEWING,
    REWORK,
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
from . import cleanup
from .acceptance import Acceptance
from .common import ENV_SPEC, WorkflowError, _slug
from .core import ChangeCore
from .graph import StageGraph
from .runs import RunLifecycle
from .workflow_tree import base_id

if TYPE_CHECKING:
    from .runtime import FlowRuntime


class HumanGates:
    """What a decision owner may do to a change, checked and applied under the lock."""

    def __init__(self, core: ChangeCore, graph: StageGraph, acceptance: Acceptance,
                 lifecycle: RunLifecycle):
        self.c, self.graph, self.acceptance, self.lifecycle = core, graph, acceptance, lifecycle
        self.flow: FlowRuntime = None  # type: ignore[assignment]   # set once it exists

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
            action, payload = self._generic(node, action, payload)
            if action == "send_back" and node.status in (ESCALATED, AWAITING_HUMAN):
                res = await self._send_back(node, payload.get("stage") or "", approver, payload)
            elif node.status == ESCALATED:
                res = await self._resolve_escalation(node, action, approver, payload)
            elif node.kind == HUMAN and node.status == AWAITING_HUMAN:
                res = await self._human_gate(node, action, approver, payload)
            else:
                raise WorkflowError(f"action {action!r} not available for {node_id} in {node.status}")
            self.c.event("human_action", f"{approver}: {action}", node_id)
            self.c.save()
            res.status = node.status
            return res

    # Today's names for the workflow's generic send-back (the CLI, links, older clients): the
    # stage they send back, by gate (``*``: any node).
    SEND_BACK_ALIASES = {
        (H_REQUIREMENTS, "revise"): "requirements",
        (H_ENV, "revise"): "design",
        (H_SPEC, "revise"): "oracle",
        (H_SPEC, "restart"): "plan",
        ("*", "revise_plan"): "design",
        ("*", "plan_fix"): "delivery",
    }

    def _generic(self, node: Node, action: str, payload: dict) -> tuple[str, dict]:
        """An alias as the generic action it names."""
        stage = self.SEND_BACK_ALIASES.get((base_id(node.id), action)) \
            or self.SEND_BACK_ALIASES.get(("*", action))
        if stage is None:
            return action, payload
        if action == "plan_fix" and not (node.escalation
                                         and node.escalation.kind == "integration_failed"):
            raise WorkflowError("plan_fix is only available when integration failed")
        return "send_back", {**payload, "stage": stage}

    def _feedback_doc(self, node: Node, approver: str, text: str, kind: str) -> str:
        n = len([f for f in node.feedback if f.source == kind]) + 1
        path = f"tasks/{node.task.id}/{kind}/{n}.md" if node.task \
            else f"control/{kind}/{_slug(node.id)}-{n}.md"
        return self.c.write_record(path, f"# {kind} for {node.id}\n\nBy: {approver}\n\n{text}\n").attestation

    # ------------------------------------------------------------------ human gates
    async def _human_gate(self, node: Node, action: str, approver: str, payload: dict) -> ActionResult:
        handler = self.GATE_ACTIONS.get((base_id(node.id), action))
        if handler is None:
            valid = [a for g, a in self.GATE_ACTIONS if g == base_id(node.id)] + ["send_back"]
            raise WorkflowError(f"action {action!r} not valid for {node.id} (one of {valid})")
        return await getattr(self, handler)(node, approver, payload)

    # (gate, action) -> method(node, approver, payload): what approving each gate decides (its
    # subjects, the tier, the attestations), and H-3's reject and recheck. Sending work back is
    # the workflow's generic send_back.
    GATE_ACTIONS = {
        (H_REQUIREMENTS, "accept"): "_accept_requirements",
        (H_REQUIREMENTS, "approve"): "_accept_requirements",
        (H_ENV, "approve"): "_approve_plan",
        (H_SPEC, "approve"): "_approve_oracle",
        (H_CHANGE, "accept"): "_accept_change",
        (H_CHANGE, "approve"): "_accept_change",
        (H_CHANGE, "reject"): "_reject_change",
        (H_CHANGE, "recheck"): "_recheck",
        (H_CHANGE, "evaluate"): "_recheck",
    }

    def _stage(self, node: Node, name: str) -> str:
        """The stage ``name`` (requirements, plan, design, oracle, delivery) ``node``'s work was
        built on – in a fix round, the round's own."""
        for s in self.flow.built_on(node.id):
            if base_id(s) == f"stage:{name}" or s == f"stage:{name}":
                return s
        raise WorkflowError(f"{node.id} was not built on a {name} stage")

    async def _send_back(self, node: Node, stage: str, approver: str, payload: dict) -> ActionResult:
        """Send the work of ``stage`` back with feedback (``stage``: a stage id or name): the
        stage's rework policy runs, and everything built on it starts over. Delivery's appends a
        fix round (§5.10a)."""
        text = (payload.get("feedback") or payload.get("answer") or payload.get("comment")
                or "").strip()
        if not stage.startswith("stage:"):
            stage = self._stage(node, stage)
        if f"send_back:{stage}" not in self.flow.actions(node.id):
            raise WorkflowError(f"{node.id} cannot send {stage} back (it may: "
                                f"{[a for a in self.flow.actions(node.id) if a.startswith('send_back:')]})")
        if base_id(stage) == "stage:delivery":
            return await self._fix_round(node, stage, approver, text or None)
        if not text:
            raise WorkflowError("feedback is required")
        att = self._feedback_doc(node, approver, text, "feedback")
        node.escalation = None
        await self.flow.act(node.id, f"send_back:{stage}", approver, text, feedback_doc=att)
        self.c.event("send_back", f"{approver}: {stage} sent back from {node.id}", node.id)
        return ActionResult()

    async def _accept_requirements(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-1: accept requirements and the risk tier (raising it is free; lowering needs a
        reason). Fixes the change's requirements and review panel; the tier decides the rest of
        the workflow's stages."""
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
        self.flow.build_tree()
        await self.flow.act(node.id, "approve", approver)
        return ActionResult(tier=tier, panel=ch.panel)

    async def _approve_subjects(self, node: Node, approver: str, payload: dict,
                                gate_subjects: list[str], baseline: bool) -> ActionResult:
        """Approve some (or "all") of the gate's subjects – separate decisions, possibly by
        different approvers; the gate is approved once every one is approved and fresh."""
        subjects = payload.get("subjects") or "all"
        everything = subjects == "all" or subjects == ["all"]
        wanted = gate_subjects if everything else [s for s in subjects if s in gate_subjects]
        if not everything and (unknown := [s for s in subjects if s not in gate_subjects
                                          and s != governance.BASELINE_SUBJECT]):
            raise WorkflowError(f"unknown approval subject(s) {unknown}; choose from "
                                f"{gate_subjects}")
        base = governance.baseline_subject(self.c) if baseline else None
        attest = base is not None and base.status != ApprovalState.APPROVED and (
            everything or governance.BASELINE_SUBJECT in subjects)
        if attest and (base.note or not base.sources):
            raise WorkflowError(f"independent-baseline: {base.note or 'no baseline evidence'}")
        env = governance.ENVIRONMENT_SUBJECT in wanted
        wanted = [s for s in wanted if s != governance.ENVIRONMENT_SUBJECT]
        try:
            done = await governance.approve_subjects(self.c, wanted, approver) if wanted else []
            if env:
                await self._approve_environment(node, approver, payload)
                done.append(governance.ENVIRONMENT_SUBJECT)
            if attest:
                # R3: the baseline attestation is decided here with the other subjects, citing
                # the baseline report and the controller's own run of its commands (G-AT1)
                await governance.record_attestation(self.c, approver, {
                    "kind": governance.BASELINE_SUBJECT, "verdict": "pass",
                    "sources": base.sources})
                done.append(governance.BASELINE_SUBJECT)
        except governance.GovernanceError as e:
            raise WorkflowError(str(e)) from None
        status = {x.subject: x.status for x in governance.subject_status(self.c)}
        pending = [s for s in gate_subjects if status.get(s) != ApprovalState.APPROVED]
        base = governance.baseline_subject(self.c) if baseline else None
        if base is not None and base.status != ApprovalState.APPROVED:
            pending.append(governance.BASELINE_SUBJECT)
        if pending:
            return ActionResult(approved=done, pending=pending)
        await self.flow.act(node.id, "approve", approver)
        return ActionResult(approved=done, pending=[])

    async def _approve_plan(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-E1 "approve the plan": the spec and task graph, each delta spec and the sandbox
        environment (if the controller has one)."""
        return await self._approve_subjects(node, approver, payload,
                                            governance.plan_subjects(self.c), baseline=False)

    async def _approve_oracle(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-2 "approve the oracle" (and, at R3, attest the independent baseline, which is the
        gate's precondition)."""
        return await self._approve_subjects(node, approver, payload,
                                            governance.oracle_subjects(self.c), baseline=True)

    async def _approve_environment(self, node: Node, approver: str, payload: dict) -> None:
        """The sandbox environment at its current version (with the plan, at H-E1);
        provisioning follows."""
        version = self.c.latest(ENV_SPEC)
        if version is None:
            raise WorkflowError(f"no {ENV_SPEC} to approve")
        note = (payload.get("feedback") or "").strip() or None
        rec = Approval(gate=H_ENV, approver=approver, pins={self.c.p(ENV_SPEC): version}, note=note)
        rec.attestation = self.c.write_record("control/approvals/environment.json", rec).attestation
        self.c.change.approvals[H_ENV] = rec
        await self._adopt_mise(EnvironmentSpec.model_validate(self.c.json_doc(ENV_SPEC, version)))

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

    async def _accept_change(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """H-3: accept the whole change at the current integration head. The gate completes once
        its preconditions – the tier's ship gates – hold (evaluated now, again on recheck)."""
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
        await self.flow.act(node.id, "approve", approver)
        return ActionResult(warnings=[f"{g} has no passing attestation: H-3 waits for it"
                                      for g in missing])

    async def _recheck(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """Evaluate H-3's preconditions (the ship gates) again, after attesting or waiving."""
        if self.c.change.pruned_at:
            raise WorkflowError("the change was pruned")
        await self.flow.act(node.id, "evaluate", approver)
        return ActionResult()

    async def _reject_change(self, node: Node, approver: str, payload: dict) -> ActionResult:
        res = self._end_change(node, approver, payload, "reject")
        await self.flow.act(node.id, "reject", approver,
                            (payload.get("reason") or payload.get("feedback") or "").strip())
        return res

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
            res = self._end_change(node, approver, payload, "abandon")
            await self.flow.act(CHANGE, "abandon", approver)
            return res
        if action in ("override_accept", "override"):
            return self._override_accept(node, approver, payload)
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
            esc = node.escalation
            # the decision stands on its own: the question it answers, then the answer – an
            # answer like "the last option" means nothing to the next run without it
            question = esc.missing_decision or esc.reason if esc else ""
            body = (f"## Question ({esc.kind})\n\n{question}\n\n## Answer\n\n{text}"
                    if esc else text)
            att = self._feedback_doc(node, approver, body, "decisions")
            node.feedback.append(Feedback(
                cycle=node.cycle, source="decisions", attestations=[att],
                head=self.c.change.integration_head,
                text=f"human decision on \"{question[:300]}\": {text}" if question
                else f"human decision: {text}"))
        node.escalation = None
        self._resume(node, resume)
        return ActionResult(status=node.status)

    async def _fix_round(self, node: Node, stage: str, approver: str,
                         comment: str | None) -> ActionResult:
        """Delivery sent back (§5.10a, from a failed integration: Plan a fix): its policy
        appends a fix round that plans new work on top of the integration head (planner,
        critique, H-E1, the oracle again if it changes, H-2, new build tasks), then integration
        runs again. Accepted tasks are never reopened. The round's record, with the failure, is
        what the planner is told (``fix_round``)."""
        ch, limit = self.c.change, self.c.cfg.limits.fix_rounds
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
                       comment=comment)
        rec.attestation = self.c.write_record(f"control/rounds/{n}.json", rec).attestation
        ch.round = n
        ch.rounds.append(rec)
        node.escalation = None
        await self.flow.act(node.id, f"send_back:{stage}", approver)
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
        ok = await self.lifecycle.sandboxes.delete(sandbox) if sandbox else True
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
        if "override" not in self.flow.actions(node.id):     # the step allows it: build tasks
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
        prov = c.current(PROVISION)
        if prov is None or c.change.environment is None or prov.status != ACCEPTED:
            raise WorkflowError("there is no provisioned environment to rebuild")
        if stale := governance.stale_environment(c):
            raise WorkflowError(f"{stale}; approve the new spec at H-E1 instead")
        reason = payload.get("reason")
        if not risk.valid_reason(reason):
            raise WorkflowError("a reason of ≥24 chars and more than one word is required")
        self.flow.runner.enter(prov)       # its wait: no run or verification uses the build
        c.event("provision", f"{approver} requested reprovisioning: {reason}", prov.id)
        return ("provisioning again from the approved spec and the current library once running "
                "agents of mapped roles finish; their new runs wait until then")

    async def change_action(self, action: str, approver: str, payload: dict | None = None) -> ActionResult:
        """Change-level governance: attest, override, approve_protected_paths, approve_surface,
        promote_proposal (an approved environment proposal into the library), reprovision
        (rebuild the environment against the current library), retrospective (of an ended
        change), prune (an ended change's integration branch and evidence refs, §5.14)."""
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
                elif action == "prune":
                    res = ActionResult(note=await cleanup.prune(self.c, approver))
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
