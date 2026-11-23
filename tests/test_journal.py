"""The state journal: a change's records as an append-only JSON Lines file, folded on load."""
from __future__ import annotations

import json

from engine_harness import TASKS, Flow, scenario

from legion_orch.store.journal import Journal, apply, diff
from legion_orch.workflow.engine import Controller
from legion_orch.workflow.engine.base.state import StateStore


def _reload(f: Flow) -> tuple[Controller, object]:
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    return ctl2, ctl2.changes[f.ce.cid]


def _dumps(ce) -> tuple:
    return (ce.change.model_dump(mode="json"),
            {k: n.model_dump(mode="json") for k, n in ce.nodes.items()},
            {k: r.model_dump(mode="json") for k, r in ce.runs.items()})


def test_entries_fold_to_the_record():
    old = {"a": 1, "log": ["x"], "tags": ["p", "q"]}
    new = {"a": 2, "log": ["x", "y", "z"], "tags": ["q"]}
    e = diff(old, new)
    assert e == {"a": 2, "+log": ["y", "z"], "tags": ["q"]}      # a list that only grew: its tail
    recs = {"r": dict(old)}
    apply(recs, "r", e)
    assert recs["r"] == new
    assert diff(new, new) == {}
    apply(recs, "r", {"$removed": True})
    assert recs == {}


@scenario()
async def test_a_restart_folds_the_journal_back_into_the_same_records(f: Flow):
    t2 = dict(TASKS[0], id="T2", title="part", files_modified=["src/part.txt"],
              verification=["test -f src/part.txt"])
    await f.to_build(tasks=[TASKS[0], t2])
    await f.implement("T1")
    await f.settle()
    before = _dumps(f.ce)
    _, ce2 = _reload(f)
    assert _dumps(ce2) == before
    assert list(ce2.nodes) == list(f.ce.nodes) and list(ce2.runs) == list(f.ce.runs)
    # the controller's records are no documents any more
    assert not f.ctl.docs.list(f"changes/{f.ce.cid}/control/runs/")
    assert not f.ctl.docs.latest_version(f"changes/{f.ce.cid}/control/change.json")


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
    entries = [e for x in lines for e in x["entries"] if e[0] == "run:r0001"]
    full = len(f.ce.runs["r0001"].model_dump())
    assert len(entries[0][1]) == full and len(entries) > 1
    assert all(len(d) < full for _, d in entries[1:])
    # one save, one line: a quiet save writes nothing
    n = len(lines)
    async with f.ctl.svc.lock:
        f.ce.save()
    assert len(path.read_text().splitlines()) == n


@scenario()
async def test_a_load_snapshots_and_a_later_load_folds_from_the_snapshot(f: Flow):
    await f.intake()
    _, ce2 = _reload(f)
    lines = [json.loads(x) for x in f.ctl.svc.journal.path(f.ce.cid).read_text().splitlines()]
    assert lines[-1]["entries"][0] == ["$snapshot", {}]           # every live record, in full
    assert {r for r, _ in lines[-1]["entries"][1:]} == {
        "change", "workflow", *(f"node:{k}" for k in f.ce.nodes), *(f"run:{k}" for k in f.ce.runs)}
    _, ce3 = _reload(f)
    assert _dumps(ce3) == _dumps(ce2)


@scenario()
async def test_snapshots_are_taken_every_so_many_entries(f: Flow):
    StateStore.SNAPSHOT_EVERY = 5
    try:
        await f.intake()
        await f.human("H-1", "accept")
    finally:
        StateStore.SNAPSHOT_EVERY = 1000
    lines = f.ctl.svc.journal.path(f.ce.cid).read_text().splitlines()
    assert sum(json.loads(x)["entries"][0][0] == "$snapshot" for x in lines) >= 2
    _, ce2 = _reload(f)
    assert _dumps(ce2) == _dumps(f.ce)


@scenario()
async def test_a_line_cut_short_by_a_crash_is_skipped_and_the_next_save_starts_a_new_one(f: Flow):
    await f.intake()
    path = f.ctl.svc.journal.path(f.ce.cid)
    before = _dumps(f.ce)
    with open(path, "a") as fh:
        fh.write('{"seq": 999, "entries": [["change", {"status": "BRO')     # the crash
    ctl2, ce2 = _reload(f)
    assert _dumps(ce2) == before                                   # the torn save never happened
    async with ctl2.svc.lock:
        ce2.change.goal = "after the crash"
        ce2.save()
    _, ce3 = _reload(f)
    assert ce3.change.goal == "after the crash"


@scenario()
async def test_a_change_saved_as_documents_is_loaded_once_then_journalled(f: Flow):
    """A store written before the journal: its records are documents."""
    await f.intake()
    await f.human("H-1", "accept")
    before = _dumps(f.ce)
    root, docs = f"changes/{f.ce.cid}/control/", f.ctl.docs

    def put(rel, data):
        docs.write(root + rel, json.dumps(data), author="controller", role="controller")
    change, nodes, runs = before
    put("change.json", change)
    put("workflow.json", f.ce.st.workflow)
    for k, n in nodes.items():
        put(f"nodes/{k}.json", n)
    put("nodes/gone.json", {"id": "gone", "removed": True})
    for k, r in runs.items():
        put(f"runs/{k}.json", r)
    f.ctl.svc.journal.path(f.ce.cid).unlink()

    fresh = Journal(f.cfg.state_dir / "journal")
    assert StateStore.change_ids(docs, fresh) == [f.ce.cid]       # found by its documents
    _, ce2 = _reload(f)
    assert _dumps(ce2) == before
    assert f.ctl.svc.journal.path(f.ce.cid).exists()              # journalled from now on
    _, ce3 = _reload(f)
    assert _dumps(ce3) == before
