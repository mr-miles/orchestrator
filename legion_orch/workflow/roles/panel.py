"""Review panel (reviewer, security, architecture): verdict and findings on the candidate
(G-RV1..G-RV4); the panel outcome is decided once every member has reported (G-RV5)."""
from __future__ import annotations

from .. import guards
from ..model import R_ACCEPTED
from ..view import RoleContext
from .base import Report, RoleHandler, build_context, evidence_of, register


@register
class Panel(RoleHandler):
    roles = ("reviewer", "security", "architecture")
    judge = True
    panel = True

    def extra_context(self, e: RoleContext, node) -> list[str]:
        refs = build_context(e, node)
        ev = evidence_of(e, node)
        ver = node.verification.get(str(node.cycle))
        return refs + [a for a in (ev, ver and ver.get("attestation")) if a]

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.findings(r.data, r.repo_atts) + guards.verdict(
            r.data, pass_word="PASS", fail_word="FAIL", gid="G-RV3", cycle=r.run.cycle,
            require_checked=True)

    async def on_done(self, e: RoleContext, r: Report) -> None:
        run, node, data = r.run, r.node, r.data
        node.reviews[f"{run.cycle}:{run.role}"] = {
            "verdict": data["verdict"], "sha": run.judged_sha, "run": run.id,
            "findings": data.get("findings", []), "checked": data.get("checked", []),
            "doc": str(r.docs[run.required_docs[0]])}
        run.status = R_ACCEPTED
        e.delete_sandbox_later(run.sandbox)
        e.panel_complete(node)
