"""StateStore: one change's state – its change, node and run records – live in the state
tables, its history in the journal."""
from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Iterator
from typing import Any

from ....store import DocStore, SchemaViolation
from ....store.journal import REMOVED, Journal, diff
from ....store.state_tables import CHANGE, WORKFLOW, StateTables
from ...model import LIVE_RUN, Change, Node, Run
from .projections import PROJECTED

CONTROL = "control/"
_KINDS = {"node": ("nodes", "node")}                  # record prefix -> (attr, schema)
# the gateway's to say, on start (Sandboxes.adopt): whether a sandbox exists, whether it is stopped
GATEWAY = {"node": {"sandbox_stopped"}, "run": {"sandbox_deleted", "sandbox_stopped"}}

# A run's documents, under its step's records (``<records>/runs/<run>/``): its assignment, from
# dispatch, and – once it is no longer live – its outcome. Everything else a run has (its stage,
# latest progress, A2A bookkeeping, its counters while live) is the live run's, in memory; its
# log is the document of everything it did.
RUN_ASSIGNMENT = ("role", "soul", "cycle", "consult", "created_at", "sandbox", "sandbox_ref",
                  "base_sha", "branch", "judged_sha", "resumed_from", "resumed_session",
                  "prompt_pin", "assets_sha", "template_vars", "write_scope", "required_docs",
                  "context_refs", "assignment", "instructions")
RUN_OUTCOME = ("status", "failure", "exit_code", "ended_at", "concluded", "session_id", "report",
               "reply", "attempts", "violations", "docs_written", "doc_bytes", "confirmed_docs",
               "verification_requests", "turns")


class StateStore:
    """A change's records: ``change``, ``workflow`` (:class:`.FlowRuntime`'s tree and inputs,
    which order the nodes once it is rebuilt) and ``node:<id>`` – the fields the engine does not
    project from documents (:mod:`.projections`) nor learn from the gateway on start
    (:data:`GATEWAY`: which sandboxes exist and which are stopped). Runs are documents: their
    assignment and outcome (:data:`RUN_ASSIGNMENT`, :data:`RUN_OUTCOME`), written here as they
    change and projected back on load.

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
        self._run_docs: dict[str, dict[str, Any]] = {}   # run document -> as last written

    # ------------------------------------------------------------- persistence
    def _current(self) -> dict[str, dict[str, Any]]:
        out = {CHANGE: self.change.model_dump(mode="json", exclude=PROJECTED["change"])}
        if self.workflow is not None:
            out[WORKFLOW] = self.workflow
        out |= {f"node:{k}": n.model_dump(mode="json", exclude=PROJECTED["node"] | GATEWAY["node"])
                for k, n in self.nodes.items()}
        return out

    def run_dir(self, run: Run) -> str:
        """Where a run's documents are: under its step's records (the resolver's: its own)."""
        node = self.nodes.get(run.node_id)
        return f"{node.records if node else f'control/steps/{run.node_id}'}/runs/{run.id}"

    def _runs_now(self) -> dict[str, dict[str, Any]]:
        out = {}
        for r in self.runs.values():
            d, base = r.model_dump(mode="json"), self.run_dir(r)
            out[f"{base}/assignment.json"] = {k: d[k] for k in RUN_ASSIGNMENT}
            if r.status not in LIVE_RUN:
                out[f"{base}/outcome.json"] = {k: d[k] for k in RUN_OUTCOME}
        return out

    def _write_runs(self) -> None:
        """Each run document that changed since it was last written: a new version."""
        now = self._runs_now()
        for rel, data in now.items():
            if self._run_docs.get(rel) != data:
                path = f"changes/{self.cid}/{rel}"
                self.docs.write(path, json.dumps(data, indent=2, sort_keys=True),
                                author="controller", role="controller",
                                base_version=self.docs.latest_version(path))
                self._run_docs[rel] = data

    def runs_loaded(self) -> None:
        """The runs were projected from their documents (on load): nothing to write."""
        self._run_docs = self._runs_now()

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
        """Persist what changed since the last save: its rows, its journal line, and the run
        documents that changed."""
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
        if entries or gone:
            self.tables.write(self.cid, {r: now[r] for r, _ in entries}, gone)
            self.journal.append(self.cid, entries + [(r, {REMOVED: True}) for r in gone])
            self._last = now
        self._write_runs()

    def load(self) -> None:
        """The change as its tables hold it (on controller start)."""
        records = self.tables.load(self.cid)
        self.change = Change.model_validate(records[CHANGE])
        self.workflow = records.get(WORKFLOW)
        for record, data in records.items():
            kind, _, key = record.partition(":")
            if kind in _KINDS:
                self.nodes[key] = Node.model_validate(data)
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
