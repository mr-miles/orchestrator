"""Workflow state: change, nodes (task graph), runs and the append-only event log."""
from __future__ import annotations

import json
import sqlite3
import threading
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
    workflow_sha: str | None = None
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
    waivers: dict[str, dict] = field(default_factory=dict)
    ship: dict | None = None


def _from_dict(cls, d: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


class StateStore:
    """Persists the whole state as a JSON snapshot + an append-only event log."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn, self.lock = conn, lock
        with lock:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, kind TEXT NOT NULL,
              node TEXT, run TEXT, message TEXT NOT NULL, data TEXT);
            CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
            BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
            BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
            """)
            conn.commit()
        self.change: Change | None = None
        self.nodes: dict[str, Node] = {}
        self.runs: dict[str, Run] = {}
        self.load()

    def load(self) -> None:
        with self.lock:
            row = self.conn.execute("SELECT v FROM state WHERE k='snapshot'").fetchone()
        if not row:
            return
        snap = json.loads(row[0])
        self.change = _from_dict(Change, snap["change"]) if snap.get("change") else None
        self.nodes = {k: _from_dict(Node, v) for k, v in snap["nodes"].items()}
        self.runs = {k: _from_dict(Run, v) for k, v in snap["runs"].items()}

    def save(self) -> None:
        snap = {"change": asdict(self.change) if self.change else None,
                "nodes": {k: asdict(v) for k, v in self.nodes.items()},
                "runs": {k: asdict(v) for k, v in self.runs.items()}}
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO state VALUES ('snapshot', ?)",
                              (json.dumps(snap),))
            self.conn.commit()

    def event(self, kind: str, message: str, node: str | None = None, run: str | None = None,
              data: Any = None) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO events (ts, kind, node, run, message, data) VALUES (?,?,?,?,?,?)",
                (now_iso(), kind, node, run, message, json.dumps(data) if data is not None else None))
            self.conn.commit()
            return cur.lastrowid

    def events(self, after: int = 0, limit: int = 500) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT seq, ts, kind, node, run, message, data FROM events WHERE seq > ? "
                "ORDER BY seq DESC LIMIT ?", (after, limit)).fetchall()
        return [{"seq": r[0], "ts": r[1], "kind": r[2], "node": r[3], "run": r[4],
                 "message": r[5], "data": json.loads(r[6]) if r[6] else None}
                for r in reversed(rows)]

    def run_by_token(self, token: str) -> Run | None:
        for r in self.runs.values():
            if r.token == token:
                return r
        return None


def _parse_ts_safe(ts: str):
    from datetime import datetime, timezone
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
