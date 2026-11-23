"""An append-only journal of records, per change: the history of the controller's state, as
text (the live state is the state tables', :mod:`.state_tables`; a restart never reads this).

One JSON Lines file per change, ``<root>/<change>.jsonl``, only ever opened for appending. Each
line is one save – all or nothing:

    {"seq": 12, "at": "2026-11-22T23:40:01.123Z", "entries": [["run:r0004", {"status": "REPORTED"}], ...]}

A record's state is the fold of its entries:

* an entry's data is a JSON object of top-level fields: each one replaces the field's value;
* a key ``+<field>`` extends a list field with the items given (a list that only grew);
* a key ``~<field>`` changes an object field in place: its value is the same kind of entry,
  one level down, with ``-`` listing the keys that went (so the workflow core's snapshot – one
  field – journals only the steps whose state changed);
* ``{"$removed": true}`` removes the record.

A record's first entry carries all its fields, so folding a journal from its start gives every
record as it stands (:meth:`Journal.fold` – for reading the history, not for a restart). A line
cut short by a crash is skipped (the next append starts a new line).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from .docstore import now_iso

log = logging.getLogger(__name__)

REMOVED = "$removed"
SUFFIX = ".jsonl"

Entry = tuple[str, dict[str, Any]]


class Journal:
    """The journal files under ``root`` (written through: flushed and synced per append)."""

    def __init__(self, root: Path, *, sync: bool = True):
        self.root, self.sync = Path(root), sync
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}        # change -> its last line's seq (once read)
        self._whole: set[str] = set()         # changes whose file ends with a whole line

    def path(self, change: str) -> Path:
        return self.root / f"{change}{SUFFIX}"

    def append(self, change: str, entries: Iterable[Entry]) -> int:
        """Append one line of ``entries`` for ``change``; returns its seq (the last one's,
        when there were no entries)."""
        entries = [[r, d] for r, d in entries]
        with self._lock:
            seq = self._last_seq(change)
            if not entries:
                return seq
            seq += 1
            line = json.dumps({"seq": seq, "at": now_iso(), "entries": entries},
                              separators=(",", ":"))
            p = self.path(change)
            torn = change not in self._whole and p.exists() and p.stat().st_size \
                and not _ends_with_newline(p)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(("\n" if torn else "") + line + "\n")   # after a torn line: a new one
                fh.flush()
                if self.sync:
                    os.fsync(fh.fileno())
            self._seq[change] = seq
            self._whole.add(change)
            return seq

    def _last_seq(self, change: str) -> int:
        if change not in self._seq:
            self._seq[change] = max((seq for seq, _ in self.lines(change)), default=0)
        return self._seq[change]

    def changes(self) -> list[str]:
        """Every change with a journal."""
        return sorted(p.name[:-len(SUFFIX)] for p in self.root.glob("*" + SUFFIX))

    def lines(self, change: str) -> Iterator[tuple[int, list[Entry]]]:
        """``(seq, entries)`` of each whole line of ``change``'s journal, in order."""
        p = self.path(change)
        if not p.exists():
            return
        with open(p, encoding="utf-8") as fh:
            for n, raw in enumerate(fh, 1):
                try:
                    line = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("%s line %d is cut short: skipped", p, n)
                    continue
                yield line["seq"], [(r, d) for r, d in line["entries"]]

    def fold(self, change: str) -> dict[str, dict[str, Any]]:
        """The records of ``change`` as its history leaves them (record -> its fields)."""
        records: dict[str, dict[str, Any]] = {}
        for _, entries in self.lines(change):
            for record, data in entries:
                apply(records, record, data)
        return records


def _ends_with_newline(p: Path) -> bool:
    with open(p, "rb") as fh:
        fh.seek(-1, os.SEEK_END)
        return fh.read(1) == b"\n"


def apply(records: dict[str, dict[str, Any]], record: str, data: dict[str, Any]) -> None:
    """Fold one entry into ``records``."""
    if data.get(REMOVED):
        records.pop(record, None)
        return
    records[record] = _apply(records.get(record, {}), data)


def _apply(cur: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    out = dict(cur)
    for k in data.get(GONE, ()):
        out.pop(k, None)
    for k, v in data.items():
        if k == GONE:
            continue
        if k.startswith("+"):
            out[k[1:]] = [*out.get(k[1:], []), *v]
        elif k.startswith("~"):
            out[k[1:]] = _apply(out.get(k[1:]) or {}, v)
        else:
            out[k] = v
    return out


def diff(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """The entry that takes a record from ``old`` to ``new`` (all of it if it is new): the
    fields that changed; a list that only grew, as its new items (``+field``); an object that
    changed, as what changed in it (``~field``)."""
    if old is None:
        return dict(new)
    return _diff(old, new)


def _diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in new.items():
        was = old.get(k, _MISSING)
        if was == v:
            continue
        if (isinstance(v, list) and isinstance(was, list) and len(v) > len(was)
                and v[:len(was)] == was):
            out["+" + k] = v[len(was):]
        elif isinstance(v, dict) and isinstance(was, dict) and was:
            out["~" + k] = _diff(was, v)
        else:
            out[k] = v
    if gone := [k for k in old if k not in new]:
        out[GONE] = gone
    return out


GONE = "-"
_MISSING = object()
