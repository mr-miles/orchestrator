"""A change's tickets, persisted: the live state – one row per ticket in ``state.db``'s ``tickets``
table, what a restart loads – and the history, in the change's journal (one line per save: each
ticket's changed fields), as for every other record (:mod:`legion_orch.store.state_tables`,
:mod:`legion_orch.store.journal`). The tickets are the state: there is nothing else to replay."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ...store.journal import Journal, diff
from ...store.state_tables import StateTables
from .model import Ticket

PREFIX = "ticket"


class TicketRecords:
    def __init__(self, tables: StateTables, journal: Journal, change: str):
        self.tables, self.journal, self.change = tables, journal, change
        self._last: dict[str, dict[str, Any]] = {}

    def load(self) -> list[Ticket]:
        rows = self.tables.rows(self.change, PREFIX)
        self._last = {f"{PREFIX}:{k}": v for k, v in rows.items()}
        return [Ticket.model_validate(v) for v in rows.values()]

    def save(self, tickets: Iterable[Ticket]) -> None:
        """The tickets that changed since the last save: their rows (one transaction), then one
        journal line. Tickets are never removed (failed and cancelled ones are history)."""
        now = {f"{PREFIX}:{x.id}": x.model_dump(mode="json") for x in tickets}
        entries = [(r, e) for r, data in now.items() if (e := diff(self._last.get(r), data))]
        if entries:
            self.tables.write(self.change, {r: now[r] for r, _ in entries}, [])
            self.journal.append(self.change, entries)
            self._last |= {r: now[r] for r, _ in entries}
