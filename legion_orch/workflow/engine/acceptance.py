"""Acceptance: verification, the review panel's decision, rework, merge and ship readiness."""
from __future__ import annotations

from ..model import (
    VERIFYING,
)
from . import cleanup
from .core import ChangeCore
from .phases import (
    Merge,
    Ok,
    PhaseContext,
    Verify,
)
from .runs import RunLifecycle
from .steps import StepRunner, step_for


class Acceptance:
    """How a candidate becomes accepted work: deterministic verification (G-V1), the review
    panel's decision, the merge (G-A1, G-A2): the background work of the Verify and Merge
    phases, whose results go back through those phases (:mod:`.phases`)."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, ctx: PhaseContext):
        self.c, self.lifecycle, self.ctx = core, lifecycle, ctx
        self.runner: StepRunner = None  # type: ignore[assignment]   # set once the runner exists

    async def verify_node(self, node_id: str, *, reserved: bool = False) -> None:
        """Verify the node's candidate with its step's Verify phase, which then says what the
        result means (the next phase, rework, or escalate)."""
        c, node = self.c, self.c.nodes[node_id]
        phase = step_for(node).phase(Verify)
        assert isinstance(phase, Verify), f"{node.kind} is not verified"
        commands = phase.commands_for(c, node)
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))",
                node_id)
        res = await phase.verification(self.ctx, node_id, node.candidate_sha, commands,
                                       reserved=reserved)
        async with c.lock:
            if node.status != VERIFYING:
                return
            path, key = phase.record(node)
            att = c.write_record(path, res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            node.verification[key] = res.model_copy(update={
                "attestation": att,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            phase.verified(self.runner, node, res, att)     # the phase's result, to its step
            c.save()

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
            if isinstance(result, Ok):
                node.accepted_sha = sha
                run = c.runs.get(node.current_run) if node.current_run else None
                await cleanup.delete_run_branch(c, run.branch if run else None, node_id)
                c.event("accepted", f"merged {sha[:12]} -> integration {result.value[:12]}",
                        node_id)
            merge.conclude(self.runner, node, result)   # the phase's result, to its step
            c.save()
