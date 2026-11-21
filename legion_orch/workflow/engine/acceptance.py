"""Acceptance: verification, the review panel's decision, rework, merge and ship readiness."""
from __future__ import annotations

from ..model import (
    ACCEPTED,
    FAILED,
    MERGING,
    ORACLE,
    PENDING,
    REWORK,
    SHIP,
    VERIFYING,
    ChangeStatus,
    CommandResult,
    Feedback,
    Node,
    ReadinessVerdict,
)
from . import cleanup
from .core import ChangeCore
from .phases import (
    Escalate,
    Evaluate,
    Merge,
    Ok,
    Panel,
    PhaseContext,
    setup_failed_text,
)
from .runs import RunLifecycle
from .steps import step_for


class Acceptance:
    """How a candidate becomes accepted work: deterministic verification (G-V1), the review
    panel's decision, rework within the cycle budget, the merge (G-A1, G-A2) – and, for the
    change as a whole, the ship readiness check. The work is done by phases (:mod:`.phases`);
    this says what their results mean for each kind of node."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, ctx: PhaseContext):
        self.c, self.lifecycle, self.ctx = core, lifecycle, ctx

    def start_verification(self, node_id: str) -> None:
        """Run the node's deterministic verification in the background (node is VERIFYING)."""
        self.c.spawn(self.verify_node(node_id))

    async def verify_node(self, node_id: str) -> None:
        """Verify the node's candidate; its step type says what the result means (review or
        merge, accept, rework, or escalate)."""
        c, node = self.c, self.c.nodes[node_id]
        step = step_for(node)
        phase = step.verify(c, node)
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(phase.commands)} command(s))",
                node_id)
        res = await phase.verification(self.ctx, node_id, node.candidate_sha)
        async with c.lock:
            if node.status != VERIFYING:
                return
            path, key = step.verification_record(node)
            att = c.write_record(path, res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            node.verification[key] = res.model_copy(update={
                "attestation": att,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            step.verified(self, node, res, phase.judge(res, node.cycle, att), att)
            c.save()

    def setup_failed(self, node: Node, r: CommandResult, att: str, escalation: Escalate) -> None:
        """G-V3 at preflight: the repository's setup failed, so nothing after it ran. The
        oracle's author is sent the output (one oracle cycle; fixing it may need the plan's
        environment, which it then reports BLOCKED); without a cycle left, a human decides. (At
        the baseline a human decides; in a build or at integration the tasks may have caused
        it: it fails like any command there.)"""
        c = self.c
        oracle = c.nodes.get(ORACLE)
        limit = c.cfg.limits.review_max_cycles + (oracle.extra_cycles if oracle else 0)
        if oracle is not None and oracle.cycle < limit:
            what = setup_failed_text(r)
            oracle.cycle += 1
            oracle.feedback.append(Feedback(
                cycle=oracle.cycle, source="verification", attestations=[att],
                text=f"BLOCKER (preflight): {what}. Output: {r.output[-800:]}. The acceptance "
                     "commands must run in a fresh clone after only the repository's setup "
                     "(mise.toml's setup task). Fix your tests or support files; if the fix is in "
                     "the setup (the plan's environment) or plan/oracle.json, report BLOCKED with "
                     "it as missing_decision."))
            oracle.candidate_sha = None
            oracle.status = REWORK
            node.status = PENDING        # runs again once the oracle is accepted
            c.event("rework", f"preflight: {what}: oracle rework cycle {oracle.cycle}", ORACLE)
            return
        self.ctx.apply(node, escalation)

    def panel_complete(self, node: Node) -> None:
        """Once every panel member has reported: merge on a unanimous PASS at the candidate,
        otherwise rework with the HIGH findings (or escalate a stale loop, G-RV5)."""
        result = Panel(tuple(self.c.change.panel)).decide(node)
        if result is None:
            return
        if isinstance(result, Ok):
            node.status = MERGING
            self.c.spawn(self.merge_node(node.id))
            return
        self.ctx.apply(node, result)

    def rework(self, node: Node, feedback: list[Feedback], source: str) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5)."""
        self.ctx.rework(node, feedback, source)

    async def merge_node(self, node_id: str) -> None:
        """G-A1 (verification and every panel PASS at the candidate, unless overridden), then
        merge into the integration branch; a conflict is rework (G-A2)."""
        c = self.c
        async with c.lock:
            node = c.nodes[node_id]
            sha = node.candidate_sha
            result = await Merge(tuple(c.change.panel)).run(self.ctx, node)
            if not isinstance(result, Ok):
                self.ctx.apply(node, result)
                c.save()
                return
            node.accepted_sha = sha
            node.status = ACCEPTED
            run = c.runs.get(node.current_run) if node.current_run else None
            await cleanup.delete_run_branch(c, run.branch if run else None, node_id)
            c.event("accepted", f"merged {sha[:12]} -> integration {result.value[:12]}", node_id)
            self.lifecycle.accept_runs(node)  # deletes the implementer sandbox (principle 8)
            c.save()

    async def ship(self) -> None:
        """The ship readiness check (workflow/ship.py); its verdict is the change's status."""
        result = (await Evaluate().run(self.ctx)).value
        c = self.c
        async with c.lock:
            result.attestation = c.write_record("control/ship/readiness.json", result).attestation
            c.change.ship = result
            c.change.status = ChangeStatus(result.verdict)
            # the node shows the verdict: NOT_READY waits for a human (attest, approve, re-run)
            c.nodes[SHIP].status = ACCEPTED if result.verdict == ReadinessVerdict.READY else FAILED
            c.event("ship", f"readiness verdict: {result.verdict}", SHIP)
            c.save()
