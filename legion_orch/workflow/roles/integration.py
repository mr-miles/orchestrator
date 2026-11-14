"""integration: whole-change verdict at the integration head (G-INT1, G-INT2), then the
controller re-runs the oracle and surface commands deterministically."""
from __future__ import annotations

from .. import guards
from ..model import READY, VERIFYING
from .base import Report, RoleHandler, register


@register
class Integration(RoleHandler):
    roles = ("integration",)

    async def guards(self, e, r: Report) -> list[str]:
        v = guards.verdict(r.data, pass_word="PASS", fail_word="FAIL", gid="G-INT1", cycle=2,
                           require_checked=False)
        v += [x.replace("G-RV2", "G-INT1") for x in guards.findings(r.data, r.repo_atts)]
        v += guards.results_cover(r.data, e._oracle_commands(), "G-INT1")
        return v + await e._protected_unchanged(r.sha)

    async def on_done(self, e, r: Report) -> None:
        node, data = r.node, r.data
        node.reviews["integration"] = {"verdict": data["verdict"], "sha": r.sha,
                                       "findings": data.get("findings", []), "run": r.run.id}
        if data["verdict"] != "PASS":
            e._escalate(node, "integration_failed", "integration verifier reported FAIL",
                        resume=READY, run=r.run.id)
            return
        node.candidate_sha = r.sha
        node.status = VERIFYING
        e._spawn(e._verify_node(node.id))
