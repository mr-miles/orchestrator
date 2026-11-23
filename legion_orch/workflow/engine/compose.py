"""A change's layers, put together: the one module that sees them all (docs/architecture.md §1).

Each layer is given only the parts it uses – the runtime the step runner, the step runner the
phases' context and services, the services the sandboxes – so control passes only between
neighbouring layers. The facade (:class:`.ChangeEngine`) is given only what it fronts: the
runtime, the decision owners' actions, the agents' reports and the run logs; it never sees the
step layer (checked by the import contracts)."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...bus import DocWritten, Escalated, Pushed
from .runtime.flow_runtime import FlowRuntime
from .runtime.human import HumanGates
from .sandboxes.sandboxes import Sandboxes
from .services.agent_runs import AgentRuns
from .services.agents import AgentRunner, RunTasks
from .services.provision import Provisioner
from .services.publish import Publisher
from .services.reports import Reports
from .services.resolver import Resolver
from .services.runs import RunLifecycle
from .services.verification import Verifications
from .steps import StepHolds, StepRunner
from .steps.phases import PhaseContext

if TYPE_CHECKING:
    from .change import ChangeEngine


def compose(ce: ChangeEngine) -> None:
    """Build ``ce``'s layers and wire them. The facade keeps ``_flow``, ``_gates``,
    ``_reports`` and ``_lifecycle``; the others are kept too, for tests only."""
    sandboxes = Sandboxes(ce, StepHolds(ce))
    lifecycle = RunLifecycle(ce, sandboxes)
    agents = AgentRuns(ce, lifecycle, sandboxes, roles=ce)
    phases = PhaseContext(ce, sandboxes, ce.svc.dispatcher, agents, Publisher(ce))
    provisioner = Provisioner(ce, lifecycle, sandboxes, ce.svc.dispatcher)
    sandboxes.environments = provisioner          # whether a role's environment is built
    reports = Reports(ce, lifecycle, roles=ce)
    agents.reports = reports
    verifications = Verifications(ce, sandboxes, ce.svc.dispatcher, lifecycle, ce.svc.verify_cap)
    agents.verifications = verifications          # G-V0: judges see verified candidates only
    phases.verifications = verifications
    agents.tasks = tasks = RunTasks(ce, lifecycle)   # a run is the orchestrator's A2A task
    runner = StepRunner(ce, phases, lifecycle, provisioner)
    gates = HumanGates(ce, lifecycle, runner)
    flow = FlowRuntime(ce, runner, lifecycle)
    runner.runtime = flow
    gates.flow = flow
    ce._flow, ce._gates, ce._reports, ce._lifecycle = flow, gates, reports, lifecycle
    ce._verifications = verifications             # fronted: pushes, agents' requests, the API
    ce._tasks = tasks
    # the workflow's one counterpart for agents: cards, inboxes, turns (services/agents/)
    agent_runner = AgentRunner(ce, lifecycle, agents, sandboxes, ce.svc.dispatcher)
    phases.agent_runner = flow.agent_runner = ce._agent_runner = agent_runner
    ce._resolver = resolver = Resolver(ce, flow.actions)   # every escalation goes to it
    ce.svc.bus.subscribe(Escalated, resolver.escalated, cid=ce.cid)
    ce.svc.bus.subscribe(Pushed, lambda e: ce.pushed(e.run, e.sha), cid=ce.cid)
    ce.svc.bus.subscribe(DocWritten, ce.projections.written, cid=ce.cid)
    # not the facade's: for tests that drive a layer directly
    ce._sandboxes, ce._agents, ce._phases, ce._provisioner, ce._runner = (
        sandboxes, agents, phases, provisioner, runner)
