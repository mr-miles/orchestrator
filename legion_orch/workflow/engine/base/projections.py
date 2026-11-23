"""A change's in-memory records, where they copy its documents, are **projections** of them.

The document is the record: a field such as ``Change.environment`` is set only from the document
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
from ...model import EnvironmentBuild, Run

if TYPE_CHECKING:
    from .core import ChangeCore

# the fields of each record that are projections: not persisted by the state store
PROJECTED: dict[str, set[str]] = {"change": {"environment"}}


def _record(model, doc: DocVersion):
    rec = model.model_validate(json.loads(doc.content))
    rec.attestation = doc.attestation
    return rec


def _environment(c: ChangeCore, doc: DocVersion) -> None:
    revoked = json.loads(doc.content).get("revoked")
    c.change.environment = None if revoked else _record(EnvironmentBuild, doc)


def _run_id(doc: DocVersion) -> str:
    """A run document's run: ``control/runs/<run>/…``."""
    return doc.path.rsplit("/", 2)[-2]


def _run_assignment(c: ChangeCore, doc: DocVersion) -> None:
    """A run, from its assignment (live until it has an outcome; it names its ticket). A run
    already in memory is the controller's own: it wrote the document."""
    if (rid := _run_id(doc)) in c.runs:
        return
    c.runs[rid] = Run.model_validate({**json.loads(doc.content), "id": rid})


def _run_outcome(c: ChangeCore, doc: DocVersion) -> None:
    """How a run ended (each version: as its conclusion moved on)."""
    if (run := c.runs.get(_run_id(doc))) is None:
        return
    data = json.loads(doc.content)
    ended = Run.model_validate({**run.model_dump(mode="json"), **data})
    for k in data:
        setattr(run, k, getattr(ended, k))


# schema -> what a new version of a document of it projects into
PROJECTORS: dict[str, Callable[[ChangeCore, DocVersion], None]] = {
    "environment-build": _environment,
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
