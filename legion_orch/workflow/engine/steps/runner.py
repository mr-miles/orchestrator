"""The step runner (plan: stages-and-steps.md §2): the step layer's entry from above. Each
node's step as it runs is an :class:`.ActiveStep` (:mod:`.active`), which moves the node through
its step type's phases (:mod:`.step_types`); at a step's boundaries the layer above is told
(:class:`StepEvents`)."""
from __future__ import annotations

from collections.abc import Awaitable
from typing import TYPE_CHECKING, Protocol

from ...model import Feedback, Node, Run
from ..base.core import ChangeCore
from .active import ActiveStep
from .node_states import WAITING, NodeStates
from .phases import PhaseContext

if TYPE_CHECKING:
    from ..services.provision import Provisioner
    from ..services.runs import RunLifecycle


# ---------------------------------------------------------------------------- the runner
class StepEvents(Protocol):
    """What the step layer tells the layer above it (the workflow runtime implements it): a
    step crossed one of its states. The step layer calls nothing else above it."""

    def completed(self, node: Node) -> None:
        """The step is done (its last phase passed, or a round's step stands)."""

    def escalated(self, node: Node) -> None:
        """The step waits for a human."""

    def resumed(self, node: Node) -> None:
        """The step's escalation was resolved: it goes on."""

    def redo(self, step: str, feedback: Feedback) -> None:
        """An earlier step must do its work again (found wanting by this one)."""

    def define_tasks(self, plan: Node) -> list[str]:
        """The plan was accepted: its task graph's build tasks become the workflow's steps (their
        ids)."""


class StepRunner:
    """The step layer: each node's step as it runs is an :class:`.ActiveStep`, made here and
    kept while the node is the same; the runner hands the runtime's calls to it and does what
    spans steps – a send-back's undoing (once per policy), retries after a restart,
    housekeeping at saves. Above it the workflow runtime starts, stops, resets and resumes
    steps; at a step's boundaries – done, escalated, resumed, an earlier step redone – the
    active step tells the runtime (:class:`StepEvents`), and nothing else does."""

    def __init__(self, core: ChangeCore, ctx: PhaseContext, lifecycle: RunLifecycle,
                 provisioner: Provisioner):
        self.c, self.ctx, self.lifecycle, self.provisioner = core, ctx, lifecycle, provisioner
        self.states = NodeStates()
        self.runtime: StepEvents = None  # type: ignore[assignment]   # set once it exists
        self._active: dict[str, ActiveStep] = {}     # node -> its step as it runs
        self.waiting = 0                     # phase tasks waiting on the change's state

    def of(self, node: Node) -> ActiveStep:
        """The node's step as it runs (a node replaced – a fix round's – gets a new one)."""
        step = self._active.get(node.id)
        if step is None or step.node is not node:
            if step is not None:
                step.give_back()
            step = self._active[node.id] = ActiveStep(self, node)
        return step

    # ------------------------------------------------------------------ from the runtime (down)
    def start(self, node: Node, feedback: list[Feedback], rerun: bool = False) -> None:
        """The core started the node's step (with the feedback it was sent back with)."""
        self.of(node).start(feedback, rerun)

    def enter(self, node: Node) -> None:
        self.of(node).enter()

    def send_back(self, nodes: list[Node]) -> Awaitable[None]:
        """The core sent these steps back: each node starts over now; what their step types
        say a send-back undoes (``on_reset``, once per policy) is returned, to be awaited."""
        done: set[type] = set()
        todo = []
        for node in nodes:
            step = self.of(node)
            step.reset()
            for policy in step.type.on_reset:
                if type(policy) not in done and policy.applies(self.c, node):
                    done.add(type(policy))
                    todo.append((policy, node))

        async def undo() -> None:
            for policy, node in todo:
                await policy(self.c, self.lifecycle, node)
        return undo()

    def reset(self, node: Node) -> None:
        self.of(node).reset()

    def resume(self, node: Node, answered: bool = False) -> None:
        self.of(node).resume(answered)

    def override(self, node: Node) -> None:
        self.of(node).override()

    def retry(self, node: Node) -> None:
        self.of(node).retry()

    def end(self, node: Node) -> None:
        self.of(node).end()

    def again(self, node: Node) -> None:
        self.of(node).again()

    def recover(self, node: Node, failed: Run | None = None) -> None:
        self.of(node).recover(failed)

    def resume_retries(self) -> None:
        """After a restart: finish retries that were removing their old sandbox."""
        for node in self.c.nodes.values():
            esc = node.escalation
            if node.status in WAITING and esc and esc.retrying:
                run = self.c.runs.get(esc.run) if esc.run else None
                self.c.spawn(self.of(node).retry_after_removing(run.sandbox if run else ""))

    def housekeeping(self, active: bool) -> None:
        """On a save: kept sandboxes whose hold ended are released, a released step's idle
        sandboxes stopped; once the change has ended, the steps that do not run after it give
        their reservations back."""
        self.lifecycle.release_sandboxes()
        self.lifecycle.sandboxes.suspend_due()
        if not active:
            for nid, step in list(self._active.items()):
                if self.c.nodes.get(nid) is not step.node or not step.type.runs_after_end:
                    step.give_back()
