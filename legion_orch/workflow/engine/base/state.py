"""StateStore: one change's state – its change, node and run records – live in the state
tables, its history in the journal."""
from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from typing import Any

from ....store import DocStore, SchemaViolation
from ....store.journal import REMOVED, Journal, diff
from ....store.state_tables import CHANGE, WORKFLOW, StateTables
from ...model import Change, Node, Run
from .projections import PROJECTED

CONTROL = "control/"
_KINDS = {"node": ("nodes", "node"), "run": ("runs", "run")}     # record prefix -> (attr, schema)
# the gateway's to say, on start (Sandboxes.adopt): whether a sandbox exists, whether it is stopped
GATEWAY = {"node": {"sandbox_stopped"}, "run": {"sandbox_deleted", "sandbox_stopped"}}


class StateStore:
    """A change's records: ``change``, ``workflow`` (:class:`.FlowRuntime`'s tree and inputs,
    which order the nodes once it is rebuilt), ``node:<id>`` and ``run:<id>`` – the fields the
    engine does not project from documents (:mod:`.projections`) nor learn from the gateway on
    start (:data:`GATEWAY`: which sandboxes exist and which are stopped).

    * **Live state** – the state tables in ``state.db`` (:mod:`legion_orch.store.state_tables`):
      each record's current values, updated in place. A restart loads only these.
    * **History** – the journal, ``<state_dir>/journal/<change>.jsonl``
      (:mod:`legion_orch.store.journal`): per save, one line of what changed (a record's changed
      fields; a list that only grew, its new items; a removal). Append-only, never read on start.

    :meth:`save` does both for the records that changed since the last save: the tables first
    (one transaction), then the journal line.
    """

    def __init__(self, docs: DocStore, tables: StateTables, journal: Journal, cid: str):
        self.docs = docs
        self.tables = tables
        self.journal = journal
        self.cid = cid
        self.root = f"changes/{cid}/{CONTROL}"
        self.change: Change | None = None
        self.nodes: dict[str, Node] = {}
        self.runs: dict[str, Run] = {}
        self.workflow: dict[str, Any] | None = None   # set by FlowRuntime before each save
        self._last: dict[str, dict[str, Any]] = {}   # record -> its fields as last saved

    # ------------------------------------------------------------- persistence
    def _current(self) -> dict[str, dict[str, Any]]:
        out = {CHANGE: self.change.model_dump(mode="json", exclude=PROJECTED["change"])}
        if self.workflow is not None:
            out[WORKFLOW] = self.workflow
        out |= {f"node:{k}": n.model_dump(mode="json", exclude=PROJECTED["node"] | GATEWAY["node"])
                for k, n in self.nodes.items()}
        out |= {f"run:{k}": r.model_dump(mode="json", exclude=GATEWAY["run"])
                for k, r in self.runs.items()}
        return out

    def _check(self, record: str, data: dict[str, Any]) -> None:
        """A record's full form is valid against its schema (guard G-W3: a run record never
        carries a credential, say)."""
        if record == WORKFLOW:
            return                                   # the core's own record: no schema
        name = CHANGE if record == CHANGE else _KINDS[record.partition(":")[0]][1]
        if errs := self.docs.schemas.validate_data(name, data):
            raise SchemaViolation(f"G-W3: {record} does not match schema {name!r}: "
                                  + "; ".join(errs))

    def save(self) -> None:
        """Persist what changed since the last save: its rows, then its journal line."""
        if self.change is None:
            return
        now = self._current()
        entries: list[tuple[str, dict[str, Any]]] = []
        for record, data in now.items():
            old = self._last.get(record)
            if old is None:
                self._check(record, data)
            if entry := diff(old, data):
                entries.append((record, entry))
        gone = [r for r in self._last if r not in now]
        if not entries and not gone:
            return
        self.tables.write(self.cid, {r: now[r] for r, _ in entries}, gone)
        self.journal.append(self.cid, entries + [(r, {REMOVED: True}) for r in gone])
        self._last = now

    def load(self) -> None:
        """The change as its tables hold it (on controller start)."""
        records = self.tables.load(self.cid)
        self.change = Change.model_validate(records[CHANGE])
        self.workflow = records.get(WORKFLOW)
        for record, data in records.items():
            kind, _, key = record.partition(":")
            if kind in _KINDS:
                attr = getattr(self, _KINDS[kind][0])
                attr[key] = (Node if kind == "node" else Run).model_validate(data)
        self._last = self._current()

    # ------------------------------------------------------------- transactions
    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Apply a transition all-or-nothing: if the block raises, the change, nodes and runs
        are restored to their state at entry and the exception propagates. Restoring keeps
        object identity, so coroutines holding a Node or Run keep seeing the live object.
        (Nothing is saved here – appends happen in :meth:`save` – and external effects such as
        git merges are not undone.)"""
        objs = [self.change, *self.nodes.values(), *self.runs.values()]
        change, nodes, runs = self.change, dict(self.nodes), dict(self.runs)
        states = {id(o): copy.deepcopy(o.__dict__) for o in objs if o is not None}
        try:
            yield
        except BaseException:
            self.change = change
            self.nodes.clear()
            self.nodes.update(nodes)
            self.runs.clear()
            self.runs.update(runs)
            for o in objs:
                if o is not None:
                    o.__dict__.clear()
                    o.__dict__.update(states[id(o)])
            raise
