"""StateStore: one change's state, persisted as versioned documents in the store."""
from __future__ import annotations

import contextlib
import copy
import json
from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from ...store import DocStore
from ..model import Change, Node, Run

CONTROL = "control/"


class StateStore:
    """Per-change state persisted as versioned documents in the store (no other durable state).

    changes/<cid>/control/change.json          change record
    changes/<cid>/control/nodes/<node>.json    a node's detail (one version per transition)

The change's structure – which nodes exist, their order and dependencies – is not stored here:
it is the workflow's tree (control/workflow.json, :class:`.FlowRuntime`), which orders the nodes
once it is rebuilt.
    changes/<cid>/control/runs/<run>.json      run record (no credentials exist to persist)
    """

    def __init__(self, docs: DocStore, cid: str):
        self.docs = docs
        self.cid = cid
        self.root = f"changes/{cid}/{CONTROL}"
        self.change: Change | None = None
        self.nodes: dict[str, Node] = {}
        self.runs: dict[str, Run] = {}
        self._written: dict[str, str] = {}
        self._known_nodes: set[str] = set()

    # ------------------------------------------------------------- persistence
    def _write(self, rel: str, obj: Any) -> None:
        path = self.root + rel
        if isinstance(obj, BaseModel):
            obj = obj.model_dump(mode="json")
        content = json.dumps(obj, indent=2, sort_keys=True)
        if self._written.get(path) == content:
            return
        self.docs.write(path, content, author="controller", role="controller",
                        base_version=self.docs.latest_version(path))
        self._written[path] = content

    def _read(self, path: str) -> Any:
        d = self.docs.read(path)
        self._written[path] = d.content
        return json.loads(d.content)

    def load(self) -> None:
        self.change = Change.model_validate(self._read(self.root + "change.json"))
        for m in self.docs.list(self.root + "nodes/"):
            data = self._read(m.path)
            self._known_nodes.add(data["id"])
            if not data.get("removed"):
                self.nodes[data["id"]] = Node.model_validate(data)
        for m in self.docs.list(self.root + "runs/"):
            run = Run.model_validate(self._read(m.path))
            self.runs[run.id] = run

    def save(self) -> None:
        if self.change is None:
            return
        self._write("change.json", self.change)
        for nid in self._known_nodes - set(self.nodes):
            self._write(f"nodes/{nid}.json", {"id": nid, "removed": True})
        self._known_nodes |= set(self.nodes)
        for n in self.nodes.values():
            self._write(f"nodes/{n.id}.json", n)
        for r in self.runs.values():
            self._write(f"runs/{r.id}.json", r)

    # ------------------------------------------------------------- transactions
    @contextlib.contextmanager
    def transaction(self) -> Iterator[None]:
        """Apply a transition all-or-nothing: if the block raises, the change, nodes and runs
        are restored to their state at entry and the exception propagates. Restoring keeps
        object identity, so coroutines holding a Node or Run keep seeing the live object.
        (Nothing is saved here; external effects such as git merges are not undone.)"""
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
