"""Sandbox plans: where a ticket's agent works, as data – what is checked out, whether it pushes,
and whether its sandbox (and agent session) is kept. :class:`.Sandboxes` carries a plan out.

The plan follows from the ticket, never from a role's name (:func:`plan_for`):

* a **criterion's** agent (a judge) gets a fresh sandbox per round, detached at the commit it
  judges (G-D4: never the judged work's sandbox);
* a ticket's **own work** keeps its sandbox and session across its attempts, until the ticket is
  closed: on its own branch if the role commits (it pushes), else detached at its base."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ...assets import GitMode
from ...tickets import Status, Ticket

CLOSED = {Status.PASSED, Status.FAILED, Status.CANCELLED}


class Checkout(StrEnum):
    BRANCH = "branch"          # its own branch (from its tip on the hub, else its base): it pushes
    BASE = "base"              # detached at its base (its merge target's head when it started)
    CANDIDATE = "candidate"    # detached at the commit it judges


@dataclass(frozen=True)
class SandboxPlan:
    checkout: Checkout
    keeps: bool = False        # the ticket holds the sandbox (and session) between its rounds

    @property
    def pushes(self) -> bool:
        return self.checkout == Checkout.BRANCH

    @property
    def keeps_session(self) -> bool:
        return self.keeps


JUDGE = SandboxPlan(Checkout.CANDIDATE)


def plan_for(ticket: Ticket, git: GitMode) -> SandboxPlan:
    """The plan of ``ticket``'s agent, whose role has ``git``."""
    if ticket.role == "criterion":
        return JUDGE
    return SandboxPlan(Checkout.BRANCH if git == GitMode.COMMIT else Checkout.BASE, keeps=True)


def held(ticket: Ticket) -> bool:
    """Whether a ticket still keeps its sandbox for another round: until it is closed."""
    return ticket.status not in CLOSED


class Environments(Protocol):
    """Whether a role's environment is being built again – the provisioner knows; the sandbox
    layer is given it, so no sandbox starts from the old build."""

    def waits_for_environment(self, role: str) -> bool:
        """``role``'s sandboxes wait: its environment is being built again."""
