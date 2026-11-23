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
    Oracle,
    PanelDecision,
    ProtectedPathApproval,
    Readiness,
    SurfacePin,
    Verification,
)

if TYPE_CHECKING:
    from .core import ChangeCore

# the fields of each record that are projections: not persisted by the state store
PROJECTED: dict[str, set[str]] = {
    "change": {"approvals", "requirements", "surface_pins", "protected_approvals", "overrides",
               "attestations", "ship", "environment", "oracle", "risk"},
    "node": {"verification", "panel"},
}


def approval_key(rec: Approval) -> str:
    """Where an approval sits in ``Change.approvals``: its gate, or – an H-2 subject – the
    subject's key (``oracle``, ``spec:<subject>``)."""
    if rec.gate != "H-2":
        return rec.gate
    return "oracle" if rec.subject == "oracle" else f"spec:{rec.subject}"


def _approval(c: ChangeCore, doc: DocVersion) -> None:
    data = json.loads(doc.content)
    if "gate" not in data:          # a protected-path approval of an older store (same schema)
        return
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


def _verification(c: ChangeCore, doc: DocVersion) -> None:
    """A step's verification: the node keeps a compact copy (the record has the output)."""
    rec = _record(Verification, doc)
    node = c.nodes.get(rec.node or "")
    if node is None or rec.key is None:      # an older store's record says neither: not projected
        return
    node.verification[rec.key] = rec.model_copy(update={
        "results": [r.model_copy(update={"output": ""}) for r in rec.results]})


def _panel(c: ChangeCore, doc: DocVersion) -> None:
    rec = _record(PanelDecision, doc)
    if (node := c.nodes.get(rec.node or "")) is not None:
        node.panel[str(rec.cycle)] = rec


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
}



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
                       key=lambda m: (m.created_at, m.path))
        for m in metas:
            PROJECTORS[m.schema](self.c, self.c.docs.read(m.path, m.version))
