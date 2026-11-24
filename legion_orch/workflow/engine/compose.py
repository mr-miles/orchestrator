"""A change's layers, put together: the one module that sees them all (docs/architecture.md §1).

Each layer is given only the parts it uses – the workflow (TicketFlow) the services' adapters,
the services the sandboxes – so control passes only between neighbouring layers. The facade
(:class:`.ChangeEngine`) is given only what it fronts: the workflow, the agents' reports and the
run logs."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...bus import DocWritten, Escalated, Pushed
from ..tickets.executors import Backoff
from .flow import Agents, Provision, Signoffs, TicketFlow, Verify
from .sandboxes.sandboxes import Sandboxes
from .services.agent_runs import AgentRuns
from .services.agents import AgentRunner, RunTasks
from .services.provision import Provisioner
from .services.publish import Publisher
from .services.reports import Reports
from .services.resolver import Resolver
from .services.runs import RunLifecycle
from .services.verification import Verifications

if TYPE_CHECKING:
    from .change import ChangeEngine


def compose(ce: ChangeEngine) -> None:
    """Build ``ce``'s layers and wire them."""
    dispatcher = ce.svc.dispatcher
    sandboxes = Sandboxes(ce)
    lifecycle = RunLifecycle(ce, sandboxes)
    agents = AgentRuns(ce, lifecycle, sandboxes, roles=ce)
    provisioner = Provisioner(ce, lifecycle, sandboxes, dispatcher)
    sandboxes.environments = provisioner          # whether a role's environment is built
    reports = Reports(ce, lifecycle, roles=ce)
    agents.reports = reports
    verifications = Verifications(ce, sandboxes, dispatcher, lifecycle, ce.svc.verify_cap)
    agents.tasks = tasks = RunTasks(ce, lifecycle)   # a run is the orchestrator's A2A task
    # the workflow's one counterpart for agents: cards, inboxes, turns (services/agents/)
    agent_runner = AgentRunner(ce, lifecycle, agents, sandboxes, dispatcher)
    agents.runner = agent_runner
    signoffs = Signoffs(ce)
    flow = TicketFlow(
        ce, agents=Agents(ce, agent_runner, agents, lifecycle), signoffs=signoffs,
        verify=Verify(ce, verifications),
        provision=Provision(ce, provisioner), publisher=Publisher(ce),
        agent_runner=agent_runner, lifecycle=lifecycle,
        backoff=Backoff(tries=ce.cfg.limits.system_retries, first=ce.cfg.limits.system_retry_s))
    ce._flow, ce._reports, ce._lifecycle, ce._signoffs = flow, reports, lifecycle, signoffs
    ce._verifications, ce._tasks, ce._agent_runner = verifications, tasks, agent_runner
    ce._resolver = resolver = Resolver(ce)               # every escalation goes to it
    ce.svc.bus.subscribe(Escalated, resolver.escalated, cid=ce.cid)
    ce.svc.bus.subscribe(Pushed, lambda e: ce.pushed(e.run, e.sha), cid=ce.cid)
    ce.svc.bus.subscribe(DocWritten, ce.projections.written, cid=ce.cid)
    # not the facade's: for tests that drive a layer directly
    ce._sandboxes, ce._agents, ce._provisioner = sandboxes, agents, provisioner
