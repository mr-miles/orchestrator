"""Stage graph and scheduling.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

from datetime import UTC, datetime

from .. import guards, risk
from ..model import (
    ACCEPTED,
    ACTIVE_NODE,
    AWAITING_HUMAN,
    BASELINE,
    BUILD,
    CRITIQUE,
    H_CHANGE,
    H_REQUIREMENTS,
    H_SPEC,
    HUMAN,
    INTEGRATION,
    LIVE_RUN,
    ORACLE,
    PENDING,
    PLAN,
    R_REPORTED,
    READY,
    RELEASE,
    REVIEWING,
    REWORK,
    ROLE_FOR_KIND,
    RUNNING,
    SHIP,
    Node,
    Run,
)
from .common import _parse_ts


class GraphMixin:
    def _add(self, node: Node) -> None:
        self.nodes[node.id] = node

    def _create_stage_nodes(self) -> None:
        tier = self.change.tier
        r = risk.rank(tier)
        self._add(Node(PLAN, PLAN, "Plan: spec, oracle, task graph", depends_on=[H_REQUIREMENTS]))
        self._add(Node(CRITIQUE, CRITIQUE, "Critique: pre-mortem", depends_on=[PLAN]))
        h2_deps = [CRITIQUE]
        if r >= 2:
            self._add(Node(ORACLE, ORACLE, "Oracle: protected acceptance tests",
                           depends_on=[CRITIQUE]))
            h2_deps = [ORACLE]
        if r >= 3:
            self._add(Node(BASELINE, BASELINE, "Independent baseline", depends_on=[ORACLE]))
            h2_deps = [BASELINE]
        self._add(Node(H_SPEC, HUMAN, "Approve spec & oracle", depends_on=h2_deps))
        if r >= 2:
            self._add(Node(INTEGRATION, INTEGRATION, "Integration verification",
                           depends_on=[H_SPEC]))  # replaced by build ids at critique PASS
        if r >= 3:
            self._add(Node(RELEASE, RELEASE, "Release & rollback plan", depends_on=[INTEGRATION]))
        if r >= 2:
            self._add(Node(H_CHANGE, HUMAN, "Accept whole change",
                           depends_on=[RELEASE if r >= 3 else INTEGRATION]))
        self._add(Node(SHIP, SHIP, "Ship readiness check",
                       depends_on=[H_CHANGE] if r >= 2 else [H_SPEC]))

    def build_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == BUILD]

    def materialize_builds(self, taskgraph: dict) -> None:
        for n in self.build_nodes():
            del self.nodes[n.id]
        wv = guards.waves(taskgraph["tasks"])
        ids = []
        for t in taskgraph["tasks"]:
            nid = f"build:{t['id']}"
            ids.append(nid)
            self._add(Node(nid, BUILD, f"{t['id']}: {t['title']}", task=t, wave=wv[t["id"]],
                           depends_on=[H_SPEC] + [f"build:{d}" for d in t["depends_on"]]))
        # downstream of builds
        if INTEGRATION in self.nodes:
            self.nodes[INTEGRATION].depends_on = list(ids)
        else:
            self.nodes[SHIP].depends_on = list(ids)
        # keep the stage order stable for display: move integration+ after builds
        for nid in (INTEGRATION, RELEASE, H_CHANGE, SHIP):
            if nid in self.nodes:
                self.nodes[nid] = self.nodes.pop(nid)

    def _deps_accepted(self, node: Node) -> bool:
        """Guard G-D1."""
        return all(self.nodes.get(d) is not None and self.nodes[d].status == ACCEPTED
                   for d in node.depends_on)

    def _live_runs(self) -> list[Run]:
        return [r for r in self.runs.values() if r.status in LIVE_RUN]

    def _overlaps_running(self, node: Node) -> bool:
        """Guard G-D3 (file overlap part)."""
        from ...git import paths_overlap
        mine = node.task["files_modified"]
        for other in self.build_nodes():
            if other.id == node.id or other.status not in ACTIVE_NODE | {REWORK}:
                continue
            if any(paths_overlap(a, b) for a in mine for b in other.task["files_modified"]):
                return True
        return False

    async def tick(self) -> None:
        launches: list[Run] = []
        async with self.lock:
            if self.change is None or self.change.status != "ACTIVE":
                return
            for node in list(self.nodes.values()):
                if node.status == PENDING and self._deps_accepted(node):
                    if node.kind == HUMAN:
                        node.status = AWAITING_HUMAN
                        self.event("human", "awaiting human decision", node.id)
                    elif node.kind == SHIP:
                        node.status = RUNNING
                        self._spawn(self._ship())
                    else:
                        node.status = READY
            self._check_timeouts()
            capacity = self.cfg.limits.max_parallel - self.ctl.live_count()
            for node in list(self.nodes.values()):
                if capacity <= 0:
                    break
                if node.status in (READY, REWORK) and node.kind in ROLE_FOR_KIND:
                    if not self._deps_accepted(node):
                        continue
                    if node.kind == BUILD and self._overlaps_running(node):
                        continue
                    run = self._prepare(node, ROLE_FOR_KIND[node.kind])
                    if run:
                        launches.append(run)
                        capacity -= 1
                elif node.status == REVIEWING:
                    for role in self.change.panel:
                        if capacity <= 0:
                            break
                        key = f"{node.cycle}:{role}"
                        busy = any(r.node_id == node.id and r.role == role and r.cycle == node.cycle
                                   and r.status in LIVE_RUN | {R_REPORTED} for r in self.runs.values())
                        if key not in node.reviews and not busy:
                            run = self._prepare(node, role, judged_sha=node.candidate_sha)
                            if run:
                                launches.append(run)
                                capacity -= 1
            self._save()
        for run in launches:
            self._spawn(self._launch(run.id))

    def _check_timeouts(self) -> None:
        limit = self.cfg.limits.run_timeout_s
        now = datetime.now(UTC)
        for run in self._live_runs():
            if (now - _parse_ts(run.created_at)).total_seconds() > limit:
                proc = self.procs.get(run.id)
                if proc and proc.returncode is None:
                    proc.kill()
                self._fail_run(run, f"timed out after {limit}s")
