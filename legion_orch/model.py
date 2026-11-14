"""Workflow state: change, nodes (task graph), runs and the append-only event log."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from typing import Any

from .docstore import now_iso

# Node kinds
INTAKE, PLAN, CRITIQUE, ORACLE, BASELINE, BUILD, INTEGRATION, RELEASE, SHIP, HUMAN = (
    "intake", "plan", "critique", "oracle", "baseline", "build", "integration", "release",
    "ship", "human")
# Human gate ids
H_REQUIREMENTS, H_SPEC, H_CHANGE = "H-1", "H-2", "H-3"

# Node statuses
PENDING, READY, RUNNING, VERIFYING, REVIEWING, MERGING, REWORK, ACCEPTED, ESCALATED, FAILED, \
    AWAITING_HUMAN = ("PENDING", "READY", "RUNNING", "VERIFYING", "REVIEWING", "MERGING",
                      "REWORK", "ACCEPTED", "ESCALATED", "FAILED", "AWAITING_HUMAN")
ACTIVE_NODE = {RUNNING, VERIFYING, REVIEWING, MERGING}

# Run statuses
R_STARTING, R_ACTIVE, R_REPORTED, R_ACCEPTED, R_SUPERSEDED, R_FAILED = (
    "STARTING", "ACTIVE", "REPORTED", "ACCEPTED", "SUPERSEDED", "FAILED")
LIVE_RUN = {R_STARTING, R_ACTIVE}

# Which agent role executes each node kind
ROLE_FOR_KIND = {INTAKE: "intake", PLAN: "planner", CRITIQUE: "critic", ORACLE: "oracle",
                 BASELINE: "baseline", BUILD: "implementer", INTEGRATION: "integration",
                 RELEASE: "release"}


@dataclass
class Node:
    id: str
    kind: str
    title: str
    depends_on: list[str] = field(default_factory=list)
    status: str = PENDING
    cycle: int = 1
    task: dict | None = None             # build contract from plan/taskgraph.json
    wave: int = 0
    runs: list[str] = field(default_factory=list)
    current_run: str | None = None
    sandbox: str | None = None           # implementer sandbox (reused across rework cycles)
    base_sha: str | None = None
    candidate_sha: str | None = None
    accepted_sha: str | None = None
    feedback: list[dict] = field(default_factory=list)   # [{cycle, source, text, attestations}]
    finding_history: list[list[str]] = field(default_factory=list)  # HIGH fingerprints / cycle
    reviews: dict[str, dict] = field(default_factory=dict)          # "cycle:role" -> result
    verification: dict[str, dict] = field(default_factory=dict)     # cycle -> result
    pending_reviews: list[str] = field(default_factory=list)        # run ids
    escalation: dict | None = None
    extra_cycles: int = 0                # granted by a human on escalation
    human: dict | None = None            # for human nodes: approval record
    updated_at: str = field(default_factory=now_iso)


@dataclass
class Run:
    id: str
    node_id: str
    role: str
    soul: str
    cycle: int
    token: str
    status: str = R_STARTING
    sandbox: str | None = None
    base_sha: str | None = None
    branch: str | None = None
    judged_sha: str | None = None        # candidate/head under judgement (judge roles)
    prompt_pin: str | None = None
    assets_sha: str | None = None     # sha256 of the asset set used to render the prompt
    write_scope: list[str] = field(default_factory=list)
    required_docs: list[str] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)
    assignment: dict = field(default_factory=dict)
    attempts: int = 0
    violations: list[list[str]] = field(default_factory=list)
    report: dict | None = None
    progress: list[dict] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    ended_at: str | None = None
    exit_code: int | None = None
    sandbox_deleted: bool = False


@dataclass
class Change:
    id: str
    goal: str
    created_at: str = field(default_factory=now_iso)
    status: str = "ACTIVE"           # ACTIVE | READY | NOT_READY | ABANDONED
    base_sha: str | None = None
    tier: str | None = None
    risk: dict | None = None
    tier_override: dict | None = None
    approvals: dict[str, dict] = field(default_factory=dict)
    oracle: dict | None = None        # parsed plan/oracle.json at approval
    oracle_sha: str | None = None
    integration_head: str | None = None
    panel: list[str] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    first_build_at: str | None = None
    attestations: dict[str, dict] = field(default_factory=dict)   # kind -> latest record
    overrides: list[dict] = field(default_factory=list)           # add_gate / waive_gate
    protected_approvals: list[dict] = field(default_factory=list) # approve protected-paths
    surface_pins: dict[str, dict] = field(default_factory=dict)   # surface id -> pins record
    ship: dict | None = None


def _from_dict(cls, d: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


CONTROL = "control/"


class StateStore:
    """Per-change state persisted as versioned documents in the store (no other durable state).

    changes/<cid>/control/change.json          change record
    changes/<cid>/control/graph.json           task graph structure (node order, kinds, deps, waves)
    changes/<cid>/control/nodes/<node>.json    node state (one version per transition)
    changes/<cid>/control/runs/<run>.json      run record (bearer token never persisted)
    changes/<cid>/control/events/<seq>.json    append-only event log
    """

    def __init__(self, docs, cid: str):
        self.docs = docs
        self.cid = cid
        self.root = f"changes/{cid}/{CONTROL}"
        self.change: Change | None = None
        self.nodes: dict[str, Node] = {}
        self.runs: dict[str, Run] = {}
        self._events: list[dict] = []
        self._written: dict[str, str] = {}
        self._known_nodes: set[str] = set()

    # ------------------------------------------------------------- persistence
    def _write(self, rel: str, obj: Any) -> None:
        path = self.root + rel
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
        self.change = _from_dict(Change, self._read(self.root + "change.json"))
        graph = self._read(self.root + "graph.json") if self.docs.latest_version(
            self.root + "graph.json") else {"order": []}
        for nid in graph["order"]:
            data = self._read(f"{self.root}nodes/{nid}.json")
            if not data.get("removed"):
                self.nodes[nid] = _from_dict(Node, data)
        self._known_nodes = set(graph["order"]) | {
            m["path"][len(self.root + "nodes/"):-5] for m in self.docs.list(self.root + "nodes/")}
        for m in self.docs.list(self.root + "runs/"):
            data = self._read(m["path"])
            data["token"] = ""          # tokens are memory-only; old runs can never act again
            run = _from_dict(Run, data)
            self.runs[run.id] = run
        for m in self.docs.list(self.root + "events/"):
            self._events.append(json.loads(self.docs.read(m["path"]).content))
        self._events.sort(key=lambda e: e["seq"])

    def save(self) -> None:
        if self.change is None:
            return
        self._write("change.json", asdict(self.change))
        self._write("graph.json", {"order": list(self.nodes), "nodes": {
            n.id: {"kind": n.kind, "title": n.title, "depends_on": n.depends_on, "wave": n.wave,
                   "task": (n.task or {}).get("id")} for n in self.nodes.values()}})
        for nid in self._known_nodes - set(self.nodes):
            self._write(f"nodes/{nid}.json", {"id": nid, "removed": True})
        self._known_nodes |= set(self.nodes)
        for n in self.nodes.values():
            self._write(f"nodes/{n.id}.json", asdict(n))
        for r in self.runs.values():
            d = asdict(r)
            d.pop("token")
            self._write(f"runs/{r.id}.json", d)

    def event(self, kind: str, message: str, node: str | None = None, run: str | None = None,
              data: Any = None) -> int:
        seq = len(self._events) + 1
        rec = {"seq": seq, "ts": now_iso(), "kind": kind, "node": node, "run": run,
               "message": message, "data": data}
        self.docs.write(f"{self.root}events/{seq:06d}.json", json.dumps(rec, sort_keys=True),
                        author="controller", role="controller", base_version=None)
        self._events.append(rec)
        return seq

    def events(self, after: int = 0, limit: int = 500) -> list[dict]:
        return [e for e in self._events if e["seq"] > after][-limit:]


def _parse_ts_safe(ts: str):
    from datetime import datetime, timezone
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
