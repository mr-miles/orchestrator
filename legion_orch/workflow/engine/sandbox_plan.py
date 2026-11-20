"""Sandbox plans (plan: stages-and-steps.md §2, the sandbox layer): where a role's agent works,
declared as data – what is checked out, whether it pushes, and how long its sandbox (and agent
session) is kept. :class:`.Sandboxes` carries a plan out; the step types say which plan each
producer has (:mod:`.steps`), and every judge has :data:`JUDGE`."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..model import ACCEPTED, Node
from .core import ChangeCore


class Checkout(StrEnum):
    BRANCH = "branch"          # its own branch (from its tip on the hub, else its base): it pushes
    BASE = "base"              # detached at the node's base
    HEAD = "head"              # detached at the integration head (G-H1 checked at dispatch)
    CANDIDATE = "candidate"    # detached at the commit it judges


@dataclass(frozen=True)
class Fresh:
    """A new sandbox for every run (judges: G-D4)."""


@dataclass(frozen=True)
class UntilAccepted:
    """Kept across the node's rework cycles until the node is accepted."""


@dataclass(frozen=True)
class UntilGate:
    """Kept, stopped between runs, with its agent session, until ``gate`` is accepted: a rework
    continues the earlier conversation."""
    gate: str


Keep = Fresh | UntilAccepted | UntilGate


@dataclass(frozen=True)
class SandboxPlan:
    checkout: Checkout
    keep: Keep = Fresh()

    @property
    def pushes(self) -> bool:
        return self.checkout == Checkout.BRANCH

    @property
    def at_head(self) -> bool:
        return self.checkout == Checkout.HEAD

    @property
    def keeps(self) -> bool:
        """The node holds the sandbox between runs (``node.sandbox``)."""
        return not isinstance(self.keep, Fresh)

    @property
    def keeps_session(self) -> bool:
        return isinstance(self.keep, UntilGate)

    def held(self, c: ChangeCore, node: Node) -> bool:
        """Whether the node still keeps its sandbox for rework."""
        if isinstance(self.keep, UntilGate):
            gate = c.nodes.get(self.keep.gate)
            return gate is not None and gate.status != ACCEPTED
        if isinstance(self.keep, UntilAccepted):
            return node.status != ACCEPTED
        return False


JUDGE = SandboxPlan(Checkout.CANDIDATE)
