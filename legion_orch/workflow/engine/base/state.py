"""StateStore: one change's state – its change, node and run records – journalled."""
from __future__ import annotations

import contextlib
import copy
from collections.abc import Iterator
from typing import Any

from ....store import DocStore, SchemaViolation
from ....store.journal import REMOVED, SNAPSHOT, Journal, diff
from ...model import Change, Node, Run
from .projections import PROJECTED

CONTROL = "control/"
CHANGE = "change"
WORKFLOW = "workflow"       # the workflow core's record (its tree and inputs, FlowRuntime)
_KINDS = {"node": ("nodes", "node"), "run": ("runs", "run")}     # record prefix -> (attr, schema)


class StateStore:
    """A change's records, persisted as an append-only journal – a JSON Lines text file,
    ``<state_dir>/journal/<change>.jsonl`` (:mod:`legion_orch.store.journal`) – no other durable
    state.

    The records are ``change``, ``node:<id>`` and ``run:<id>``. :meth:`save` appends, per record
    that changed since it last did, one entry of the fields that changed (a list that only grew:
    its new items); a node dropped from the change is a removal entry. Every
    ``SNAPSHOT_EVERY`` entries, and on every load, a snapshot (every live record in full)
    is appended, and a load folds from the latest one. Nothing is rewritten.

    The change's structure – which nodes exist, their order and dependencies – is the workflow's
    tree: the record ``workflow`` (:class:`.FlowRuntime`'s tree and inputs), which orders the
    nodes once it is rebuilt.

    """

    SNAPSHOT_EVERY = 1000

    def __init__(self, docs: DocStore, journal: Journal, cid: str):
        self.docs = docs
        self.journal = journal
        self.cid = cid
        self.root = f"changes/{cid}/{CONTROL}"
        self.change: Change | None = None
        self.nodes: dict[str, Node] = {}
        self.runs: dict[str, Run] = {}
        self.workflow: dict[str, Any] | None = None   # set by FlowRuntime before each save
        self._last: dict[str, dict[str, Any]] = {}   # record -> its fields as last journalled
        self._since = 0                              # entries since the latest snapshot

    # ------------------------------------------------------------- persistence
    def _current(self) -> dict[str, dict[str, Any]]:
        out = {CHANGE: self.change.model_dump(mode="json", exclude=PROJECTED["change"])}
        out |= {f"node:{k}": n.model_dump(mode="json", exclude=PROJECTED["node"])
                for k, n in self.nodes.items()}
        out |= {f"run:{k}": r.model_dump(mode="json") for k, r in self.runs.items()}
        if self.workflow is not None:
            out[WORKFLOW] = self.workflow
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
        """Persist what changed since the last save: one append (a transaction)."""
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
        entries += [(r, {REMOVED: True}) for r in self._last if r not in now]
        if not entries:
            return
        self.journal.append(self.cid, entries)
        self._last = now
        self._since += len(entries)
        if self._since >= self.SNAPSHOT_EVERY:
            self._snapshot(now)

    def _snapshot(self, now: dict[str, dict[str, Any]]) -> None:
        for record, data in now.items():
            self._check(record, data)
        self.journal.append(self.cid, [(SNAPSHOT, {}), *now.items()])   # one line
        self._last, self._since = now, 0

    def load(self) -> None:
        records = self.journal.fold(self.cid)
        self.change = Change.model_validate(records[CHANGE])
        self.workflow = records.get(WORKFLOW)
        for record, data in records.items():
            kind, _, key = record.partition(":")
            if kind in _KINDS:
                attr = getattr(self, _KINDS[kind][0])
                attr[key] = (Node if kind == "node" else Run).model_validate(data)
        self._snapshot(self._current())

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
