"""JSON views of the controller for the operator UI (presentation only: read, never mutate)."""
from __future__ import annotations

from ..workflow import governance
from ..workflow.engine import ChangeEngine, Controller
from ..workflow.engine.common import ENV_SPEC
from ..workflow.environment import EnvironmentSpec, Library, LibraryError, resolve
from ..workflow.model import ACCEPTED, H_ENV, LIVE_RUN, RETRO


def change_list(ctl: Controller) -> list[dict]:
    out = []
    for ce in ctl.changes.values():
        c = ce.change
        out.append({"id": c.id, "goal": c.goal, "status": c.status, "tier": c.tier,
                    "created_at": c.created_at,
                    "accepted": sum(n.status == ACCEPTED for n in ce.nodes.values()),
                    "nodes": len(ce.nodes),
                    "live_runs": sum(r.status in LIVE_RUN for r in ce.runs.values()),
                    "retrospective": ce.nodes[RETRO].status if RETRO in ce.nodes else None,
                    "needs_you": sum(i.blocking for i in ce.attention_items)})
    return sorted(out, key=lambda c: c["created_at"], reverse=True)


def environment_spec(ce: ChangeEngine) -> dict | None:
    """The latest environment spec for the H-E1 card: the proposals its profiles use and each
    profile's effective network (what its tools bring plus extras), or the problems."""
    if H_ENV not in ce.nodes or not ce.latest(ENV_SPEC):
        return None
    raw = ce.json_doc(ENV_SPEC)
    spec = EnvironmentSpec.model_validate(raw)
    try:
        profiles, problems = resolve(spec, Library.for_config(ce.cfg, ce.assets.schemas))
        network = {p: [{"policy": x.name, "tools": list(x.tools),
                        "hosts": [f"{e.host}:{e.port}" for e in x.endpoints]}
                       for x in rp.policies] for p, rp in profiles.items()}
    except LibraryError as e:
        network, problems = {}, [str(e)]
    return {"path": ce.p(ENV_SPEC), "version": ce.latest(ENV_SPEC), "spec": raw,
            "proposals": {k.plural: v for k, v in spec.used_proposals().items()},
            "network": network, "problems": problems}


def change_state(ctl: Controller, ce: ChangeEngine) -> dict:
    """Everything the UI shows for one change. Run assignments (the rendered prompt variables)
    are left out; node feedback is counted, not copied."""
    ch, cfg = ce.change, ctl.cfg
    nodes = [n.model_dump(mode="json") | {"feedback_count": len(n.feedback)}
             for n in ce.nodes.values()]
    runs = [r.model_dump(mode="json", exclude={"assignment"}) for r in ce.runs.values()]
    return {"project": cfg.project, "repo": cfg.repo.name, "decision_owners": cfg.decision_owners,
            "assets_sha": ctl.assets.sha, "limits": cfg.limits.__dict__,
            "change": ch.model_dump(mode="json") if ch else None, "nodes": nodes, "runs": runs,
            "gates": governance.gate_set(ce) if ch and ch.tier else [],
            "h2": [s.model_dump() for s in governance.h2_status(ce)] if ch and ch.requirements else [],
            "surfaces": [s.model_dump() for s in governance.surfaces(ce)] if ch else [],
            "environment": environment_spec(ce),
            "attestation_kinds": sorted(governance.ATTESTATION_KINDS)}


def _item(i) -> dict:
    return i.model_dump(mode="json") | {"key": i.key}


def attention(ctl: Controller) -> dict:
    """Every change's attention items (WORKFLOW.md §2.2): ``needs_you`` (blocking: a gate or an
    escalation waits for a decision owner) and ``inbox`` (to read: ended changes, retrospective
    reports; the UI keeps which were read)."""
    items = ctl.attention_items()
    return {"needs_you": [_item(i) for i in items if i.blocking],
            "inbox": [_item(i) for i in items if not i.blocking]}


# What the "while you were away" digest counts, by timeline event kind (no model call).
DIGEST_KINDS = {"dispatch": "runs started", "report": "reports accepted",
                "report_rejected": "reports rejected", "run_failed": "runs failed",
                "rework": "rework cycles", "accepted": "tasks merged",
                "escalation": "escalations", "human_action": "human decisions",
                "fix_round": "fix rounds", "ship": "ship checks", "abandoned": "abandoned"}


def digest(ctl: Controller, since: str) -> dict:
    """What happened since ``since`` (ISO), per change, from the timelines; changes with no
    events are left out. What waits for a decision is never folded into the counts: it is
    listed in full (``needs_you``)."""
    changes = []
    for ce in ctl.changes.values():
        evs = ce.timeline_events(since)
        counts: dict[str, int] = {}
        for ev in evs:
            label = DIGEST_KINDS.get(ev.get("kind"))
            if label:
                counts[label] = counts.get(label, 0) + 1
        if evs:
            ch = ce.change
            changes.append({"id": ce.cid, "goal": ch.goal if ch else "",
                            "status": ch.status if ch else None, "events": len(evs),
                            "counts": counts})
    att = attention(ctl)
    return {"since": since, "changes": changes, "needs_you": att["needs_you"],
            "inbox": [i for i in att["inbox"] if i["since"] >= since]}
