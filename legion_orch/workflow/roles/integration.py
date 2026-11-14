"""integration: whole-change verdict at the integration head (G-INT1, G-INT2), then the
controller re-runs the oracle and surface commands deterministically."""
from __future__ import annotations

from .. import guards
from ..model import READY, VERIFYING
from ..view import RoleContext
from .base import Report, RoleHandler, evidence_of, register


@register
class Integration(RoleHandler):
    roles = ("integration",)
    judge = True
    judges_head = True

    def extra_context(self, e: RoleContext, node) -> list[str]:
        return [a for a in (evidence_of(e, b) for b in e.build_nodes()) if a]

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        v = guards.verdict(r.data, pass_word="PASS", fail_word="FAIL", gid="G-INT1", cycle=2,
                           require_checked=False)
        v += [x.replace("G-RV2", "G-INT1") for x in guards.findings(r.data, r.repo_atts)]
        v += guards.results_cover(r.data, e.oracle_commands(), "G-INT1")
        return v + await e.protected_unchanged(r.sha)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        node, data = r.node, r.data
        node.reviews["integration"] = {"verdict": data["verdict"], "sha": r.sha,
                                       "findings": data.get("findings", []), "run": r.run.id}
        if data["verdict"] != "PASS":
            e.escalate(node, "integration_failed", "integration verifier reported FAIL",
                        resume=READY, run=r.run.id)
            return
        node.candidate_sha = r.sha
        node.status = VERIFYING
        e.start_verification(node.id)
