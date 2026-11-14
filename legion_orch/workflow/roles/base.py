"""Role handlers: the role-specific half of handling an agent's ``report``.

The engine checks what every role shares (report shape, attestations, required documents and git
result) and then calls the handler registered for the run's role:

* :meth:`RoleHandler.guards` – role-specific guards; returns violation strings,
* :meth:`RoleHandler.on_done` – the transition after an accepted ``DONE`` report.

Adding a role = assets (``roles.yaml``, soul, prompt, schemas) + one handler module here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..attest import Attestation
from ..model import ACCEPTED, Node, Run

if TYPE_CHECKING:
    from ..engine import ChangeEngine


@dataclass
class Report:
    """An accepted-so-far report, after the shared guards passed."""
    run: Run
    node: Node
    parsed: list[Attestation]
    data: dict
    docs: dict[str, Attestation] = field(default_factory=dict)   # required docs, by full path
    sha: str | None = None                                        # required git result
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def repo_atts(self) -> set[str]:
        return {str(a) for a in self.parsed}


class RoleHandler:
    roles: tuple[str, ...] = ()

    async def guards(self, e: ChangeEngine, r: Report) -> list[str]:
        return []

    async def on_done(self, e: ChangeEngine, r: Report) -> None:
        """Default: the work is accepted; the node completes and its sandboxes are deleted."""
        r.node.status = ACCEPTED
        e._accept_runs(r.node)


REGISTRY: dict[str, RoleHandler] = {}


def register(cls: type[RoleHandler]) -> type[RoleHandler]:
    inst = cls()
    for role in cls.roles:
        if role in REGISTRY:
            raise ValueError(f"role {role!r} registered twice")
        REGISTRY[role] = inst
    return cls


def handler_for(role: str) -> RoleHandler:
    try:
        return REGISTRY[role]
    except KeyError:
        raise ValueError(f"no handler registered for role {role!r}") from None
