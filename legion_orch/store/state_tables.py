"""The controller's live state, per change, as tables in the store's database.

What a restart loads – current values only, each row updated in place as the change saves (its
history is the journal's, :mod:`.journal`, never read on start):

* ``changes`` – one row per change: its record (``record``);
* ``tickets`` – one row per ticket of the change (``record``: its fields, state included);
* ``hooks``   – one row per side process's ticket, outside the tree (the retrospective).

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
  record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
  change TEXT NOT NULL,
  ticket TEXT NOT NULL,
  record TEXT NOT NULL,
  PRIMARY KEY (change, ticket)
);
CREATE TABLE IF NOT EXISTS hooks (
  change TEXT NOT NULL,
  ticket TEXT NOT NULL,
  record TEXT NOT NULL,
  PRIMARY KEY (change, ticket)
);
"""

CHANGE = "change"
_TABLES = {"ticket": ("tickets", "ticket"),                   # record prefix -> (table, key)
           "hook": ("hooks", "ticket")}


def _dumps(data: Any) -> str:
    return json.dumps(data, separators=(",", ":"))


class StateTables:
    """The live state tables, through the document store's connection and lock. Records are
    keyed as the state store names them: ``change``, ``ticket:<id>``, ``hook:<id>``."""

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

    def rows(self, change: str, prefix: str) -> dict[str, dict[str, Any]]:
        """The change's records of one kind (``ticket``), key -> fields, in the order they were
        first written."""
        table, key = _TABLES[prefix]
        with self.lock:
            return {k: json.loads(d) for k, d in self.conn.execute(
                f"SELECT {key}, record FROM {table} WHERE change=? ORDER BY rowid",
                (change,)).fetchall()}

    def load(self, change: str) -> dict[str, dict[str, Any]]:
        """Every record of ``change`` (record -> its fields); tickets in the order they were
        first written."""
        out: dict[str, dict[str, Any]] = {}
        with self.lock:
            row = self.conn.execute("SELECT record FROM changes WHERE change=?",
                                    (change,)).fetchone()
            if row is None:
                return out
            out[CHANGE] = json.loads(row[0])
            for prefix, (table, key) in _TABLES.items():
                for k, data in self.conn.execute(
                        f"SELECT {key}, record FROM {table} WHERE change=? ORDER BY rowid",
                        (change,)).fetchall():
                    out[f"{prefix}:{k}"] = json.loads(data)
        return out
