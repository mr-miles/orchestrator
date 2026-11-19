"""Stage graph: which nodes a change has and how they depend on each other."""
from __future__ import annotations

from ...git import paths_overlap
from .. import guards, risk
from ..model import (
    ACCEPTED,
    ACTIVE_NODE,
    BASELINE,
    BUILD,
    H_CHANGE,
    H_ENV,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTEGRATION,
    ORACLE,
    PENDING,
    PLAN,
    PREFLIGHT,
    PROVISION,
    READY,
    RELEASE,
    RETRO,
    REWORK,
    SHIP,
    Node,
)
from .common import ENV_SPEC
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
        # the plan node is accepted once the critic passes it (planner, then critique, then
        # rework until it does): like a build task with its review
        c.add_node(Node(id=PLAN, kind=PLAN, title="Plan: spec, oracle, task graph; critique",
                        depends_on=[H_REQUIREMENTS]))
        after_critique = PLAN
        if c.cfg.environment is not None:          # the plan includes the sandbox environment
            c.add_node(Node(id=H_ENV, kind=HUMAN, title="Approve sandbox environment",
                            depends_on=[PLAN]))
            c.add_node(Node(id=PROVISION, kind=PROVISION, title="Provision: environment images",
                            depends_on=[H_ENV]))
            after_critique = PROVISION
        h2_deps = [after_critique]
        if r >= 2:
            c.add_node(Node(id=ORACLE, kind=ORACLE, title="Oracle: protected acceptance tests",
                            depends_on=[after_critique]))
            # the controller runs the oracle's commands in a fresh verifier sandbox (§5.4a)
            c.add_node(Node(id=PREFLIGHT, kind=PREFLIGHT, title="Preflight: the oracle's commands run",
                            depends_on=[ORACLE]))
            h2_deps = [PREFLIGHT]
        if r >= 3:
            c.add_node(Node(id=BASELINE, kind=BASELINE, title="Independent baseline",
                            depends_on=[PREFLIGHT]))
            h2_deps = [BASELINE]
        # H-2 also waits for the plan and the environment directly: in a fix round, oracle and
        # baseline may still be accepted from round 1 while the round is planned or its
        # environment provisioned again (§5.10a)
        c.add_node(Node(id=H_SPEC, kind=HUMAN, title="Approve spec & oracle",
                        depends_on=list(dict.fromkeys([PLAN, after_critique, *h2_deps]))))
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
        """Replace the build nodes by one per task of an accepted task graph. A blank
        repository's scaffold task (§5.3b) follows the plan (and provisioning) instead of H-2;
        the oracle and H-2 wait for it."""
        c = self.c
        for n in c.build_nodes():
            del c.nodes[n.id]
        for nid in (ORACLE, H_SPEC):     # an earlier plan's scaffold (also after Restart plan)
            if nid in c.nodes:
                c.nodes[nid].depends_on = [d for d in c.nodes[nid].depends_on
                                           if not d.startswith("build:")]
        wv = guards.waves(taskgraph["tasks"])
        ids = []
        for t in taskgraph["tasks"]:
            nid = f"build:{t['id']}"
            ids.append(nid)
            first = [PROVISION if PROVISION in c.nodes else PLAN] if t.get("scaffold") else [H_SPEC]
            c.add_node(Node(id=nid, kind=BUILD, title=f"{t['id']}: {t['title']}", task=t, wave=wv[t["id"]],
                            depends_on=first + [f"build:{d}" for d in t["depends_on"]]))
            if t.get("scaffold"):
                self._place_scaffold(nid, first[0])
        # downstream of builds
        if INTEGRATION in c.nodes:
            c.nodes[INTEGRATION].depends_on = list(ids)
        else:
            c.nodes[SHIP].depends_on = list(ids)
        # keep the stage order stable for display: move integration+ after builds
        for nid in (INTEGRATION, RELEASE, H_CHANGE, SHIP):
            if nid in c.nodes:
                c.nodes[nid] = c.nodes.pop(nid)

    def _place_scaffold(self, nid: str, after: str) -> None:
        """The oracle and H-2 depend on the scaffold, which is shown right after ``after``."""
        c = self.c
        for dep in (ORACLE, H_SPEC):
            if dep in c.nodes:
                c.nodes[dep].depends_on = list(dict.fromkeys(c.nodes[dep].depends_on + [nid]))
        order = [k for k in c.nodes if k != nid]
        order.insert(order.index(after) + 1, nid)
        for k in order:
            c.nodes[k] = c.nodes.pop(k)

    def add_round_builds(self, taskgraph: dict, n: int) -> list[str]:
        """Fix round ``n``: one build node per task the round appended to the task graph
        (earlier rounds' tasks stay as accepted). Integration then waits for them too."""
        c = self.c
        built = {b.task.id for b in c.build_nodes()}
        new = [t for t in taskgraph["tasks"] if t["id"] not in built]
        new_ids = {t["id"] for t in new}
        # earlier tasks are accepted: only the new ones (and their dependencies among each
        # other) form waves
        wv = guards.waves([dict(t, depends_on=[d for d in t["depends_on"] if d in new_ids])
                           for t in new])
        base = max((b.wave for b in c.build_nodes()), default=0)
        ids = []
        for t in new:
            nid = f"build:{t['id']}"
            ids.append(nid)
            c.add_node(Node(id=nid, kind=BUILD, title=f"{t['id']}: {t['title']}", task=t,
                            wave=base + wv[t["id"]], round=n,
                            depends_on=[H_SPEC] + [f"build:{d}" for d in t["depends_on"]]))
        if INTEGRATION in c.nodes:
            c.nodes[INTEGRATION].depends_on = list(dict.fromkeys(
                c.nodes[INTEGRATION].depends_on + ids))
        for nid in (INTEGRATION, RELEASE, H_CHANGE, SHIP):
            if nid in c.nodes:
                c.nodes[nid] = c.nodes.pop(nid)
        return ids

    def start_round(self, n: int) -> list[str]:
        """Fix round ``n`` passed its critique (§5.10a): what the round changed runs again – the
        environment (H-E1, provision) if its spec changed, the oracle if `plan/oracle.json`
        changed, and (R3) the baseline in any case – and the round's tasks are added. H-2 then
        waits for all of it."""
        c = self.c
        env = c.change.approvals.get(H_ENV)
        if H_ENV in c.nodes and env and env.pins.get(c.p(ENV_SPEC)) != c.latest(ENV_SPEC):
            self.reset_environment()
        orc = c.change.approvals.get("oracle")
        oracle_doc = "plan/oracle.json"
        if ORACLE in c.nodes and orc and orc.pins.get(c.p(oracle_doc)) != c.latest(oracle_doc):
            node = c.nodes[ORACLE]
            node.status, node.cycle = PENDING, node.cycle + 1
            node.base_sha = node.candidate_sha = node.accepted_sha = None
        for nid in (PREFLIGHT, BASELINE):     # run again at the round's head
            if nid in c.nodes:
                node = c.nodes[nid]
                node.status, node.cycle, node.candidate_sha = PENDING, node.cycle + 1, None
        return self.add_round_builds(c.json_doc("plan/taskgraph.json"), n)

    def remove_round_builds(self, n: int) -> None:
        """The round's plan is sent back at H-2: its (not yet dispatched) build nodes go."""
        c = self.c
        gone = {b.id for b in c.build_nodes() if b.round == n}
        for nid in gone:
            del c.nodes[nid]
        if INTEGRATION in c.nodes:
            c.nodes[INTEGRATION].depends_on = [d for d in c.nodes[INTEGRATION].depends_on
                                               if d not in gone]

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
