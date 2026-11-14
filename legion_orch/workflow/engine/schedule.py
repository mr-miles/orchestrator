"""Scheduler: one tick moves ready nodes forward and dispatches runs within the parallel limit."""
from __future__ import annotations

from ..model import (
    AWAITING_HUMAN,
    BUILD,
    HUMAN,
    LIVE_RUN,
    PENDING,
    R_REPORTED,
    READY,
    REVIEWING,
    REWORK,
    ROLE_FOR_KIND,
    RUNNING,
    SHIP,
    Node,
    Run,
)
from .acceptance import Acceptance
from .core import ChangeCore
from .dispatch import Dispatcher
from .graph import StageGraph
from .runs import RunLifecycle


class Scheduler:
    def __init__(self, core: ChangeCore, graph: StageGraph, lifecycle: RunLifecycle,
                 dispatcher: Dispatcher, acceptance: Acceptance):
        self.c, self.graph, self.lifecycle = core, graph, lifecycle
        self.dispatcher, self.acceptance = dispatcher, acceptance

    async def tick(self) -> None:
        """Under the lock: promote nodes whose dependencies are accepted, fail timed-out runs
        and prepare the runs there is capacity for; then launch them outside the lock."""
        c = self.c
        launches: list[Run] = []
        async with c.lock:
            if c.change is None or c.change.status != "ACTIVE":
                return
            for node in list(c.nodes.values()):
                if node.status == PENDING and self.graph.deps_accepted(node):
                    self._promote(node)
            self.lifecycle.check_timeouts()
            capacity = c.cfg.limits.max_parallel - c.host.live_count()
            for node in list(c.nodes.values()):
                if capacity <= 0:
                    break
                for run in self._dispatch(node, capacity):
                    launches.append(run)
                    capacity -= 1
            c.save()
        for run in launches:
            c.spawn(self.dispatcher.launch(run.id))

    def _promote(self, node: Node) -> None:
        c = self.c
        if node.kind == HUMAN:
            node.status = AWAITING_HUMAN
            c.event("human", "awaiting human decision", node.id)
        elif node.kind == SHIP:
            node.status = RUNNING
            c.spawn(self.acceptance.ship())
        else:
            node.status = READY

    def _dispatch(self, node: Node, capacity: int) -> list[Run]:
        """Prepare at most ``capacity`` runs for the node: its role's run when it is ready (or
        in rework), or the missing panel members' runs while it is under review."""
        c = self.c
        if node.status in (READY, REWORK) and node.kind in ROLE_FOR_KIND:
            if not self.graph.deps_accepted(node):
                return []
            if node.kind == BUILD and self.graph.overlaps_running(node):
                return []
            run = self.dispatcher.prepare(node, ROLE_FOR_KIND[node.kind])
            return [run] if run else []
        runs: list[Run] = []
        if node.status == REVIEWING:
            for role in c.change.panel:
                if len(runs) >= capacity:
                    break
                busy = any(r.node_id == node.id and r.role == role and r.cycle == node.cycle
                           and r.status in LIVE_RUN | {R_REPORTED} for r in c.runs.values())
                if f"{node.cycle}:{role}" not in node.reviews and not busy:
                    run = self.dispatcher.prepare(node, role, judged_sha=node.candidate_sha)
                    if run:
                        runs.append(run)
        return runs
