"""The design-review workflow (REQ-5): its skeleton, and a change under it end to end as far as
the selection goes: no plan or build before ``H-S`` passes, requirements for selected findings
only. Driven in-process by tests/engine_harness.py."""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from engine_harness import REPO, ROOT, Flow, signals

from legion_orch.workflow.assets import Assets

GOAL = "Review the design of the app"
ORDER = ["change", "workspace", "review", "inspect", "findings-review", "select", "H-S",
         "requirements", "intake", "H-1", "plan", "design", "critique", "H-E1", "provision",
         "scaffold", "verify:scaffold", "review:scaffold", "can-merge:scaffold", "delivery",
         "oracle", "preflight", "H-2", "baseline", "H-2b", "builds", "verify:delivery",
         "integration", "evaluate", "describe", "H-3", "release", "publish"]


@pytest.fixture(scope="module")
def specs():
    a = Assets(ROOT / "assets").workflows
    return a["design-review"], a["default"]


def _by_id(spec):
    return {t.id: t for t in spec.skeleton.walk()}


def test_flattened_order(specs):
    spec, _ = specs
    assert [t.id for t in spec.skeleton.walk()] == ORDER


def test_review_group_types_and_signs(specs):
    t = _by_id(specs[0])
    assert (t["inspect"].type, t["findings-review"].type) == (
        "agent:design-reviewer", "agent:findings-reviewer")
    assert (t["select"].type, t["H-S"].type) == ("agent:selector", "human:signoff")
    assert t["H-S"].signs == ["selection"]
    assert [c.id for c in t["inspect"].criteria] == ["findings-review"]
    assert [c.id for c in t["select"].criteria] == ["H-S"]
    assert t["review"].type == t["requirements"].type            # a grouping, like requirements


def test_inputs(specs):
    t = _by_id(specs[0])
    assert t["intake"].inputs == ["selection"]
    assert t["design"].inputs == ["requirements", "risk", "selection"]
    assert t["H-1"].signs == ["requirements", "risk"] and t["H-1"].applies == "tier"


def test_selection_signoff_is_not_waivable(specs):
    spec, _ = specs
    assert spec.criteria["human:signoff"].waivable is False
    assert "agent:findings-reviewer" not in spec.criteria        # a judge: waivable by default


def test_policies_are_the_defaults(specs):
    spec, default = specs
    assert spec.tiers == default.tiers
    assert spec.hooks == default.hooks
    assert spec.stale == default.stale
    assert spec.criteria == default.criteria


def test_rest_of_skeleton_is_the_defaults(specs):
    spec, default = specs
    new = {"review", "inspect", "findings-review", "select", "H-S"}
    a, b = _by_id(spec), _by_id(default)
    assert set(a) - set(b) == new and set(b) <= set(a)
    for k in set(b) - {"intake", "design", "change", "requirements", "plan"}:
        assert a[k] == b[k], k


# ---------------------------------------------------------------- the scenario
async def _start(tmp_path) -> Flow:
    f = Flow(tmp_path, max_report_attempts=20)
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change(GOAL, REPO, workflow="design-review")
    await f.settle()
    return f


def _head(f: Flow) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=f.tmp / "src-repo", check=True,
                          capture_output=True, text=True).stdout.strip()


def _no_plan_work(f: Flow) -> None:
    assert f.status("design") == "pending", f.status()
    assert not [t for t in f.ce.st.tickets.values() if t.type == "agent:implementer"]
    assert not [r for r in f.ce.runs.values() if r.ticket in ("design", "builds")]


async def _selection(f: Flow, ev: str) -> None:
    r = f.run("select")
    sel = await f.write(r, "selection.json", {
        "selected": [{"id": "F-1", "reason": "real coupling", "scope": "greet", "evidence": [ev]}],
        "dropped": [{"id": "F-2", "reason": "cosmetic"}]})
    out = await f.report(r, [sel], outputs={"selection": sel})
    assert out.accepted, out.violations


@pytest.mark.scenario
def test_selection_gates_the_plan_and_builds_serve_selected_findings(tmp_path):
    async def go():
        f = await _start(tmp_path)
        try:
            assert f.ce.change.workflow == "design-review"
            ev = f"git:{REPO}/src/greet.txt@{_head(f)}"
            # no plan work before the selection is signed
            _no_plan_work(f)
            assert f.status("inspect") == "working" and f.status("intake") == "pending"

            FINDING = {"evidence": [ev], "problem": "p", "direction": "d", "effort": "small"}
            r = f.run("inspect")
            fnd = await f.write(r, "findings.json", {"findings": [
                dict(FINDING, id="F-1", title="coupled", severity="high"),
                dict(FINDING, id="F-2", title="naming", severity="low")]})
            out = await f.report(r, [fnd, ev], outputs={"findings": fnd})
            assert out.accepted, out.violations

            j = f.run("findings-review")
            rev = await f.write(j, "review.json", {"reviews": [
                {"finding": "F-1", "verdict": "valid", "severity": "high", "note": "confirmed"},
                {"finding": "F-2", "verdict": "valid", "severity": "low", "note": "confirmed"}]})
            out = await f.report(j, [fnd, rev], {"verdict": "PASS", "findings": [],
                                                 "checked": ["evidence"]},
                                 outputs={"findings-review": rev})
            assert out.accepted, out.violations
            assert f.status("select") == "working"

            await _selection(f, ev)
            assert f.status("H-S") == "working" and "H-S" in f.ce.signoffs_waiting()
            _no_plan_work(f)

            await f.signoff("H-S", approve=False, reason="also keep F-2 out of scope notes")
            assert (f.ticket("select").status, f.ticket("select").attempt) == ("working", 2)
            assert any("F-2 out of scope" in x for x in f.run("select").assignment["findings"])
            _no_plan_work(f)
            assert f.status("intake") == "pending"

            await _selection(f, ev)
            await f.signoff("H-S")
            assert f.ce.tickets.root.pointers["selection"] == f.ticket("select").outputs["selection"]
            _no_plan_work(f)                              # intake is next, not the plan
            assert f.status("intake") == "working"

            r = f.run("intake")
            risk = await f.write(r, "risk.json", signals())
            for reqs, cite in (("- REQ-1: fix the naming F-2\n", "F-2"),          # unselected
                               ("- REQ-1: fix it\n", "no id"),                    # none cited
                               ("- REQ-1: decouple F-1\n- REQ-2: rename F-2\n", "F-2")):
                req = await f.write(r, "requirements.md", f"# Requirements\n\n{reqs}")
                out = await f.report(r, [req, risk], outputs={"requirements": req, "risk": risk},
                                     exit_agent=False)
                assert not out.accepted and out.violations[0].startswith("G-DR4"), out.violations
                assert cite in " ".join(out.violations) or cite == "no id"
            req = await f.write(r, "requirements.md", "# Requirements\n\n- REQ-1: decouple F-1\n")
            out = await f.report(r, [req, risk], outputs={"requirements": req, "risk": risk})
            assert out.accepted, out.violations
            _no_plan_work(f)                              # H-1 not yet signed
            assert "H-1" in f.ce.signoffs_waiting()

            await f.signoff("H-1")
            assert f.status("design") == "working"        # only now

            await f.plan()
            assert not [t for t in f.ce.st.tickets.values() if t.type == "agent:implementer"]
            await f.critique("PASS")
            await f.signoff("H-E1")                       # the signed plan adds its builds
            builds = [t for t in f.ce.st.tickets.values() if t.type == "agent:implementer"]
            assert [b.id for b in builds] == ["T1"]
            reqs = f.ce.doc("tickets/intake/requirements.md").content
            sel = f.ce.json_doc(f.ticket("select").outputs["selection"].split("@")[0][5:])
            selected = {s["id"] for s in sel["selected"]}
            assert selected == {"F-1"}
            for b in builds:
                for q in b.work["requirements"]:
                    block = reqs.split(f"- {q}:")[1].split("\n- REQ-")[0]
                    assert {"F-1"} == {x for x in ("F-1", "F-2") if x in block}
        finally:
            await f.stop()
    asyncio.run(go())


def test_selector_reads_the_judges_report_when_no_ticket_pins_it():
    from types import SimpleNamespace as NS

    from legion_orch.workflow.roles.design import _review

    def run(role, status, outputs):
        return NS(role=role, report=NS(status=status, outputs=outputs))
    ref = "file:d/review.json@2"
    e = NS(tickets=NS(t={}), runs={
        "r1": run("findings-reviewer", "DONE", {"findings-review": "file:d/review.json@1"}),
        "r2": run("findings-reviewer", "DONE", {"findings-review": ref}),
        "r3": run("selector", "DONE", {"selection": "file:d/s.json@1"}),
        "r4": run("findings-reviewer", "BLOCKED", {})})
    assert _review(e) == ref
    assert _review(NS(tickets=NS(t={}), runs={})) is None
