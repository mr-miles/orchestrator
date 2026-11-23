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
from ...model import Approval

if TYPE_CHECKING:
    from .core import ChangeCore

# the fields of each record that are projections: not persisted by the state store
PROJECTED: dict[str, set[str]] = {"change": {"approvals"}}


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


# schema -> what a new version of a document of it projects into
PROJECTORS: dict[str, Callable[[ChangeCore, DocVersion], None]] = {
    "approval": _approval,
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
