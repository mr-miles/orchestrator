"""Stage graph: which nodes a change has and how they depend on each other."""
from __future__ import annotations

from ...git import paths_overlap
from .. import guards, risk
from ..model import (
    ACCEPTED,
    ACTIVE_NODE,
    BASELINE,
    BUILD,
    CRITIQUE,
    H_CHANGE,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTEGRATION,
    ORACLE,
    PENDING,
    PLAN,
    PROVISION,
    READY,
    RELEASE,
    RETRO,
    REWORK,
    SHIP,
    Node,
)
from .core import ChangeCore


class StageGraph:
    """The node graph of one change: stage nodes by tier, build nodes from the task graph, and
    the dependency and overlap queries the scheduler asks."""

    def __init__(self, core: ChangeCore):
        self.c = core

    def create_stage_nodes(self) -> None:
        """After H-1: the stages the accepted tier requires."""
        c = self.c
        tier = c.change.tier
        r = risk.rank(tier)
        c.add_node(Node(id=PLAN, kind=PLAN, title="Plan: spec, oracle, task graph",
                        depends_on=[H_REQUIREMENTS]))
        c.add_node(Node(id=CRITIQUE, kind=CRITIQUE, title="Critique: pre-mortem", depends_on=[PLAN]))
        after_critique = CRITIQUE
        if c.cfg.environment is not None:          # the plan includes the sandbox environment
            c.add_node(Node(id=H_ENV, kind=HUMAN, title="Approve sandbox environment",
                            depends_on=[CRITIQUE]))
            c.add_node(Node(id=PROVISION, kind=PROVISION, title="Provision: environment images",
                            depends_on=[H_ENV]))
            after_critique = PROVISION
        h2_deps = [after_critique]
        if r >= 2:
            c.add_node(Node(id=ORACLE, kind=ORACLE, title="Oracle: protected acceptance tests",
                            depends_on=[after_critique]))
            h2_deps = [ORACLE]
        if r >= 3:
            c.add_node(Node(id=BASELINE, kind=BASELINE, title="Independent baseline", depends_on=[ORACLE]))
            h2_deps = [BASELINE]
        c.add_node(Node(id=H_SPEC, kind=HUMAN, title="Approve spec & oracle", depends_on=h2_deps))
        if r >= 2:
            c.add_node(Node(id=INTEGRATION, kind=INTEGRATION, title="Integration verification",
                            depends_on=[H_SPEC]))  # replaced by build ids at critique PASS
        if r >= 3:
            c.add_node(Node(id=RELEASE, kind=RELEASE, title="Release & rollback plan",
                            depends_on=[INTEGRATION]))
        if r >= 2:
            c.add_node(Node(id=H_CHANGE, kind=HUMAN, title="Accept whole change",
                            depends_on=[RELEASE if r >= 3 else INTEGRATION]))
        c.add_node(Node(id=SHIP, kind=SHIP, title="Ship readiness check",
                        depends_on=[H_CHANGE] if r >= 2 else [H_SPEC]))

    def add_retrospective(self) -> Node:
        """Queue the retrospective of an ended change: a new node, or a new cycle of it."""
        c = self.c
        node = c.nodes.get(RETRO)
        if node is None:
            node = Node(id=RETRO, kind=RETRO, status=READY,
                        title="Retrospective: lessons for the assets and process")
            c.add_node(node)
        else:
            node.cycle, node.status, node.escalation = node.cycle + 1, READY, None
        return node

    def materialize_builds(self, taskgraph: dict) -> None:
        """Replace the build nodes by one per task of an accepted task graph."""
        c = self.c
        for n in c.build_nodes():
            del c.nodes[n.id]
        wv = guards.waves(taskgraph["tasks"])
        ids = []
        for t in taskgraph["tasks"]:
            nid = f"build:{t['id']}"
            ids.append(nid)
            c.add_node(Node(id=nid, kind=BUILD, title=f"{t['id']}: {t['title']}", task=t, wave=wv[t["id"]],
                            depends_on=[H_SPEC] + [f"build:{d}" for d in t["depends_on"]]))
        # downstream of builds
        if INTEGRATION in c.nodes:
            c.nodes[INTEGRATION].depends_on = list(ids)
        else:
            c.nodes[SHIP].depends_on = list(ids)
        # keep the stage order stable for display: move integration+ after builds
        for nid in (INTEGRATION, RELEASE, H_CHANGE, SHIP):
            if nid in c.nodes:
                c.nodes[nid] = c.nodes.pop(nid)

    def reset_environment(self) -> None:
        """The plan (with its environment spec) is being reworked: the environment's approval
        and build are dropped, so H-E1 and provision follow the new spec again."""
        c = self.c
        if H_ENV not in c.nodes:
            return
        for nid in (H_ENV, PROVISION):
            c.nodes[nid].status = PENDING
        c.change.approvals.pop(H_ENV, None)
        c.change.environment = None

    def deps_accepted(self, node: Node) -> bool:
        """Guard G-D1."""
        c = self.c
        return all(c.nodes.get(d) is not None and c.nodes[d].status == ACCEPTED
                   for d in node.depends_on)

    def overlaps_running(self, node: Node) -> bool:
        """Guard G-D3 (file overlap part)."""
        mine = node.task.files_modified
        for other in self.c.build_nodes():
            if other.id == node.id or other.status not in ACTIVE_NODE | {REWORK}:
                continue
            if any(paths_overlap(a, b) for a in mine for b in other.task.files_modified):
                return True
        return False
