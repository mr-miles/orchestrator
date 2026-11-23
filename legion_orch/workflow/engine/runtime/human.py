"""Human actions: decisions at gates and escalations (what approving a gate records is the
gate step's: :mod:`..steps.approvals`), and change-level governance."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ... import governance, risk
from ...environment import EnvironmentSpec, Kind, Library, LibraryError, promote
from ...flow import CHANGE
from ...model import (
    ACCEPTED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    PROVISION,
    RETRO,
    ChangeEnd,
    ChangeStatus,
    Feedback,
    FixRound,
    Node,
    Override,
)
from ...results import ActionResult
from ..base.common import WorkflowError, _slug
from ..base.core import ChangeCore
from ..base.ids import base_id
from ..services import cleanup
from ..services.runs import RunLifecycle

if TYPE_CHECKING:
    from ..steps.runner import StepRunner
    from .flow_runtime import FlowRuntime


class HumanGates:
    """What a decision owner may do to a change, checked and applied under the lock."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, runner: StepRunner):
        self.c, self.lifecycle, self.runner = core, lifecycle, runner
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
            elif node.status == AWAITING_HUMAN:
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
        """A gate waiting for a decision: re-checked (a retry), rejected (H-3: the step says
        whether it can be), or approved – what approving records is the gate step's
        (:meth:`.StepRunner.approve`); once the gate is approved, its escalation is answered
        and its step goes on. Sending work back is the workflow's generic send_back."""
        if action in ("recheck", "evaluate"):
            return await self._recheck(node, approver, payload)
        if action == "reject":
            if "reject" not in self.flow.actions(node.id):
                raise WorkflowError(f"{node.id} cannot be rejected")
            return await self._end_change(node, approver, payload, "reject")
        approved = await self.runner.approve(node, action, approver, payload)
        if approved.answered:
            await self.flow.decide(node, "answer", approver, "approved")   # its gate step goes on
        return approved.result

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
        await self.flow.act(node.id, f"send_back:{stage}", approver, text, feedback_doc=att)
        self.c.event("send_back", f"{approver}: {stage} sent back from {node.id}", node.id)
        return ActionResult()

    async def _recheck(self, node: Node, approver: str, payload: dict) -> ActionResult:
        """Check a gate's preconditions (H-3: the ship gates) again, after attesting or
        waiving: its step retries."""
        if self.c.change.pruned_at:
            raise WorkflowError("the change was pruned")
        await self.flow.decide(node, "retry", approver)     # its gate step checks again
        return ActionResult()

    async def _end_change(self, node: Node, approver: str, payload: dict,
                          action: str) -> ActionResult:
        """Abandon the change (at an escalation, or rejected at H-3): record who, where and
        the operator's reason, end it through the workflow (the core ends it; the runtime marks
        the change abandoned and the node it ended at), and run the retrospective on what the
        change produced so far (unless ``retrospective`` is false), whether or not
        retrospectives run automatically."""
        c = self.c
        reason = (payload.get("reason") or payload.get("feedback") or "").strip() or None
        rec = ChangeEnd(by=approver, node=node.id, action=action, reason=reason)
        rec.attestation = c.write_record("control/ended.json", rec).attestation
        c.change.ended = rec
        if action == "reject":
            await self.flow.act(node.id, "reject", approver, reason or "")
        else:
            await self.flow.act(CHANGE, "abandon", approver)
        self.runner.end(node)          # the change ended at this node
        verb = "abandoned the change" if action == "abandon" else "rejected the change"
        c.event("abandoned", f"{approver} {verb} at {node.id}: {reason or '(no reason given)'}",
                node.id)
        if payload.get("retrospective", True) is not False and RETRO not in c.nodes:
            self.flow.add_retrospective()
            c.event("retrospective", "the change was abandoned: retrospective queued", RETRO)
        return ActionResult(attestation=rec.attestation)

    # ------------------------------------------------------------------ escalations
    async def _resolve_escalation(self, node: Node, action: str, approver: str,
                                  payload: dict) -> ActionResult:
        """What a decision owner decides at an escalation: recorded here, carried out by the
        workflow (the core resumes the step; the step runner takes it up where it stopped)."""
        if action == "abandon":
            return await self._end_change(node, approver, payload, "abandon")
        if action in ("override_accept", "override"):
            reason = self._override_accept(node, approver, payload)
            await self.flow.decide(node, "override", approver, reason)
            return ActionResult(status=node.status)
        if action.startswith("rerun:"):
            reason = self._rerun(action.split(":", 1)[1], payload)
            await self.flow.decide(node, action, approver, reason)
            return ActionResult(status=node.status, note=(
                "provisioning again from the approved spec and the current library once running "
                "agents of mapped roles finish; this step is retried in the new build"))
        if action not in ("retry", "answer"):
            raise WorkflowError(f"unknown escalation action {action!r}")
        if node.escalation and node.escalation.retrying:
            raise WorkflowError("a retry is already in progress (removing the old sandbox)")
        text = ""
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
        await self.flow.decide(node, action, approver, text)
        if node.escalation and node.escalation.retrying:
            run = self.c.runs.get(node.escalation.run) if node.escalation.run else None
            return ActionResult(note=f"removing sandbox {run.sandbox if run else ''}, "
                                     "then retrying")
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
        await self.flow.act(node.id, f"send_back:{stage}", approver)
        self.c.event("fix_round", f"{approver}: fix round {n} planned on top of "
                     f"{(ch.integration_head or '')[:12]}", node.id)
        return ActionResult(note=f"fix round {n}: the planner plans it on top of the integration head")

    def _override_accept(self, node: Node, approver: str, payload: dict) -> str:
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
        return payload["reason"]

    def _promote(self, approver: str, payload: dict) -> str:
        """Copy a proposal of the H-E1-approved spec version into the library."""
        appr = self.c.change.approvals.get(H_ENV)
        path, version = governance.pinned(appr, governance.ENV_FILE)
        if version is None:
            raise WorkflowError("the environment spec is not approved (H-E1)")
        try:
            kind = Kind(payload.get("kind"))
        except ValueError:
            raise WorkflowError(f"kind must be one of {[k.value for k in Kind]}") from None
        spec = EnvironmentSpec.model_validate(self.c.json_doc(path, version))
        source = f"file:{path}@{version} (approved by {appr.approver}, " \
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
        node = self.flow.add_retrospective()
        focus = (payload.get("focus") or payload.get("feedback") or "").strip()
        if focus:
            att = self._feedback_doc(node, approver, focus, "focus")
            node.feedback.append(Feedback(cycle=node.cycle, source="focus", text=focus,
                                          attestations=[att]))
        c.event("retrospective", f"{approver} asked for a retrospective (cycle {node.cycle})", RETRO)
        return f"retrospective queued (cycle {node.cycle})"

    def _rerun(self, step: str, payload: dict) -> str:
        """An escalation answered by running an earlier step again in place – provisioning:
        the H-E1-approved spec built again against the current library (G-PV1), e.g. after a
        library entry was fixed. The spec and its approval stay; to change what the spec asks
        for, revise the plan instead. Returns the reason (≥24 chars, more than one word)."""
        c = self.c
        prov = c.nodes.get(step)
        if prov is None or prov.kind != PROVISION or c.change.environment is None \
                or prov.status != ACCEPTED:
            raise WorkflowError("there is no provisioned environment to rebuild")
        if stale := governance.stale_environment(c):
            raise WorkflowError(f"{stale}; approve the new spec at H-E1 instead")
        reason = (payload.get("reason") or "").strip()
        if not risk.valid_reason(reason):
            raise WorkflowError("a reason of ≥24 chars and more than one word is required")
        return reason

    async def change_action(self, action: str, approver: str, payload: dict | None = None) -> ActionResult:
        """Change-level governance: attest, override, approve_protected_paths, approve_surface,
        promote_proposal (an approved environment proposal into the library), retrospective (of an ended
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
