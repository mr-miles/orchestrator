"""The shape of a change's workflow (plan: stages-and-steps.md §1): stages nest, a stage holds
steps, smaller stages and parallel groups of steps, and may end with a gate – an ordinary step
that runs once the rest of the stage is done (a human approving: its work escalates until they
do). Plain data – what it means to run them is :mod:`.core`."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field


@dataclass
class Step:
    """One unit of work. The core only knows its id and what humans may do with it."""
    id: str
    overridable: bool = False          # a human may accept it without its own checks (builds)
    # done, it may run again in place – as a later escalated step's answer – while what was
    # built on it stands (provisioning: the same approved spec, built again)
    rerunnable: bool = False
    rejectable: bool = False           # escalated, a human may reject it: the change ends (H-3)
    depends_on: list[str] = field(default_factory=list)   # within its Parallel group


@dataclass
class Parallel:
    """Steps that run as their dependencies allow (a task graph); done when all are. Its steps
    may be known only later (:meth:`.FlowState.define`); empty, it is done at once."""
    id: str
    steps: list[Step]


@dataclass
class StartOver:
    """Sent back: everything in the stage starts again."""


@dataclass
class AppendRound:
    """Sent back: the work done stays; a new round (``template(n)``) is inserted before child
    ``before``, which runs again with everything after it (None: after the last child)."""
    template: Callable[[int], Stage]
    before: str | None = None


@dataclass
class Stage:
    id: str
    children: list[Stage | Step | Parallel]
    gate: Step | None = None           # runs last, once the children are done
    rework: StartOver | AppendRound = field(default_factory=StartOver)
    feedback_to: str | None = None     # the step a send-back's feedback goes to (default: the first)


@dataclass
class Workflow:
    """A change: its stages, in order."""
    stages: list[Stage]


Node = Stage | Step | Parallel


def walk(node: Stage | Step | Parallel) -> Iterator[Node]:
    """``node`` and everything in it, in execution order (a stage's gate last)."""
    yield node
    if isinstance(node, Stage):
        for child in node.children:
            yield from walk(child)
        if node.gate is not None:
            yield node.gate
    elif isinstance(node, Parallel):
        yield from node.steps
