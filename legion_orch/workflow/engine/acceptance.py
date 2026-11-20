"""Acceptance: verification, the review panel's decision, rework, merge and ship readiness."""
from __future__ import annotations

from ..model import (
    ORACLE,
    PENDING,
    REWORK,
    VERIFYING,
    CommandResult,
    Feedback,
    Node,
)
from . import cleanup
from .core import ChangeCore
from .phases import (
    Escalate,
    Merge,
    Ok,
    PhaseContext,
    Verify,
    setup_failed_text,
)
from .runs import RunLifecycle
from .steps import StepRunner, step_for
from .workflow_tree import in_round


class Acceptance:
    """How a candidate becomes accepted work: deterministic verification (G-V1), the review
    panel's decision, rework within the cycle budget, the merge (G-A1, G-A2). The work is done
    by phases (:mod:`.phases`); this says what their results mean for each kind of node."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, ctx: PhaseContext):
        self.c, self.lifecycle, self.ctx = core, lifecycle, ctx
        self.runner: StepRunner = None  # type: ignore[assignment]   # set once the runner exists

    async def verify_node(self, node_id: str) -> None:
        """Verify the node's candidate with its step's Verify phase, which then says what the
        result means (the next phase, rework, or escalate)."""
        c, node = self.c, self.c.nodes[node_id]
        phase = step_for(node).phase(Verify)
        assert isinstance(phase, Verify), f"{node.kind} is not verified"
        commands = phase.commands_for(c, node)
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))",
                node_id)
        res = await phase.verification(self.ctx, node_id, node.candidate_sha, commands)
        async with c.lock:
            if node.status != VERIFYING:
                return
            path, key = phase.record(node)
            att = c.write_record(path, res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            node.verification[key] = res.model_copy(update={
                "attestation": att,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            phase.conclude(self.runner, node, res, att)
            c.save()

    def setup_failed(self, node: Node, r: CommandResult, att: str, escalation: Escalate) -> None:
        """G-V3 at preflight: the repository's setup failed, so nothing after it ran. The
        oracle's author is sent the output (one oracle cycle; fixing it may need the plan's
        environment, which it then reports BLOCKED); without a cycle left, a human decides. (At
        the baseline a human decides; in a build or at integration the tasks may have caused
        it: it fails like any command there.)"""
        c = self.c
        oracle = c.nodes.get(in_round(ORACLE, node.round)) or c.current(ORACLE)
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
            c.event("rework", f"preflight: {what}: oracle rework cycle {oracle.cycle}", oracle.id)
            return
        self.ctx.apply(node, escalation)

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
            merge = step_for(node).phase(Merge)
            assert isinstance(merge, Merge)
            result = await merge.run(self.ctx, node)
            if not isinstance(result, Ok):
                self.ctx.apply(node, result)
                c.save()
                return
            node.accepted_sha = sha
            run = c.runs.get(node.current_run) if node.current_run else None
            await cleanup.delete_run_branch(c, run.branch if run else None, node_id)
            c.event("accepted", f"merged {sha[:12]} -> integration {result.value[:12]}", node_id)
            self.runner.advance(node, merge)  # accepted; its sandbox goes (principle 8)
            c.save()
