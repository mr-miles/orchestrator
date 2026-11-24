"""A change's metrics (``control/metrics.json``), computed by the controller when the change ends
– numbers a retrospective (or a person) can trust, rather than count by reading logs.

Pure: from the change's journal (every save's changed fields: each ticket's status, attempt and
reason for waiting, stamped with ``since``), its run records and their digests (cost and tokens,
from the agent CLI's result events). What it says:

* ``work`` – the tickets with work of their own (not criteria, not grouping): how many passed
  on their first attempt with no criterion rejecting them and no wait on a human (first pass),
  how many were reworked and how many rounds that took;
* ``rework`` – every time a criterion rejected its owner's work, by criterion type (a review, a
  verification, a sign-off …) and by ticket: where the rework came from;
* ``waiting`` – every time a ticket waited on a human, by reason, and for how long;
* ``signoffs`` – the decision owners: sign-offs decided, rejected, and how long they waited for
  a decision;
* ``roles`` – per role: runs, how they ended, the models that served them, reports rejected,
  wall time, cost and tokens;
* ``tickets`` – the same per ticket with work, for finding the outliers.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ..store.journal import REMOVED, apply

GROUPING, SIGNOFF = "grouping", "human:signoff"
#: what a criterion's round goes to when it rejects its owner's work: ``rejected`` while the owner
#: waits, else straight back to ``pending`` – the owner's next attempt starts in the same save
REJECTED = ("rejected", "pending")
_TOKENS = {"input": "input_tokens", "output": "output_tokens",
           "cache_read": "cache_read_input_tokens", "cache_write": "cache_creation_input_tokens"}


def _at(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(a: str | None, b: str | None) -> float | None:
    x, y = _at(a), _at(b)
    return round((y - x).total_seconds(), 1) if x and y else None


class _Episode:
    """One stay of a ticket in a status: from its ``since`` to the next change."""
    __slots__ = ("status", "attempt", "waiting", "since")

    def __init__(self, status: str, attempt: int, waiting: str | None, since: str):
        self.status, self.attempt, self.waiting, self.since = status, attempt, waiting, since


def _episodes(history: Iterable[list[tuple[str, dict[str, Any]]]]
              ) -> tuple[dict[str, dict[str, Any]], dict[str, list[_Episode]]]:
    """The tickets as they stand at the end, and each ticket's stays in a status, in order –
    from the journal's lines (each a save's entries)."""
    records: dict[str, dict[str, Any]] = {}
    stays: dict[str, list[_Episode]] = defaultdict(list)
    for entries in history:
        for record, data in entries:
            if not record.startswith("ticket:"):
                continue
            if data.get(REMOVED):
                records.pop(record, None)
                continue
            apply(records, record, data)
            x = records[record]
            tid = record.partition(":")[2]
            now = (x.get("status"), int(x.get("attempt") or 0), x.get("waiting"))
            last = stays[tid][-1] if stays[tid] else None
            if last is None or (last.status, last.attempt, last.waiting) != now:
                stays[tid].append(_Episode(*now, x.get("since") or ""))
    return {k.partition(":")[2]: v for k, v in records.items()}, stays


def _cost(digest: Mapping[str, Any] | None) -> tuple[float, dict[str, int]]:
    """A run's cost and tokens: the sum over its log's result events."""
    usd, tokens = 0.0, dict.fromkeys(_TOKENS, 0)
    for r in (digest or {}).get("results") or []:
        usd += float(r.get("total_cost_usd") or 0)
        usage = r.get("usage") or {}
        for k, field in _TOKENS.items():
            tokens[k] += int(usage.get(field) or 0)
    return usd, tokens


def _add(into: dict[str, Any], usd: float, tokens: Mapping[str, int]) -> None:
    into["cost_usd"] = round(into.get("cost_usd", 0.0) + usd, 4)
    t = into.setdefault("tokens", dict.fromkeys(_TOKENS, 0))
    for k, v in tokens.items():
        t[k] += v


def compute(change: Mapping[str, Any], history: Iterable[list[tuple[str, dict[str, Any]]]],
            runs: Iterable[Mapping[str, Any]], digests: Mapping[str, Mapping[str, Any]],
            now: str) -> dict[str, Any]:
    """The change's metrics. ``change``: its record; ``history``: its journal's lines' entries,
    in order; ``runs``: its run records; ``digests``: run id -> digest; ``now``: when (the end of
    anything still open)."""
    tickets, stays = _episodes(history)
    runs = list(runs)

    def own_work(x: Mapping[str, Any]) -> bool:
        return x.get("role") != "criterion" and x.get("type", GROUPING) != GROUPING

    def ends(tid: str, i: int) -> str:
        nxt = stays[tid][i + 1].since if i + 1 < len(stays[tid]) else now
        return nxt or now

    def rejections(ss: list[_Episode]) -> list[int]:
        """The rounds (stay indexes) of a criterion that ended in a rejection."""
        return [i for i, s in enumerate(ss[:-1])
                if s.status == "working" and ss[i + 1].status in REJECTED]

    # rework: each time a criterion rejected its owner's work
    rejected_by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    causes: dict[str, dict[str, Any]] = {}
    for tid, ss in stays.items():
        x = tickets.get(tid) or {}
        if x.get("role") != "criterion":
            continue
        n = len(rejections(ss))
        if not n:
            continue
        ctype, owner = x.get("type", ""), x.get("parent") or ""
        rejected_by[owner][ctype] += n
        c = causes.setdefault(ctype, {"rejections": 0, "tickets": {}})
        c["rejections"] += n
        c["tickets"][owner] = c["tickets"].get(owner, 0) + n

    # waiting on a human, by reason
    waiting: dict[str, dict[str, Any]] = {}
    for tid, ss in stays.items():
        for i, s in enumerate(ss):
            if s.status != "waiting":
                continue
            w = waiting.setdefault(s.waiting or "unknown", {"count": 0, "seconds": 0.0,
                                                            "tickets": []})
            w["count"] += 1
            w["seconds"] = round(w["seconds"] + (_seconds(s.since, ends(tid, i)) or 0), 1)
            if tid not in w["tickets"]:
                w["tickets"].append(tid)

    # sign-offs: the decision owners' waits (a sign-off works while it waits for a decision)
    decided = rejected = 0
    wait_s, longest = 0.0, {"ticket": None, "seconds": 0.0}
    for tid, ss in stays.items():
        if (tickets.get(tid) or {}).get("type") != SIGNOFF:
            continue
        no = rejections(ss)
        for i, s in enumerate(ss):
            if s.status != "working":
                continue
            secs = _seconds(s.since, ends(tid, i)) or 0
            wait_s += secs
            if secs > longest["seconds"]:
                longest = {"ticket": tid, "seconds": secs}
            nxt = ss[i + 1].status if i + 1 < len(ss) else None
            if nxt == "passed" or i in no:
                decided += 1
                rejected += i in no

    # roles and tickets: runs, cost, tokens
    roles: dict[str, dict[str, Any]] = {}
    per_run: dict[str, tuple[float, dict[str, int]]] = {}
    for r in runs:
        usd, tokens = per_run[r["id"]] = _cost(digests.get(r["id"]))
        g = roles.setdefault(r.get("role", "?"), {"runs": 0, "statuses": {}, "rejected_reports": 0,
                                                  "wall_seconds": 0.0})
        g["runs"] += 1
        st = str(r.get("status", "?"))
        g["statuses"][st] = g["statuses"].get(st, 0) + 1
        g["rejected_reports"] += len(r.get("violations") or [])
        served = r.get("model_served") or "unknown"
        g.setdefault("models", {})[served] = g.get("models", {}).get(served, 0) + 1
        g["wall_seconds"] = round(g["wall_seconds"] + (
            _seconds(r.get("created_at"), r.get("ended_at") or now) or 0), 1)
        _add(g, usd, tokens)

    work, per_ticket = {"count": 0, "first_pass": 0, "reworked": 0, "rework_rounds": 0}, []
    for tid, x in tickets.items():
        if not own_work(x):
            continue
        attempts = int(x.get("attempt") or 0)
        rejections = dict(rejected_by.get(tid, {}))
        waited = any(s.status == "waiting" for s in stays.get(tid, []))
        first = x.get("status") == "passed" and attempts <= 1 and not rejections and not waited
        work["count"] += 1
        work["first_pass"] += first
        work["reworked"] += attempts > 1
        work["rework_rounds"] += max(attempts - 1, 0)
        row: dict[str, Any] = {"id": tid, "type": x.get("type"), "status": x.get("status"),
                               "attempts": attempts, "first_pass": first,
                               "rejections": rejections,
                               "runs": sum(1 for r in runs if r.get("ticket") == tid)}
        for r in runs:
            if r.get("ticket") == tid or r.get("ticket") in (x.get("criteria") or []):
                _add(row, *per_run[r["id"]])
        row.setdefault("cost_usd", 0.0)
        per_ticket.append(row)
    work["first_pass_rate"] = round(work["first_pass"] / work["count"], 3) if work["count"] \
        else None

    totals: dict[str, Any] = {"runs": len(runs)}
    for usd, tokens in per_run.values():
        _add(totals, usd, tokens)
    totals.setdefault("cost_usd", 0.0)
    totals.setdefault("tokens", dict.fromkeys(_TOKENS, 0))
    statuses: dict[str, int] = {}
    for x in tickets.values():
        statuses[x.get("status", "?")] = statuses.get(x.get("status", "?"), 0) + 1
    ended = (change.get("ended") or {}).get("at") if isinstance(change.get("ended"), dict) \
        else None
    return {
        "change": {"id": change.get("id"), "tier": change.get("tier"),
                   "status": change.get("status"), "created_at": change.get("created_at"),
                   "computed_at": now,
                   "wall_seconds": _seconds(change.get("created_at"), ended or now)},
        "tickets": {"total": len(tickets), "by_status": statuses},
        "work": work,
        "rework": dict(sorted(causes.items(), key=lambda kv: -kv[1]["rejections"])),
        "waiting": waiting,
        "signoffs": {"decided": decided, "rejected": rejected,
                     "decision_wait_seconds": round(wait_s, 1), "longest_wait": longest},
        "roles": dict(sorted(roles.items())),
        "totals": totals,
        "tickets_with_work": per_ticket,
    }
