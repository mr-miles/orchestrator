"""JSON views of the controller for the operator UI (presentation only: read, never mutate)."""
from __future__ import annotations

from ..workflow import governance
from ..workflow.engine import ChangeEngine, Controller
from ..workflow.model import ACCEPTED, LIVE_RUN


def change_list(ctl: Controller) -> list[dict]:
    out = []
    for ce in ctl.changes.values():
        c = ce.change
        out.append({"id": c.id, "goal": c.goal, "status": c.status, "tier": c.tier,
                    "created_at": c.created_at,
                    "accepted": sum(n.status == ACCEPTED for n in ce.nodes.values()),
                    "nodes": len(ce.nodes),
                    "live_runs": sum(r.status in LIVE_RUN for r in ce.runs.values())})
    return sorted(out, key=lambda c: c["created_at"], reverse=True)


def change_state(ctl: Controller, ce: ChangeEngine) -> dict:
    """Everything the UI shows for one change. Run assignments (the rendered prompt variables)
    are left out; node feedback is counted, not copied."""
    ch, cfg = ce.change, ctl.cfg
    nodes = [n.model_dump(mode="json") | {"feedback_count": len(n.feedback)}
             for n in ce.nodes.values()]
    runs = [r.model_dump(mode="json", exclude={"assignment"}) for r in ce.runs.values()]
    return {"project": cfg.project, "repo": cfg.repo.name, "decision_owners": cfg.decision_owners,
            "version": ctl.version, "assets_sha": ctl.assets.sha, "limits": cfg.limits.__dict__,
            "change": ch.model_dump(mode="json") if ch else None, "nodes": nodes, "runs": runs,
            "gates": governance.gate_set(ce) if ch and ch.tier else [],
            "h2": [s.model_dump() for s in governance.h2_status(ce)] if ch and ch.requirements else [],
            "surfaces": [s.model_dump() for s in governance.surfaces(ce)] if ch else [],
            "attestation_kinds": sorted(governance.ATTESTATION_KINDS)}
