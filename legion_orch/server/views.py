"""JSON views of the controller for the operator UI (presentation only: read, never mutate)."""
from __future__ import annotations

import difflib
import json

from ..store import DocStore
from ..workflow import cards, setup
from ..workflow.attest import FILE_RE
from ..workflow.engine import ChangeEngine, Controller
from ..workflow.environment import (
    EnvironmentSpec,
    Library,
    LibraryError,
    all_tools,
    planned_versions,
    resolve,
)
from ..workflow.model import LIVE_RUN
from ..workflow.tickets import Status
from .a2a import A2AHub


def change_list(ctl: Controller) -> list[dict]:
    out = []
    for ce in ctl.changes.values():
        c, tickets = ce.change, list(ce.tickets.t.values())
        out.append({"id": c.id, "workspace": c.workspace, "goal": c.goal,
                    "status": c.status, "tier": c.tier,
                    "created_at": c.created_at,
                    "passed": sum(t.status == Status.PASSED for t in tickets),
                    "tickets": len(tickets),
                    "live_runs": sum(r.status in LIVE_RUN for r in ce.runs.values()),
                    "needs_you": sum(i.blocking for i in ce.attention_items)})
    return sorted(out, key=lambda c: c["created_at"], reverse=True)


def environment_spec(ce: ChangeEngine) -> dict | None:
    """The environment spec being signed off (the planner's output, else the approved one):
    the proposals its profiles use and each profile's effective network (what its tools bring
    plus extras), or the problems."""
    ref = next((t.outputs.get("environment") for t in ce.tickets.t.values()
                if t.type == "agent:planner" and t.outputs.get("environment")), None) \
        or (ce.tickets.root.pointers.get("environment") if ce.tickets.t else None)
    m = FILE_RE.match(ref) if isinstance(ref, str) else None
    if ce.cfg.environment is None or m is None:
        return None
    raw = json.loads(ce.docs.read(m["path"], int(m["version"])).content)
    spec = EnvironmentSpec.model_validate(raw)
    try:
        profiles, problems = resolve(spec, Library.for_config(ce.cfg, ce.assets.schemas))
        network = {p: [{"policy": x.name, "tools": list(x.tools),
                        "hosts": [f"{e.host}:{e.port}" for e in x.endpoints]}
                       for x in rp.policies] for p, rp in profiles.items()}
        versions = _versions(ce, spec, profiles)
    except LibraryError as e:
        network, problems, versions = {}, [str(e)], []
    return {"path": m["path"], "version": int(m["version"]), "spec": raw,
            "proposals": {k.plural: v for k, v in spec.used_proposals().items()},
            "network": network, "problems": problems, "versions": versions}


def _versions(ce: ChangeEngine, spec: EnvironmentSpec, profiles) -> list[dict]:
    """The version each tool gets – the same in every image – and where it comes from: the
    provisioned build's, else what provisioning will decide (the mise.toml that runs gives it,
    else the latest, looked up then: ``version`` None)."""
    built = ce.change.environment.tools if ce.change.environment else {}
    running = setup.tools_at(spec.mise.content) if spec.mise is not None else ce.change.base_tools
    planned = planned_versions(all_tools(profiles), running)
    out = []
    for tool, p in sorted(planned.items()):
        b = built.get(tool)
        out.append({"tool": tool, "version": b.version if b else p.version,
                    "source": b.source if b else p.source,
                    "in_mise_toml": tool in running, "provisioned": b is not None})
    return out


def change_state(ctl: Controller, ce: ChangeEngine) -> dict:
    """Everything the UI shows for one change: its tickets (the tree: the root first), the
    pointers (the approved outputs), the sign-offs waiting and what they show, its runs. Run
    assignments (the rendered prompt variables) are left out."""
    ch, cfg = ce.change, ctl.cfg
    tickets = [t.model_dump(mode="json") for t in ce.tickets.t.values()]
    runs = [r.model_dump(mode="json", exclude={"assignment"}) for r in ce.runs.values()]
    root = ce.tickets.root if ce.tickets.t else None
    return {"project": cfg.project, "repo": ch.workspace if ch else None,
            "decision_owners": cfg.decision_owners,
            "assets_sha": ctl.assets.sha, "limits": cfg.limits.__dict__,
            "change": ch.model_dump(mode="json") if ch else None,
            "tickets": tickets, "runs": runs,
            # side processes' tickets, outside the tree (the retrospective)
            "hooks": [t.model_dump(mode="json") for t in ce.hooks.values()],
            "pointers": root.pointers if root else {},
            "paused": list(root.paused) if root else [],
            "signoffs": ce.signoffs_waiting(),
            "environment": environment_spec(ce),
            # the change's pull request once published (control/pr.json)
            "pull_request": ce.json_doc("control/pr.json") if ch and ce.latest("control/pr.json")
            else None,
            # ticket -> the log of its verification running now (/api/runs/<log>/log)
            "verifying": dict(ce.verifying),
            "verifications": [v.model_dump(mode="json") for v in ce.verifications()],
            "a2a": {"cards": [c.model_dump(mode="json") | {"state": cards.agent_state(ce, c)}
                              for c in A2AHub.of(ctl).cards(ce)],
                    "questions": [q.model_dump(mode="json")
                                  for q in ce.change.questions.values()]}}


def _item(i) -> dict:
    return i.model_dump(mode="json")


def attention(ctl: Controller) -> dict:
    """Every change's attention items (WORKFLOW.md §2.2): ``needs_you`` (blocking: a sign-off or
    an escalation waits for a decision owner) and ``inbox`` (to read: ended changes; the UI
    keeps which were read)."""
    items = ctl.attention_items()
    return {"needs_you": [_item(i) for i in items if i.blocking],
            "inbox": [_item(i) for i in items if not i.blocking]}


# What the "while you were away" digest counts, by timeline event kind (no model call).
DIGEST_KINDS = {"dispatch": "runs started", "report": "reports accepted",
                "report_rejected": "reports rejected", "run_failed": "runs failed",
                "escalation": "escalations", "decision": "human decisions",
                "signoff": "sign-offs", "publish": "publications"}


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


def doc_diff(docs: DocStore, path: str, old: int, new: int | None) -> dict:
    """A unified diff between two versions of a document (``new``: the latest)."""
    a, b = docs.read(path, old), docs.read(path, new)
    lines = difflib.unified_diff(a.content.splitlines(keepends=True),
                                 b.content.splitlines(keepends=True),
                                 fromfile=f"{path}@{a.version}", tofile=f"{path}@{b.version}")
    return {"path": path, "from": a.version, "to": b.version, "diff": "".join(lines)}
