"""The state journal: a change's records as an append-only JSON Lines file, folded on load."""
from __future__ import annotations

import json

from engine_harness import TASKS, Flow, scenario

from legion_orch.store.journal import Journal, apply, diff
from legion_orch.workflow.engine import Controller


async def _reload(f: Flow) -> tuple[Controller, object]:
    """A restart: the tables, then the gateway's sandboxes (as Controller.start)."""
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    await ctl2.adopt_sandboxes()
    return ctl2, ctl2.changes[f.ce.cid]


def _dumps(ce) -> tuple:
    """What a restart keeps: the change, the steps, and each run as its documents say (its
    stage, latest progress and live bookkeeping are the live run's only)."""
    from legion_orch.workflow.engine.base.state import RUN_ASSIGNMENT, RUN_OUTCOME
    kept = {"id", "node_id", *RUN_ASSIGNMENT, *RUN_OUTCOME}
    return (ce.change.model_dump(mode="json"),
            {k: n.model_dump(mode="json") for k, n in ce.nodes.items()},
            {k: r.model_dump(mode="json", include=kept) for k, r in ce.runs.items()})


def test_entries_fold_to_the_record():
    old = {"a": 1, "log": ["x"], "tags": ["p", "q"]}
    new = {"a": 2, "log": ["x", "y", "z"], "tags": ["q"]}
    e = diff(old, new)
    assert e == {"a": 2, "+log": ["y", "z"], "tags": ["q"]}      # a list that only grew: its tail
    recs = {"r": dict(old)}
    apply(recs, "r", e)
    assert recs["r"] == new
    apply(recs, "r", {"$removed": True})
    assert recs == {}
    # an object changes in place: only what changed in it, and the keys that went
    core = {"states": {"plan": {"status": "running", "attempt": 1}, "intake": {"status": "done"},
                       "old": {"status": "done"}}, "ended": None}
    later = {"states": {"plan": {"status": "done", "attempt": 1}, "intake": {"status": "done"},
                        "H-1": {"status": "running"}}, "ended": None}
    e2 = diff(core, later)
    assert e2 == {"~states": {"~plan": {"status": "done"}, "H-1": {"status": "running"},
                              "-": ["old"]}}
    recs = {"w": core}
    apply(recs, "w", e2)
    assert recs["w"] == later
    assert diff(new, new) == {}



@scenario()
async def test_a_restart_loads_the_same_records_from_the_tables_alone(f: Flow):
    t2 = dict(TASKS[0], id="T2", title="part", files_modified=["src/part.txt"],
              verification=["test -f src/part.txt"])
    await f.to_build(tasks=[TASKS[0], t2])
    await f.implement("T1")
    await f.settle()
    before = _dumps(f.ce)
    f.ctl.svc.journal.path(f.ce.cid).unlink()          # the history is not read on start
    _, ce2 = await _reload(f)
    assert _dumps(ce2) == before
    assert list(ce2.nodes) == list(f.ce.nodes) and list(ce2.runs) == list(f.ce.runs)
    # the controller's records are no documents
    assert not f.ctl.docs.list(f"changes/{f.ce.cid}/control/runs/")
    assert not f.ctl.docs.latest_version(f"changes/{f.ce.cid}/control/change.json")


@scenario()
async def test_the_tables_hold_one_row_per_record_updated_in_place(f: Flow):
    await f.to_build()
    await f.implement()
    conn, cid = f.ctl.svc.tables.conn, f.ce.cid
    count = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t} WHERE change=?", (cid,)).fetchone()[0]  # noqa: E731
    assert count("changes") == 1
    assert count("steps") == len(f.ce.nodes)        # runs are documents, not rows
    # the journal's history folds to the same records
    folded = f.ctl.svc.journal.fold(cid)
    assert folded == f.ctl.svc.tables.load(cid)


@scenario()
async def test_the_journal_is_only_appended_to_and_a_save_appends_what_changed(f: Flow):
    path = f.ctl.svc.journal.path(f.ce.cid)
    await f.intake()
    seen = path.read_bytes()
    await f.human("H-1", "accept")
    await f.plan()
    now = path.read_bytes()
    assert now.startswith(seen) and len(now) > len(seen)          # appended, nothing rewritten
    lines = [json.loads(x) for x in now.decode().splitlines()]
    assert [x["seq"] for x in lines] == list(range(1, len(lines) + 1))
    # a record is in full once; every later entry of it carries only what changed
    entries = [e for x in lines for e in x["entries"] if e[0] == "node:intake"]
    full = len(entries[0][1])
    assert len(entries) > 1 and all(len(d) < full for _, d in entries[1:])
    # runs are documents: an assignment (sandbox recorded) and an outcome per conclusion
    run = f.ce.runs["r0001"]
    base = f"{f.ce.st.run_dir(run)}"
    assert base == "control/steps/intake/runs/r0001"
    assert f.ce.latest(f"{base}/assignment.json") <= 3 and f.ce.latest(f"{base}/outcome.json") <= 4
    assert not any(e[0].startswith("run:") for x in lines for e in x["entries"])
    # the workflow core is one field: after its first entry, only the steps that changed
    wf = [d for x in lines for r, d in x["entries"] if r == "workflow"]
    assert len(wf) > 2 and all("core" not in d for d in wf[1:])
    # one save, one line: a quiet save writes nothing
    n = len(lines)
    async with f.ctl.svc.lock:
        f.ce.save()
    assert len(path.read_text().splitlines()) == n


@scenario()
async def test_a_line_cut_short_by_a_crash_is_skipped_and_the_next_save_starts_a_new_one(f: Flow):
    await f.intake()
    j, cid = f.ctl.svc.journal, f.ce.cid
    before = j.fold(cid)
    with open(j.path(cid), "a") as fh:
        fh.write('{"seq": 999, "entries": [["change", {"status": "BRO')     # the crash
    fresh = Journal(f.cfg.state_dir / "journal")
    assert fresh.fold(cid) == before                     # the torn save is not history
    ctl2, ce2 = await _reload(f)                               # the controller after the crash
    async with ctl2.svc.lock:
        ce2.change.goal = "after the crash"
        ce2.save()
    assert Journal(f.cfg.state_dir / "journal").fold(cid)["change"]["goal"] == "after the crash"
