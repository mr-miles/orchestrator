"""Records copied from documents are projections of them: set as the document is written
(DocWritten on the bus) and on load from the documents, never persisted by the state store.
What is left of them with tickets: the environment build and the runs (audit documents)."""
from __future__ import annotations

import json

from engine_harness import Flow, scenario

from legion_orch.store import now_iso


def _journalled(f: Flow, field: str) -> bool:
    path = f.ctl.svc.journal.path(f.ce.cid)
    return any(field in d for line in path.read_text().splitlines()
               for r, d in json.loads(line)["entries"] if r == "change")


BUILD = {"spec": "file:x@1", "roles": {}, "log": None}


@scenario()
async def test_the_environment_build_is_projected_and_a_revocation_removes_it(f: Flow):
    async with f.ctl.svc.lock:
        f.ce.write_record("control/environment/build.json", BUILD)
        assert f.ce.change.environment.spec == "file:x@1"            # at once
        f.ce.save()
    assert not _journalled(f, "environment")                         # the document is its copy
    ctl2, _ = await f.restart()
    assert ctl2.changes[f.ce.cid].change.environment.spec == "file:x@1"
    async with f.ctl.svc.lock:
        f.ce.write_record("control/environment/build.json", {**BUILD, "revoked": True})
        assert f.ce.change.environment is None
        f.ce.save()
    ctl3, _ = await f.restart()
    assert ctl3.changes[f.ce.cid].change.environment is None         # the revocation holds


@scenario()
async def test_a_written_document_is_announced_with_its_schema(f: Flow):
    from legion_orch.bus import DocWritten
    seen: list[DocWritten] = []
    f.ctl.bus.subscribe(DocWritten, seen.append, cid=f.ce.cid)
    async with f.ctl.svc.lock:
        f.ce.write_record("control/environment/build.json", BUILD)
        f.ce.write_record("control/environment/build.json", BUILD)   # unchanged: no version
    assert seen == [DocWritten(f.ce.cid, "control/environment/build.json", 1, "environment-build")]


@scenario()
async def test_a_run_is_its_assignment_and_its_outcome(f: Flow):
    await f.to_build()
    await f.implement()
    run = next(r for r in f.ce.runs.values() if r.role == "implementer")
    base = f"control/runs/{run.id}"
    assignment = f.ce.json_doc(f"{base}/assignment.json")
    outcome = f.ce.json_doc(f"{base}/outcome.json")
    assert assignment["prompt_pin"] == run.prompt_pin and assignment["template_vars"] == run.template_vars
    assert assignment["ticket"] == "T1" and assignment["attempt"] == 1      # it names its ticket
    assert "status" not in assignment and "progress" not in assignment and "stage" not in assignment
    assert outcome["status"] == run.status and outcome["report"]["summary"] == run.report.summary
    assert outcome["report"]["outputs"]["evidence"].startswith("file:")
    ctl2, _ = await f.restart()
    again = ctl2.changes[f.ce.cid].runs[run.id]
    assert (again.ticket, again.status, again.report, again.template_vars) == \
        (run.ticket, run.status, run.report, run.template_vars)


@scenario()
async def test_a_run_live_at_a_restart_has_no_outcome_until_recovery_ends_it(f: Flow):
    await f.intake()
    await f.signoff("H-1")
    live = f.run("design")                                  # the planner is working
    base = f"control/runs/{live.id}"
    assert f.ce.latest(f"{base}/assignment.json") and not f.ce.latest(f"{base}/outcome.json")
    ctl2, ce2 = await f.restart()
    assert ce2.runs[live.id].status == "STARTING"          # projected from its assignment alone
    await ctl2.adopt_sandboxes()
    async with ctl2.svc.lock:
        ce2.recover()
    assert ce2.runs[live.id].status == "FAILED"
    assert ce2.json_doc(f"{base}/outcome.json")["status"] == "FAILED"
    assert ctl2.next_run_id() > live.id                     # run ids carry on
