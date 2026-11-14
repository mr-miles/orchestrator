"""Role handlers: the role-specific half of handling an agent's ``report``.

The engine checks what every role shares (report shape, attestations, required documents and git
result) and then calls the handler registered for the run's role:

* :meth:`RoleHandler.guards` – role-specific guards; returns violation strings,
* :meth:`RoleHandler.on_done` – the transition after an accepted ``DONE`` report.

Adding a role = assets (``roles.yaml``, soul, prompt, schemas) + one handler module here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..attest import Attestation
from ..model import ACCEPTED, Node, Run
from ..view import RoleContext


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

    # ---- dispatch policy (read by the engine instead of hard-coded role names)
    pushes = False                # pushes its own branch: one branch per node, git push allowed
    judge = False                 # judges another run's work: always a fresh sandbox (G-D4)
    panel = False                 # review-panel member: dispatched per cycle on the candidate
    judges_head = False           # works at the integration head (judged sha = head)
    reuses_sandbox = False        # keeps its sandbox across the node's rework cycles
    needs_fresh_approval = False  # spec/oracle approval must be fresh before dispatch (G-B1)
    # ---- context given to the run (attestations listed in the prompt / assignment)
    sees_requirements = True      # accepted requirements and risk (H-1 pins)
    sees_draft_plan = False       # latest plan documents, even after H-2 approval
    sees_oracle_commit = True     # the protected oracle commit

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        """Role-specific context attestations beyond the common ones."""
        return []

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return []

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """Default: the work is accepted; the node completes and its sandboxes are deleted."""
        r.node.status = ACCEPTED
        e.accept_runs(r.node)


def brief_findings(findings: list[dict]) -> list[dict]:
    """Compact copy for node records (the run record and the review document keep the full text),
    so repeated review cycles cannot grow a node record without bound."""
    keep = ("severity", "confidence", "dimension", "title")
    return [{**{k: f.get(k) for k in keep if k in f},
             "observation": str(f.get("observation", ""))[:500], "impact": str(f.get("impact", ""))[:300],
             "fix": str(f.get("fix", ""))[:500], "attestations": list(f.get("attestations", []))[:5]}
            for f in findings[:30] if isinstance(f, dict)]


def build_context(e: RoleContext, node: Node) -> list[str]:
    """Context of a build task: its declared context and its accepted dependencies' commits."""
    refs = [c for c in (node.task or {}).get("context", []) if isinstance(c, str)]
    for d in node.depends_on:
        dn = e.nodes.get(d)
        if dn and dn.task and dn.accepted_sha:
            refs.append(f"git:{e.cfg.repo.name}@{dn.accepted_sha}")
    return refs


def evidence_of(e: RoleContext, node: Node) -> str | None:
    return e.latest_att(f"tasks/{node.task['id']}/evidence/cycle-{node.cycle}.md")


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


def roles_where(attr: str) -> set[str]:
    """Role names whose handler declares ``attr`` true, e.g. ``roles_where("pushes")``."""
    return {name for name, h in REGISTRY.items() if getattr(h, attr)}
