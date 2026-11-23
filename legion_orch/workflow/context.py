"""What an agent may know about where its work stands (MCP ``step_status``, ``workflow_context``),
derived from the change's state alone (like :mod:`.attention` and :mod:`.cards`).

* :func:`step_status` – one step as it is now: its phase and cycle, the budgets left, its
  candidate's verification, the panel's progress, an escalation, the step's agents.
* :func:`workflow_context` – how a step fits the change: the goal and tier, the stages and where
  the step is, the task's place in the task graph (what it builds on, what builds on it, which
  tasks touch nearby files), the requirements and acceptance criteria it serves, who judges it,
  and whom to ask.

Agents read these to decide, never to override what their assignment says.
"""
from __future__ import annotations

import posixpath
from typing import Any

from . import cards as card_views
from . import governance
from .model import BUILD, V_FINAL, Node, Oracle
from .view import ChangeView, RoleContext


def _dirs(paths: list[str]) -> set[str]:
    return {posixpath.dirname(p.rstrip("/")) or "." for p in paths}


def _judges(e: ChangeView, node: Node) -> list[str]:
    """The step's judges: as recorded when it started, or – a build task not started yet – the
    change's panel."""
    if not node.agent_roles:
        return list(e.change.panel) if node.kind == BUILD else []
    producer = node.agent_roles[0]
    return [r for r in node.agent_roles if r != producer]


def _budget(e: ChangeView, node: Node) -> dict[str, Any] | None:
    lim = e.cfg.limits
    if node.kind == BUILD:
        n = lim.review_max_cycles + node.extra_cycles
        return {"kind": "review cycles", "cycle": node.cycle, "of": n,
                "left": max(0, n - node.cycle)}
    if any(r in ("critic", "pr-reviewer") for r in node.agent_roles):
        n = lim.critique_max_cycles + node.extra_cycles
        return {"kind": "critique cycles", "cycle": node.cycle, "of": n,
                "left": max(0, n - node.cycle)}
    return None


def step_status(e: ChangeView, node_id: str) -> dict[str, Any]:
    """One step as it is now."""
    node = e.nodes.get(node_id)
    if node is None:
        raise KeyError(node_id)
    out: dict[str, Any] = {"node": node.id, "title": node.title, "kind": str(node.kind),
                           "status": str(node.status), "since": node.status_since,
                           "cycle": node.cycle, "budget": _budget(e, node),
                           "candidate": node.candidate_sha, "accepted": node.accepted_sha}
    if node.candidate_sha:
        runs = [v for v in e.change.verifications.values()
                if v.sha == node.candidate_sha and v.node == node.id]
        latest = max(runs, key=lambda v: v.queued_at, default=None)
        out["verification"] = ({"id": latest.id, "state": str(latest.state),
                                "final": latest.state in V_FINAL} if latest else None)
    judges = _judges(e, node)
    if judges:
        out["panel"] = [{"role": r, **({"verdict": str(rv.verdict),
                                        "findings": [f.title for f in rv.findings]}
                                       if (rv := node.review(node.cycle, r)) else
                                       {"verdict": None})} for r in judges]
    if node.escalation:
        esc = node.escalation
        out["escalation"] = {"kind": esc.kind, "reason": esc.reason[:1000],
                             "resume": str(esc.resume) if esc.resume else None,
                             "at": esc.at}
    out["agents"] = [{"card": c.id, "state": card_views.agent_state(e, c)}
                     for c in card_views.ordered(e, card_views.cards(e)) if c.node == node.id]
    return out


def workflow_context(e: RoleContext, node_id: str, stages: list[dict]) -> dict[str, Any]:
    """How the step fits the change (``stages``: the workflow's stage tree, outermost first)."""
    ch, node = e.change, e.nodes.get(node_id)
    if node is None:
        raise KeyError(node_id)
    out: dict[str, Any] = {
        "change": {"id": ch.id, "goal": ch.goal, "tier": ch.tier, "status": str(ch.status),
                   "round": ch.round},
        "stages": [{"id": s["id"], "title": s["title"], "status": s["status"],
                    "depth": s["depth"], "here": node.id in s["nodes"]} for s in stages],
    }
    status = {n.id: str(n.status) for n in e.nodes.values()}
    task = node.task
    if task is not None:
        builds = {n.task.id: n for n in e.build_nodes() if n.task}
        mine = _dirs(task.files_modified)
        out["task"] = {
            "id": task.id, "title": task.title, "requirements": task.requirements,
            "files_modified": task.files_modified,
            "builds_on": [{"task": d, "node": builds[d].id, "status": status[builds[d].id]}
                          for d in task.depends_on if d in builds],
            "built_on_by": [{"task": t, "node": n.id, "status": status[n.id]}
                            for t, n in builds.items() if task.id in n.task.depends_on],
            "nearby": [{"task": t, "node": n.id, "status": status[n.id],
                        "shared_dirs": sorted(mine & _dirs(n.task.files_modified))}
                       for t, n in builds.items()
                       if t != task.id and mine & _dirs(n.task.files_modified)],
        }
    reqs = task.requirements if task else list(ch.requirements)
    oracle, approved = ch.oracle, ch.oracle is not None
    planned = f"{governance.draft(e)}/oracle.json"
    if oracle is None and e.latest(planned):                 # planned, not (yet) approved
        try:
            oracle = Oracle.model_validate(e.json_doc(planned))
        except ValueError:
            oracle = None
    out["oracle_approved"] = approved
    out["requirements"] = [
        {"id": r, "criteria": [{"id": c.id, "text": c.text, "tests": c.tests,
                                "commands": c.commands}
                               for c in (oracle.criteria if oracle else []) if c.requirement == r]}
        for r in reqs]
    judges = _judges(e, node)
    out["judged_by"] = [{"role": r, "loads": [s for s in e.assets.roles[r].skills
                                              if s not in ("harness-contract", "docstore-protocol",
                                                           "attestation-format", "a2a-protocol",
                                                           "a2a-cli")]}
                        for r in judges if r in e.assets.roles]
    ask = [c for c in card_views.ordered(e, card_views.cards(e)) if c.node is not None and c.node != node.id]
    related = {d["node"] for d in out.get("task", {}).get("builds_on", [])} | {
        d["node"] for d in out.get("task", {}).get("nearby", [])}
    out["ask"] = [{"card": c.id, "state": card_views.agent_state(e, c),
                   "why": "your task builds on it" if c.node in related and task
                   and any(d["node"] == c.node for d in out["task"]["builds_on"])
                   else "it touches nearby files" if c.node in related
                   else "the oracle: acceptance tests" if c.role == "oracle"
                   else "the plan" if c.role == "planner"
                   else "stuck on a guard, the oracle or the plan" if c.role == "resolver"
                   else "another step"}
                  for c in ask if c.node in related or c.role in ("oracle", "planner", "resolver")]
    out["ask"].insert(0, {"card": card_views.ORCHESTRATOR, "state": "orchestrator",
                          "why": "list_cards, ask_human"})
    return out
