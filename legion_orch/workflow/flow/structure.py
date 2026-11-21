"""The shape of a change's workflow (plan: stages-and-steps.md §1): stages nest, every stage ends
with a gate, a stage holds steps, smaller stages and parallel groups of steps. Plain data – what
it means to run them is :mod:`.core`."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field


@dataclass
class Step:
    """One unit of work. The core only knows its id and what humans may do with it."""
    id: str
    overridable: bool = False          # a human may accept it without its own checks (builds)
    depends_on: list[str] = field(default_factory=list)   # within its Parallel group


@dataclass
class Parallel:
    """Steps that run as their dependencies allow (a task graph); done when all are. Its steps
    may be known only later (:meth:`.FlowState.define`); empty, it is done at once."""
    id: str
    steps: list[Step]


@dataclass
class Gate:
    """A stage's end: an approval (by people, or automatic) plus deterministic preconditions the
    runtime evaluates. The stage completes when both hold."""
    id: str
    subjects: list[str] = field(default_factory=list)   # approved separately (none: the gate)
    preconditions: bool = False        # the runtime evaluates checks before the gate completes
    automatic: bool = False            # approved by the controller once the stage's work is done
    rejectable: bool = False           # rejecting it ends the change (the last gate)


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
    gate: Gate
    rework: StartOver | AppendRound = field(default_factory=StartOver)
    feedback_to: str | None = None     # the step a send-back's feedback goes to (default: the first)


@dataclass
class Workflow:
    """A change: its stages, in order."""
    stages: list[Stage]


Node = Stage | Step | Parallel | Gate


def walk(node: Stage | Step | Parallel) -> Iterator[Node]:
    """``node`` and everything in it, in execution order (a stage's gate last)."""
    yield node
    if isinstance(node, Stage):
        for child in node.children:
            yield from walk(child)
        yield node.gate
    elif isinstance(node, Parallel):
        yield from node.steps
