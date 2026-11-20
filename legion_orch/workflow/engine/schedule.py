"""Scheduler: one tick moves ready nodes forward and dispatches runs within the parallel limit."""
from __future__ import annotations

from ..model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BUILD,
    HUMAN,
    LIVE_RUN,
    MERGING,
    PENDING,
    PLAN,
    PREFLIGHT,
    PROVISION,
    R_REPORTED,
    READY,
    RETRO,
    REVIEWING,
    REWORK,
    ROLE_FOR_KIND,
    RUNNING,
    SHIP,
    VERIFYING,
    Node,
    Resume,
    Run,
)
from ..roles import handler_for
from . import cleanup
from .acceptance import Acceptance
from .core import ChangeCore
from .dispatch import Dispatcher
from .graph import StageGraph
from .provision import Provisioner
from .runs import RunLifecycle


class Scheduler:
    def __init__(self, core: ChangeCore, graph: StageGraph, lifecycle: RunLifecycle,
                 dispatcher: Dispatcher, acceptance: Acceptance, provisioner: Provisioner):
        self.c, self.graph, self.lifecycle = core, graph, lifecycle
        self.dispatcher, self.acceptance = dispatcher, acceptance
        self.provisioner = provisioner
        self._branch_head: str | None = None

    async def tick(self) -> None:
        """Under the lock: promote nodes whose dependencies are accepted, fail timed-out runs
        and prepare the runs there is capacity for; then launch them outside the lock."""
        c = self.c
        launches: list[Run] = []
        async with c.lock:
            if c.change is None:
                return
            if self.lifecycle.release_sandboxes():      # a gate passed, or the change ended
                c.save()
            active = c.change.status == "ACTIVE"
            if not active and c.cfg.retrospective.auto and RETRO not in c.nodes:
                self.graph.add_retrospective()
                c.event("retrospective", "the change has ended: retrospective queued", RETRO)
                c.save()
            if cleanup.due(c):                     # ended, retrospective done (§5.14)
                c.spawn(cleanup.end_of_change(c))
            # an ended change only runs a retrospective someone asked for
            if not active and not any(n.kind == RETRO for n in c.nodes.values()):
                return
            if active:
                for node in list(c.nodes.values()):
                    if node.status == PENDING and self.graph.deps_accepted(node) \
                            and (node.kind != PROVISION or self._environment_quiescent()):
                        self._promote(node)
            self.lifecycle.check_timeouts()
            # G-H1: the branch head, if a head-judging run may be dispatched this tick
            self._branch_head = (await c.git.integration_head(c.change.id)
                                 if active and any(self._judges_head(n) for n in c.nodes.values())
                                 else None)
            capacity = c.cfg.limits.max_parallel - c.host.live_count()
            for node in list(c.nodes.values()):
                if capacity <= 0:
                    break
                if not active and node.kind != RETRO:
                    continue
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
        elif node.kind == PREFLIGHT:          # the controller verifies the integration head
            node.candidate_sha = c.change.integration_head
            node.status = VERIFYING
            c.spawn(self.acceptance.verify_node(node.id))
        else:
            node.status = READY

    def _environment_quiescent(self) -> bool:
        """Provisioning (or reprovisioning) may start: no run of a role the current build maps
        is live and nothing is verifying or merging in that build."""
        c = self.c
        mapped = set(c.change.environment.roles) if c.change.environment else set()
        return not any(r.status in LIVE_RUN and r.role in mapped for r in c.runs.values()) \
            and not any(n.status in (VERIFYING, MERGING) for n in c.nodes.values())

    def _waits_for_environment(self, role: str) -> bool:
        """A role the provisioned environment maps waits while it is being rebuilt
        (``reprovision``), so no sandbox starts from the old build or the default image."""
        c = self.c
        prov, env = c.nodes.get(PROVISION), c.change.environment
        return bool(prov and env and prov.status != ACCEPTED and role in env.roles)

    @staticmethod
    def _judges_head(node: Node) -> bool:
        """A node about to dispatch a run at the integration head (not the retrospective, which
        reads an ended change)."""
        return node.status in (READY, REWORK) and node.kind in ROLE_FOR_KIND \
            and node.kind != RETRO and handler_for(ROLE_FOR_KIND[node.kind]).judges_head

    def _dispatch(self, node: Node, capacity: int) -> list[Run]:
        """Prepare at most ``capacity`` runs for the node: its role's run when it is ready (or
        in rework), or the missing panel members' runs while it is under review (the critic's,
        for the plan)."""
        c = self.c
        if node.kind == PROVISION and node.status == READY:     # controller work, no run
            node.status = RUNNING
            c.event("provision", "provisioning the approved environment", node.id)
            c.spawn(self.provisioner.provision())
            return []
        if node.status in (READY, REWORK) and node.kind in ROLE_FOR_KIND:
            if not self.graph.deps_accepted(node):
                return []
            if node.kind == RETRO and self.lifecycle.sandboxes.reflector.pending:
                return []                  # its material includes the last reflections
            if node.kind == BUILD and self.graph.overlaps_running(node):
                return []
            if self._waits_for_environment(ROLE_FOR_KIND[node.kind]):
                return []
            if self._judges_head(node) and self._branch_head \
                    and (moved := c.head_moved(self._branch_head)):
                c.escalate(node, "head_moved", moved, resume=Resume.READY)
                return []
            run = self.dispatcher.prepare(node, ROLE_FOR_KIND[node.kind])
            return [run] if run else []
        runs: list[Run] = []
        if node.status == REVIEWING:
            # a build task's review panel; the plan's critic
            for role in (c.change.panel if node.kind == BUILD else ["critic"] if node.kind == PLAN
                         else []):
                if len(runs) >= capacity:
                    break
                busy = any(r.node_id == node.id and r.role == role and r.cycle == node.cycle
                           and r.status in LIVE_RUN | {R_REPORTED} for r in c.runs.values())
                if f"{node.cycle}:{role}" not in node.reviews and not busy \
                        and not self._waits_for_environment(role):
                    run = self.dispatcher.prepare(node, role, judged_sha=node.candidate_sha)
                    if run:
                        runs.append(run)
        return runs
