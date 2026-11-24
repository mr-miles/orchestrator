"""A change's metrics (legion_orch/workflow/metrics.py): computed by the controller from the
journal, the runs and their digests, and written before the retrospective, which reads them."""
from __future__ import annotations

import json

from engine_harness import Flow, scenario

from legion_orch.workflow.metrics import compute


def ts(minute: int) -> str:
    return f"2026-11-24T10:{minute:02d}:00.000Z"


def tk(tid, status, attempt=1, *, since, parent="c", type_="agent:implementer", role="subtask",
       waiting=None, criteria=()):
    return (f"ticket:{tid}", {"id": tid, "parent": parent, "type": type_, "role": role,
                             "status": status, "attempt": attempt, "waiting": waiting,
                             "since": since, "criteria": list(criteria)})


def step(tid, since, **fields):
    return (f"ticket:{tid}", {"since": since, **fields})


HISTORY = [
    [("ticket:c", {"id": "c", "parent": None, "type": "grouping", "role": "root",
                   "status": "subtasks", "attempt": 0, "since": ts(0)}),
     tk("T1", "working", since=ts(0), criteria=["review:T1"]),
     tk("review:T1", "pending", 0, since=ts(0), parent="T1", type_="agent:reviewer",
        role="criterion"),
     tk("T2", "working", since=ts(0), criteria=["H-1"]),
     tk("H-1", "pending", 0, since=ts(0), parent="T2", type_="human:signoff", role="criterion")],
    # T1's review rejects it once; then T1 passes on its second attempt
    [step("T1", ts(1), status="checking"), step("review:T1", ts(1), status="working", attempt=1)],
    [step("review:T1", ts(2), status="rejected"), step("T1", ts(2), status="working", attempt=2)],
    [step("T1", ts(3), status="checking"), step("review:T1", ts(3), status="working", attempt=2)],
    [step("review:T1", ts(4), status="passed"), step("T1", ts(4), status="passed")],
    # T2: its sign-off waits 10 minutes for a decision owner and passes first time
    [step("T2", ts(1), status="checking"), step("H-1", ts(1), status="working", attempt=1)],
    [step("H-1", ts(11), status="passed"), step("T2", ts(11), status="passed")],
    # T3 is blocked: it waits on a human 5 minutes, is retried and passes
    [tk("T3", "working", since=ts(12))],
    [step("T3", ts(13), status="waiting", waiting="blocked")],
    [step("T3", ts(18), status="working", waiting=None)],
    [step("T3", ts(19), status="passed")],
]
RUNS = [
    {"id": "r1", "ticket": "T1", "role": "implementer", "status": "ACCEPTED",
     "created_at": ts(0), "ended_at": ts(1), "violations": [["G-1: bad"]]},
    {"id": "r2", "ticket": "review:T1", "role": "reviewer", "status": "ACCEPTED",
     "created_at": ts(1), "ended_at": ts(2), "violations": []},
    {"id": "r3", "ticket": "T1", "role": "implementer", "status": "FAILED",
     "created_at": ts(2), "ended_at": ts(3), "violations": []},
]
DIGESTS = {
    "r1": {"results": [{"total_cost_usd": 0.5, "usage": {"input_tokens": 100, "output_tokens": 20,
                                                          "cache_read_input_tokens": 1000}}]},
    "r2": {"results": [{"total_cost_usd": 0.25, "usage": {"input_tokens": 50}}]},
}


def test_the_metrics_of_a_change():
    m = compute({"id": "c1", "tier": "R1", "status": "READY", "created_at": ts(0)},
                HISTORY, RUNS, DIGESTS, ts(20))
    assert m["change"]["wall_seconds"] == 1200.0
    assert m["work"] == {"count": 3, "first_pass": 1, "reworked": 1, "rework_rounds": 1,
                         "first_pass_rate": 0.333}         # T2 only: T1 reworked, T3 waited
    rows = {r["id"]: r for r in m["tickets_with_work"]}
    assert rows["T1"]["rejections"] == {"agent:reviewer": 1} and not rows["T1"]["first_pass"]
    assert rows["T2"]["first_pass"]
    assert m["rework"] == {"agent:reviewer": {"rejections": 1, "tickets": {"T1": 1}}}
    assert m["waiting"] == {"blocked": {"count": 1, "seconds": 300.0, "tickets": ["T3"]}}
    assert m["signoffs"] == {"decided": 1, "rejected": 0, "decision_wait_seconds": 600.0,
                             "longest_wait": {"ticket": "H-1", "seconds": 600.0}}
    impl = m["roles"]["implementer"]
    assert (impl["runs"], impl["statuses"], impl["rejected_reports"], impl["wall_seconds"]) == (
        2, {"ACCEPTED": 1, "FAILED": 1}, 1, 120.0)
    assert impl["cost_usd"] == 0.5 and impl["tokens"]["cache_read"] == 1000
    assert rows["T1"]["cost_usd"] == 0.75                  # its own runs and its criteria's
    assert m["totals"]["cost_usd"] == 0.75 and m["totals"]["runs"] == 3
    assert m["tickets"]["by_status"] == {"subtasks": 1, "passed": 5}


def test_without_runs_or_history():
    m = compute({"id": "c1"}, [], [], {}, ts(0))
    assert m["work"]["first_pass_rate"] is None and m["totals"]["cost_usd"] == 0.0


@scenario()
async def test_the_retrospective_is_given_the_metrics(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL", title="no newline")          # one rework, by the review
    await f.to_end_from_build()
    await f.change_action("retrospective")
    await f.settle()
    files = f.runner.payloads[f.run("retrospective").sandbox]
    m = json.loads(files["retro/metrics.json"])
    assert m == json.loads(f.ce.doc("control/metrics.json").content)
    assert m["rework"]["agent:reviewer"]["tickets"] == {"T1": 1}
    t1 = next(r for r in m["tickets_with_work"] if r["id"] == "T1")
    assert (t1["attempts"], t1["first_pass"]) == (2, False)
    assert m["signoffs"]["decided"] >= 3 and m["signoffs"]["rejected"] == 0   # H-1, H-E1, H-3
    assert m["roles"]["implementer"]["runs"] == 2
    assert "retro/docs/control/metrics.json" not in files      # once, at the top
