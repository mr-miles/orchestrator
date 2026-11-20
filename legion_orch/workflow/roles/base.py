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
from ..model import Finding, Node, Record, Run
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

    # ---- dispatch policy (where its agent works is its step's sandbox plan: engine/steps.py)
    judge = False                 # judges another run's work: always a fresh sandbox (G-D4)
    panel = False                 # judge dispatched per cycle on the candidate (node REVIEWING)
    needs_fresh_approval = False  # spec/oracle approval must be fresh before dispatch (G-B1)
    # ---- context given to the run (attestations listed in the prompt / assignment)
    sees_requirements = True      # accepted requirements and risk (H-1 pins)
    sees_draft_plan = False       # latest plan documents, even after H-2 approval
    sees_oracle_commit = True     # the protected oracle commit
    sees_risk = True              # intake/risk.json among the H-1 pins
    # A build task's roles: of the approved delta specs, only those of the task's own
    # requirements (the others are about other tasks).
    task_scoped_specs = False

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
        """Default: the agent's phase passed; the step goes on (accepted after its last
        phase, which deletes its sandboxes)."""
        e.step_done(r.node)


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


def scaffold_note(e: RoleContext, role: str) -> str:
    """The ``scaffold`` prompt variable (§5.3b): in a blank repository, what the scaffold task
    asks of the planner, the critic or the oracle (empty otherwise)."""
    if not e.change.blank_repo:
        return ""
    if role == "oracle":
        s = e.scaffold_node()
        if s is None or not s.accepted_sha:
            return ""
        return (f"## Scaffold\n\nThe repository was blank; scaffold task {s.task.id} "
                f"(\"{s.task.title}\") set up its build tooling and is merged at "
                f"`{s.accepted_sha[:12]}`, in your working copy. Write the acceptance tests "
                "for that layout, test runner and configuration – do not invent another or "
                "change the scaffold's files – so that every failure means \"the implementation "
                "is missing\".")
    head = ("## Blank repository\n\nThe repository has no build tooling or code yet (only "
            "documentation, licence or git settings). ")
    if role == "critic":
        return head + (
            "The plan must start with one scaffold task (`\"scaffold\": true`, G-P11), built "
            "before the oracle: check that it only sets up the project – layout, build and test "
            "tooling, configuration, a placeholder entry point – and none of the requirements' "
            "behaviour; that its verification commands prove the tooling works on its own (no "
            "acceptance test exists yet); and that it fixes every choice the oracle's tests "
            "depend on (test runner, its configuration, paths, how the system is started).")
    return head + (
        "Plan one **scaffold task** first, marked `\"scaffold\": true` (G-P11): no dependencies, "
        "and every other task depends on it. It is built, verified and reviewed before the "
        "oracle, so the acceptance tests are written against a real project instead of a guess. "
        "It sets up only the project – layout, package manifest, build, lint and test tooling "
        "and their configuration, a placeholder entry point – none of the requirements' "
        "behaviour (cite the requirements whose stack it sets up). Its `instructions` fix every "
        "choice the tests depend on: test runner and configuration, file layout, how the system "
        "is started. Its `verification` commands must pass on the scaffold alone (no acceptance "
        "test exists yet). Keep the oracle's acceptance tests and `protected_support` out of "
        "its `files_modified`.")


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
    """Role names whose handler declares ``attr`` true, e.g. ``roles_where("panel")``."""
    return {name for name, h in REGISTRY.items() if getattr(h, attr)}
