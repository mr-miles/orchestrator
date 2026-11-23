"""Records copied from documents are projections of them: set as the document is written
(DocWritten on the bus) and on load from the documents, never persisted by the state store."""
from __future__ import annotations

import json

from engine_harness import Flow, scenario

from legion_orch.store import now_iso
from legion_orch.workflow.engine import Controller


def _journalled(f: Flow, field: str) -> bool:
    path = f.ctl.svc.journal.path(f.ce.cid)
    return any(field in d for line in path.read_text().splitlines()
               for r, d in json.loads(line)["entries"] if r == "change")


@scenario()
async def test_approvals_are_projected_from_their_documents_and_not_journalled(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    h1 = f.ce.change.approvals["H-1"]
    assert h1.attestation == f.ce.latest_att("control/approvals/requirements.json")
    assert not _journalled(f, "approvals")                  # the document is its only copy
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    ce2 = ctl2.changes[f.ce.cid]
    assert ce2.change.approvals == f.ce.change.approvals    # rebuilt from the documents


@scenario()
async def test_a_write_updates_the_projection_before_it_returns_and_a_revocation_removes_it(f: Flow):
    await f.intake()
    async with f.ctl.svc.lock:
        f.ce.write_record("control/approvals/environment.json",
                          {"gate": "H-E1", "approver": "ops", "at": now_iso()})
        assert f.ce.change.approvals["H-E1"].approver == "ops"      # at once
        f.ce.write_record("control/approvals/environment.json",
                          {"gate": "H-E1", "approver": "controller", "at": now_iso(),
                           "revoked": True})
        assert "H-E1" not in f.ce.change.approvals
        f.ce.save()
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    assert "H-E1" not in ctl2.changes[f.ce.cid].change.approvals    # the revocation holds


@scenario()
async def test_a_written_document_is_announced_with_its_schema(f: Flow):
    from legion_orch.bus import DocWritten
    seen: list[DocWritten] = []
    f.ctl.bus.subscribe(DocWritten, seen.append, cid=f.ce.cid)
    async with f.ctl.svc.lock:
        f.ce.write_record("control/approvals/environment.json",
                          {"gate": "H-E1", "approver": "ops", "at": now_iso()})
        f.ce.write_record("control/approvals/protected-paths/1.json",
                          {"task": "T1", "paths": [".github/x"], "approver": "ops",
                           "reason": "r", "at": now_iso()})
        f.ce.write_record("control/approvals/environment.json",      # unchanged: no version
                          {"gate": "H-E1", "approver": "ops",
                           "at": f.ce.json_doc("control/approvals/environment.json")["at"]})
    assert seen == [
        DocWritten(f.ce.cid, "control/approvals/environment.json", 1, "approval"),
        DocWritten(f.ce.cid, "control/approvals/protected-paths/1.json", 1, "protected-approval")]


@scenario()
async def test_every_projected_field_comes_back_from_the_documents_alone(f: Flow):
    from legion_orch.workflow.engine.base.projections import PROJECTED
    await f.to_build()
    ch = f.ce.change
    assert ch.risk and ch.requirements and ch.approvals     # set by now (R1: no oracle step)
    for field in PROJECTED["change"]:
        assert not _journalled(f, field), field
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    assert ctl2.changes[f.ce.cid].change.model_dump() == ch.model_dump()


@scenario()
async def test_a_steps_verification_and_panel_come_back_from_their_records(f: Flow):
    await f.to_build()
    await f.implement()
    for role in f.ce.change.panel:
        await f.review(role=role)
    await f.settle()
    node = f.ce.nodes["build:T1"]
    assert node.verification and node.panel
    path = f.ctl.svc.journal.path(f.ce.cid)
    entries = [d for line in path.read_text().splitlines()
               for r, d in json.loads(line)["entries"] if r == "node:build:T1"]
    assert not any("verification" in d or "panel" in d for d in entries)
    assert all(not v.results or all(r.output == "" for r in v.results)
               for v in node.verification.values())            # the node's copy is compact
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    again = ctl2.changes[f.ce.cid].nodes["build:T1"]
    assert again.verification == node.verification and again.panel == node.panel
