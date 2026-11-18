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
from ..model import ACCEPTED, Finding, Node, Record, Run
from ..view import RoleContext


@dataclass
class Report:
    """An accepted-so-far report, after the shared guards passed."""
    run: Run
    node: Node
    parsed: list[Attestation]
    data: Record                     # typed report data (workflow/report_data.py)
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
    # Keeps its sandbox (stopped between runs) and its agent session across the node's rework
    # cycles until this gate node is accepted: a rework continues the earlier conversation.
    keeps_session_until: str | None = None
    needs_fresh_approval = False  # spec/oracle approval must be fresh before dispatch (G-B1)
    # ---- context given to the run (attestations listed in the prompt / assignment)
    sees_requirements = True      # accepted requirements and risk (H-1 pins)
    sees_draft_plan = False       # latest plan documents, even after H-2 approval
    sees_oracle_commit = True     # the protected oracle commit
    sees_risk = True              # intake/risk.json among the H-1 pins
    # A build task's roles: of the approved delta specs, only those of the task's own
    # requirements (the others are about other tasks).
    task_scoped_specs = False

    @property
    def keeps_sandbox(self) -> bool:
        """Its node holds the sandbox between runs (``node.sandbox``)."""
        return self.reuses_sandbox or self.keeps_session_until is not None

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        """Role-specific context attestations beyond the common ones."""
        return []

    def payload_files(self, e: RoleContext, node: Node, run: Run) -> dict[str, str]:
        """Extra files for the run's payload (path relative to the payload directory -> text)."""
        return {}

    def extra_variables(self, e: RoleContext, node: Node) -> dict[str, str]:
        """Role-specific prompt template variables beyond the common ones."""
        return {}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return []

    async def on_done(self, e: RoleContext, r: Report) -> None:
        """Default: the work is accepted; the node completes and its sandboxes are deleted."""
        r.node.status = ACCEPTED
        e.accept_runs(r.node)


def brief_findings(findings: list[Finding]) -> list[Finding]:
    """A report's findings, compacted for the node record."""
    return [f.brief() for f in findings[:30]]


def build_context(e: RoleContext, node: Node) -> list[str]:
    """Context of a build task: its declared context and its accepted dependencies' commits."""
    refs = list(node.task.context) if node.task else []
    for d in node.depends_on:
        dn = e.nodes.get(d)
        if dn and dn.task and dn.accepted_sha:
            refs.append(f"git:{e.cfg.repo.name}@{dn.accepted_sha}")
    return refs


def fix_round_note(e: RoleContext, role: str) -> str:
    """The ``fix_round`` prompt variable: what a fix round asks of the planner or the critic
    (empty outside a round)."""
    rnd = e.change.rounds[-1] if e.change.rounds else None
    if rnd is None or e.change.round != rnd.number:
        return ""
    built = [b for b in e.build_nodes() if b.round < rnd.number]
    tasks = "\n".join(f"- {b.task.id} (round {b.round}): {b.task.title}, accepted at "
                      f"`{(b.accepted_sha or '?')[:12]}`" for b in built) or "- (none)"
    failure = "\n".join(f"- {a}" for a in rnd.failure) or "- (the integration escalation)"
    head = (f"## Fix round {rnd.number}\n\nIntegration failed at `{rnd.head[:12]}`. The "
            f"failure (read all of it):\n{failure}\n\nDecision owner's comment: "
            f"{rnd.comment or '(none)'}\n\nTasks already built:\n{tasks}\n\n")
    if role == "critic":
        return head + (
            "Judge the round: the tasks it appends to `plan/taskgraph.json` and any document "
            "it revised. Together the new tasks must address every failure named above, and "
            "each must say in its `instructions` which failure it fixes and why.")
    return head + (
        "Plan new work on top of the integration head, as if the change started from the "
        "existing code; accepted tasks are never reopened. Revise `plan/taskgraph.json` by "
        "**appending** one or more tasks with ids no task has used, keeping every task above "
        "exactly as it is (G-FR2). Each new task's `instructions` say which failure it fixes "
        "and why, and its `context` cites the integration report. A new task may depend on "
        "accepted tasks and modify their files, and cites at least one accepted requirement "
        "(G-FR1); requirements are unchanged. Revise `plan/spec.md` or a delta spec only if "
        "the cause is a shared interface or contract, `plan/oracle.json` only if the "
        "acceptance tests themselves must change, and the sandbox environment only if the "
        "sandboxes must change.")


def evidence_of(e: RoleContext, node: Node) -> str | None:
    return e.latest_att(f"tasks/{node.task.id}/evidence/cycle-{node.cycle}.md")


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
