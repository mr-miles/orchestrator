"""The controller's live state, per change, as tables in the store's database.

What a restart loads – current values only, each row updated in place as the change saves (its
history is the journal's, :mod:`.journal`, never read on start):

* ``changes`` – one row per change: its record (``record``) and the workflow's
  (``workflow``: the stage tree's parameters and the core's inputs);
* ``steps``   – one row per node of the change (``record``).

(Agent runs are documents – their assignment and outcome – not rows.)

Records are JSON objects of the fields the engine does not project from documents. A save
writes the rows that changed in one transaction.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
  change TEXT PRIMARY KEY,
  record TEXT NOT NULL,
  workflow TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  change TEXT NOT NULL,
  node TEXT NOT NULL,
  record TEXT NOT NULL,
  PRIMARY KEY (change, node)
);
"""

CHANGE, WORKFLOW = "change", "workflow"
_TABLES = {"node": ("steps", "node")}                          # record prefix -> (table, key)


def _dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"))


class StateTables:
    """The live state tables, through the document store's connection and lock. Records are
    keyed as the state store names them: ``change``, ``workflow``, ``node:<id>``."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self.conn, self.lock = conn, lock
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def changes(self) -> list[str]:
        """Every change, oldest first."""
        with self.lock:
            return [r[0] for r in self.conn.execute(
                "SELECT change FROM changes ORDER BY rowid").fetchall()]

    def write(self, change: str, put: dict[str, dict[str, Any]], drop: list[str]) -> None:
        """Set the records in ``put`` and remove those in ``drop`` (one transaction)."""
        with self.lock, self.conn:
            for record, data in put.items():
                if record == CHANGE:
                    self.conn.execute(
                        "INSERT INTO changes (change, record) VALUES (?, ?) "
                        "ON CONFLICT(change) DO UPDATE SET record=excluded.record",
                        (change, _dumps(data)))
                elif record == WORKFLOW:
                    self.conn.execute("UPDATE changes SET workflow=? WHERE change=?",
                                      (_dumps(data), change))
                else:
                    table, key = self._where(record)
                    self.conn.execute(
                        f"INSERT INTO {table} (change, {key}, record) VALUES (?, ?, ?) "
                        f"ON CONFLICT(change, {key}) DO UPDATE SET record=excluded.record",
                        (change, record.partition(":")[2], _dumps(data)))
            for record in drop:
                table, key = self._where(record)
                self.conn.execute(f"DELETE FROM {table} WHERE change=? AND {key}=?",
                                  (change, record.partition(":")[2]))

    @staticmethod
    def _where(record: str) -> tuple[str, str]:
        kind = record.partition(":")[0]
        if kind not in _TABLES:
            raise ValueError(f"not a state record: {record!r}")
        return _TABLES[kind]

    def load(self, change: str) -> dict[str, dict[str, Any]]:
        """Every record of ``change`` (record -> its fields); nodes in the order they were first
        written."""
        out: dict[str, dict[str, Any]] = {}
        with self.lock:
            row = self.conn.execute("SELECT record, workflow FROM changes WHERE change=?",
                                    (change,)).fetchone()
            if row is None:
                return out
            out[CHANGE] = json.loads(row[0])
            if row[1] is not None:
                out[WORKFLOW] = json.loads(row[1])
            for prefix, (table, key) in _TABLES.items():
                for k, data in self.conn.execute(
                        f"SELECT {key}, record FROM {table} WHERE change=? ORDER BY rowid",
                        (change,)).fetchall():
                    out[f"{prefix}:{k}"] = json.loads(data)
        return out
