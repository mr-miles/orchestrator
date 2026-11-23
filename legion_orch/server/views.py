"""JSON views of the controller for the operator UI (presentation only: read, never mutate)."""
from __future__ import annotations

import difflib

from ..git import GitError
from ..store import DocStore
from ..workflow import governance, ship, traceability
from ..workflow.engine import ChangeEngine, Controller
from ..workflow.environment import EnvironmentSpec, Library, LibraryError, resolve
from ..workflow.governance.approvals import ENV_SPEC
from ..workflow.model import ACCEPTED, H_ENV, H_SPEC, LIVE_RUN, RETRO


def change_list(ctl: Controller) -> list[dict]:
    out = []
    for ce in ctl.changes.values():
        c = ce.change
        out.append({"id": c.id, "workspace": c.workspace, "goal": c.goal,
                    "status": c.status, "tier": c.tier,
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
    if ce.cfg.environment is None or not ce.latest(ENV_SPEC):
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


def h2_rows(ce: ChangeEngine) -> list[dict]:
    """The approval decisions, each with the gate that makes it: H-E1 "approve the plan" (the
    spec and task graph, each delta spec, the environment) and H-2 "approve the oracle" (the
    oracle and, at R3, the baseline attestation, its precondition)."""
    oracle = set(governance.oracle_subjects(ce))
    rows = [s.model_dump() | {"kind": "approval", "gate": H_SPEC if s.subject in oracle else H_ENV}
            for s in governance.subject_status(ce)]
    base = governance.baseline_subject(ce)
    return rows + ([base.model_dump() | {"gate": H_SPEC}] if base else [])


def workflow_state(ce: ChangeEngine) -> dict:
    """The stage rules' view: what a human may do on each node now (from the workflow core),
    and each waiting gate's unmet checks (its escalation's)."""
    wf = ce.workflow
    actions = {nid: wf.actions(nid) for nid in ce.nodes if wf.actions(nid)}
    unmet = {n.id: n.escalation.reason.split(", ") for n in ce.nodes.values()
             if n.escalation and n.escalation.kind == "preconditions_unmet"}
    return {"actions": actions, "unmet": unmet, "stages": wf.stages()}


def change_state(ctl: Controller, ce: ChangeEngine) -> dict:
    """Everything the UI shows for one change. Run assignments (the rendered prompt variables)
    are left out; node feedback is counted, not copied."""
    ch, cfg = ce.change, ctl.cfg
    nodes = [n.model_dump(mode="json") | {"feedback_count": len(n.feedback)}
             for n in ce.nodes.values()]
    runs = [r.model_dump(mode="json", exclude={"assignment"}) for r in ce.runs.values()]
    return {"project": cfg.project, "repo": ch.workspace if ch else None,
            "decision_owners": cfg.decision_owners,
            "assets_sha": ctl.assets.sha, "limits": cfg.limits.__dict__,
            "change": ch.model_dump(mode="json") if ch else None, "nodes": nodes, "runs": runs,
            "gates": governance.gate_set(ce) if ch and ch.tier else [],
            "h2": h2_rows(ce) if ch and ch.requirements else [],
            "workflow": workflow_state(ce) if ch else {"actions": {}, "unmet": {}, "stages": []},
            "h3": [r.model_dump() for r in governance.h3_attestations(ce)] if ch and ch.tier else [],
            "surfaces": [s.model_dump() for s in governance.surfaces(ce)] if ch else [],
            "environment": environment_spec(ce),
            # the change's pull request once published (control/pr.json)
            "pull_request": ce.json_doc("control/pr.json") if ch and ce.latest("control/pr.json")
            else None,
            # node -> the log of its verification running now (/api/runs/<log>/log)
            "verifying": dict(ce.verifying),
            "a2a": {"cards": [c.model_dump(mode="json") for c in ce.a2a_cards()],
                    "questions": [q.model_dump(mode="json")
                                  for q in ce.change.questions.values()]},
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


# ---------------------------------------------------------------------------- gate screens (§2.3)
def trace(ce: ChangeEngine) -> dict:
    return traceability.traceability(ce).model_dump(mode="json")


async def acceptance(ce: ChangeEngine) -> dict:
    """What H-3 accepts: the diff (whole and per task), the protected paths it changes, and a dry
    evaluation of the ship gates at the integration head (what would not be satisfied yet)."""
    out = {"diff": None, "readiness": None, "error": None}
    try:
        out["diff"] = (await traceability.diffstat(ce)).model_dump(mode="json")
        if ce.change.tier and ce.change.integration_head:
            out["readiness"] = (await ship.evaluate(ce)).model_dump(mode="json")
    except GitError as e:
        out["error"] = str(e)
    return out


def doc_diff(docs: DocStore, path: str, old: int, new: int | None) -> dict:
    """A unified diff between two versions of a document (``new``: the latest)."""
    a, b = docs.read(path, old), docs.read(path, new)
    lines = difflib.unified_diff(a.content.splitlines(keepends=True),
                                 b.content.splitlines(keepends=True),
                                 fromfile=f"{path}@{a.version}", tofile=f"{path}@{b.version}")
    return {"path": path, "from": a.version, "to": b.version, "diff": "".join(lines)}
