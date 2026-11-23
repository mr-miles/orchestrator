"""A change's in-memory records, where they copy its documents, are **projections** of them.

The document is the record: a field such as ``Change.approvals`` is set only from the document
it copies, when a new version of it is written (``DocWritten`` on the bus, published by the
store as the write commits), and on load by replaying the change's documents through the same
projectors. The state store does not persist projected fields (:data:`PROJECTED`): the
documents are their only durable copy, and a reader reads memory, never the store.

A projector is chosen by the document's **schema** (``DocWritten.schema``, bound to its path by
the store) and applied as ``apply(core, doc)``. To clear a projected value, write the document
again (a revocation) – never only the field.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from ....bus import DocWritten
from ....store import DocVersion
from ... import guards, risk
from ...model import (
    H_REQUIREMENTS,
    Approval,
    AttestationRecord,
    EnvironmentBuild,
    GateOverride,
    Node,
    Oracle,
    PanelDecision,
    PlanPointer,
    ProtectedPathApproval,
    Readiness,
    ReviewReport,
    Run,
    SurfacePin,
    Verification,
    cycle_of,
    step_record,
)

if TYPE_CHECKING:
    from .core import ChangeCore

# the fields of each record that are projections: not persisted by the state store
PROJECTED: dict[str, set[str]] = {
    "change": {"approvals", "requirements", "surface_pins", "protected_approvals", "overrides",
               "attestations", "ship", "environment", "oracle", "risk", "plan"},
    "node": {"verification", "panel", "reviews"},
}


def approval_key(rec: Approval) -> str:
    """Where an approval sits in ``Change.approvals``: its gate, or – an H-2 subject – the
    subject's key (``oracle``, ``spec:<subject>``)."""
    if rec.gate != "H-2":
        return rec.gate
    return "oracle" if rec.subject == "oracle" else f"spec:{rec.subject}"


def _approval(c: ChangeCore, doc: DocVersion) -> None:
    data = json.loads(doc.content)
    if data.get("revoked"):                       # e.g. the environment provisioned again
        c.change.approvals.pop(data["gate"], None)
        return
    rec = Approval.model_validate(data)
    rec.attestation = doc.attestation
    c.change.approvals[approval_key(rec)] = rec
    if rec.gate == H_REQUIREMENTS:                 # H-1 fixes the requirements it approved
        req = c.docs.read(c.p("intake/requirements.md"), rec.pins[c.p("intake/requirements.md")])
        c.change.requirements = guards.requirements_ids(req.content)


def _record(model, doc: DocVersion):
    rec = model.model_validate(json.loads(doc.content))
    rec.attestation = doc.attestation
    return rec


def _numbered(items: list, rec, doc: DocVersion) -> list:
    """A list of numbered records (``…/<n>.json``): this document's record, in its place."""
    mine = f"file:{doc.path}@"
    return [x for x in items if not (x.attestation or "").startswith(mine)] + [rec]


def _surface_pin(c: ChangeCore, doc: DocVersion) -> None:
    data = json.loads(doc.content)
    if data.get("revoked"):                       # the plan started again: no pins
        c.change.surface_pins.pop(data["surface"], None)
        return
    rec = _record(SurfacePin, doc)
    c.change.surface_pins[rec.surface] = rec


def _protected(c: ChangeCore, doc: DocVersion) -> None:
    c.change.protected_approvals = _numbered(c.change.protected_approvals,
                                             _record(ProtectedPathApproval, doc), doc)


def _override(c: ChangeCore, doc: DocVersion) -> None:
    c.change.overrides = _numbered(c.change.overrides, _record(GateOverride, doc), doc)


def _attestation(c: ChangeCore, doc: DocVersion) -> None:
    rec = _record(AttestationRecord, doc)
    c.change.attestations[rec.kind] = rec


def _ship(c: ChangeCore, doc: DocVersion) -> None:
    c.change.ship = _record(Readiness, doc)


def _environment(c: ChangeCore, doc: DocVersion) -> None:
    revoked = json.loads(doc.content).get("revoked")
    c.change.environment = None if revoked else _record(EnvironmentBuild, doc)


def _accepted(c: ChangeCore, doc: DocVersion) -> None:
    """The version of an agent's document the controller accepted: the oracle when it merged
    (and as H-2 approved it), the risk assessment when the intake report was accepted."""
    data = json.loads(doc.content)
    content = (None if data.get("revoked")
               else json.loads(c.docs.read(c.p(data["doc"]), data["version"]).content))
    if data["subject"] == "oracle":
        c.change.oracle = None if content is None else Oracle.model_validate(content)
    elif data["subject"] == "risk":
        c.change.risk = None if content is None else risk.derive_tier(content)


def _step(c: ChangeCore, doc: DocVersion) -> tuple[Node, str] | None:
    """The step a record is filed under (its path: :attr:`Node.records`) and its file name."""
    found = step_record(doc.path[len(c.p("")):])
    node = c.nodes.get(found[0]) if found else None
    return (node, found[2]) if node is not None else None


def _verification(c: ChangeCore, doc: DocVersion) -> None:
    """A step's verification: the node keeps a compact copy (the record has the output), by
    cycle – or, not a task, by its kind."""
    if (step := _step(c, doc)) is None:
        return
    node, name = step
    rec = _record(Verification, doc)
    key = str(cycle_of(name)) if node.task else str(node.kind)
    node.verification[key] = rec.model_copy(update={
        "results": [r.model_copy(update={"output": ""}) for r in rec.results]})


def _panel(c: ChangeCore, doc: DocVersion) -> None:
    if (step := _step(c, doc)) is not None:
        rec = _record(PanelDecision, doc)
        step[0].panel[str(rec.cycle)] = rec


def _review(c: ChangeCore, doc: DocVersion) -> None:
    """A judge's accepted report: its review on the node (the integration verifier's has one
    place, ``integration``). Only the controller's own record counts (G-W1)."""
    if doc.author != "controller" or (step := _step(c, doc)) is None:
        return
    node, name = step
    rec = ReviewReport.model_validate(json.loads(doc.content))
    key = ("integration" if rec.role == "integration"
           else node.review_key(cycle_of(name), rec.role))
    node.reviews[key] = rec.review


def _plan(c: ChangeCore, doc: DocVersion) -> None:
    """The plan worked to (``control/plan.json``)."""
    c.change.plan = PlanPointer.model_validate(json.loads(doc.content))


def _run_of(c: ChangeCore, doc: DocVersion) -> tuple[str, str] | None:
    """``(node id, run id)`` of a run document's path: ``tasks/<task>/runs/<run>/…`` (a build
    task's) or ``control/steps/<node>/runs/<run>/…``."""
    p = doc.path[len(c.p("")):].split("/")
    if len(p) == 5 and p[0] == "tasks" and p[2] == "runs":
        return f"build:{p[1]}", p[3]
    if len(p) == 6 and p[:2] == ["control", "steps"] and p[3] == "runs":
        return p[2], p[4]
    return None


def _run_assignment(c: ChangeCore, doc: DocVersion) -> None:
    """A run, from its assignment (live until it has an outcome). A run already in memory is
    the controller's own: it wrote the document."""
    if (where := _run_of(c, doc)) is None or where[1] in c.runs:
        return
    node_id, rid = where
    c.runs[rid] = Run.model_validate({**json.loads(doc.content), "id": rid, "node_id": node_id})


def _run_outcome(c: ChangeCore, doc: DocVersion) -> None:
    """How a run ended (each version: as its conclusion moved on)."""
    if (where := _run_of(c, doc)) is None or (run := c.runs.get(where[1])) is None:
        return
    data = json.loads(doc.content)
    ended = Run.model_validate({**run.model_dump(mode="json"), **data})
    for k in data:
        setattr(run, k, getattr(ended, k))


# schema -> what a new version of a document of it projects into
PROJECTORS: dict[str, Callable[[ChangeCore, DocVersion], None]] = {
    "approval": _approval,
    "protected-approval": _protected,
    "surface-pin": _surface_pin,
    "override": _override,
    "attestation": _attestation,
    "readiness": _ship,
    "environment-build": _environment,
    "accepted": _accepted,
    "verification": _verification,
    "panel-decision": _panel,
    "report": _review,
    "plan-pointer": _plan,
    "run-assignment": _run_assignment,
    "run-outcome": _run_outcome,
}


def _replay_order(m) -> tuple:
    """Runs before what refers to them – each run's assignment first, in run order, then its
    outcome – then every other document oldest first."""
    if m.schema in ("run-assignment", "run-outcome"):
        rid = m.path.rsplit("/", 2)[-2]
        return (0 if m.schema == "run-assignment" else 1, int(rid[1:]) if rid[1:].isdigit() else 0)
    return (2, m.created_at, m.path)



class Projections:
    """One change's projectors: applied as its documents are written, and replayed on load."""

    def __init__(self, core: ChangeCore):
        self.c = core
        self._prefix = core.p("")

    def written(self, event: DocWritten) -> None:
        """A document of the change was written (a bus handler)."""
        if (apply := PROJECTORS.get(event.schema)) and self.c.change is not None:
            apply(self.c, self.c.doc(event.path, event.version))

    def replay(self) -> None:
        """Every projected field from the documents as they stand (on load), oldest first."""
        metas = sorted((m for m in self.c.docs.list(self._prefix) if m.schema in PROJECTORS),
                       key=_replay_order)
        for m in metas:
            PROJECTORS[m.schema](self.c, self.c.docs.read(m.path, m.version))
