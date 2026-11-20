"""Scheduler: one tick moves ready nodes forward and dispatches runs within the parallel limit."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..model import (
    ACCEPTED,
    LIVE_RUN,
    MERGING,
    PROVISION,
    READY,
    RETRO,
    REWORK,
    VERIFYING,
    Node,
    Run,
)
from . import cleanup
from .acceptance import Acceptance
from .core import ChangeCore
from .dispatch import Dispatcher
from .graph import StageGraph
from .provision import Provisioner
from .runs import RunLifecycle
from .steps import StepRunner, step_for

if TYPE_CHECKING:
    pass


class Scheduler:
    def __init__(self, core: ChangeCore, graph: StageGraph, lifecycle: RunLifecycle,
                 dispatcher: Dispatcher, acceptance: Acceptance, provisioner: Provisioner):
        self.c, self.graph, self.lifecycle = core, graph, lifecycle
        self.dispatcher, self.acceptance = dispatcher, acceptance
        self.provisioner = provisioner
        self.branch_head: str | None = None      # G-H1: read once per tick
        self.runner: StepRunner = None  # type: ignore[assignment]   # set once the runner exists

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
            if not active and not any(step_for(n).runs_after_end for n in c.nodes.values()):
                return
            self.lifecycle.check_timeouts()
            if active and c.change.reprovision == "requested" and self.environment_quiescent():
                c.change.reprovision = "running"     # a change-level operation, not a step
                c.spawn(self.provisioner.rebuild())
            # G-H1: the branch head, if a head-judging run may be dispatched this tick
            self.branch_head = (await c.git.integration_head(c.change.id)
                                if active and any(self.judges_head(n) for n in c.nodes.values())
                                 else None)
            capacity = c.cfg.limits.max_parallel - c.host.live_count()
            for node in list(c.nodes.values()):
                if capacity <= 0:
                    break
                if not active and not step_for(node).runs_after_end:
                    continue
                for run in self.runner.dispatch(node, capacity):
                    launches.append(run)
                    capacity -= 1
            c.save()
        for run in launches:
            c.spawn(self.dispatcher.launch(run.id))

    def environment_quiescent(self) -> bool:
        """Provisioning (or reprovisioning) may start: no run of a role the current build maps
        is live and nothing is verifying or merging in that build."""
        c = self.c
        mapped = set(c.change.environment.roles) if c.change.environment else set()
        return not any(r.status in LIVE_RUN and r.role in mapped for r in c.runs.values()) \
            and not any(n.status in (VERIFYING, MERGING) for n in c.nodes.values())

    def waits_for_environment(self, role: str) -> bool:
        """A role the provisioned environment maps waits while it is being rebuilt
        (``reprovision``), so no sandbox starts from the old build or the default image."""
        c = self.c
        prov, env = c.current(PROVISION), c.change.environment
        return bool(prov and env and role in env.roles
                    and (prov.status != ACCEPTED or c.change.reprovision))

    @staticmethod
    def judges_head(node: Node) -> bool:
        """A node about to dispatch a run at the integration head (not the retrospective, which
        reads an ended change)."""
        return node.status in (READY, REWORK) and step_for(node).judges_head
