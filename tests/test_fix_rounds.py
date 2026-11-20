"""Fix rounds (WORKFLOW.md §5.10a): a late integration failure sends Delivery back, and its policy
appends a fix round – a Round stage whose nodes are suffixed ``@n``: plan (fix mode), critique,
H-E1, the oracle again only if it changed, preflight, baseline, H-2, the new tasks – then
integration runs again. (The harness resolves ``plan``, ``H-2`` … to the current round's node.)"""
from __future__ import annotations

import pytest
from engine_harness import ORACLE, REPO, TASKS, Flow, scenario

from legion_orch.server import views
from legion_orch.workflow import governance
from legion_orch.workflow.engine import Controller, WorkflowError
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    ESCALATED,
    PENDING,
    REWORK,
    RUNNING,
    ApprovalState,
    ChangeStatus,
    Resume,
)

# the round's fix: a new task that may modify the accepted T1's file and depend on it
T2 = dict(TASKS[0], id="T2", title="greet.txt ends with a newline", depends_on=["T1"],
          instructions="Fixes the failing acceptance test: greet.txt lacks its newline.",
          verification=["test -f src/greet.txt"])


async def _fail_integration(f: Flow) -> str:
    """Integration reports PASS but the controller's oracle run fails at the head (G-V1)."""
    cmd = f.ce.oracle_commands()[0]
    f.runner.commands[cmd] = (1, "152 failed\n")
    await f.integration("PASS")
    node = f.ce.nodes["integration"]
    assert node.status == ESCALATED and node.escalation.kind == "integration_failed"
    return cmd


async def _to_round(f: Flow, **kw) -> str:
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    cmd = await _fail_integration(f)
    await f.human("integration", "plan_fix", **kw)
    return cmd


@scenario()
async def test_a_fix_round_adds_tasks_on_top_of_the_integration_head(f: Flow):
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    cmd = await _fail_integration(f)
    head, integ = f.ce.change.integration_head, f.ce.nodes["integration"]
    await f.human("integration", "plan_fix", comment="greet.txt needs a trailing newline")
    ch = f.ce.change
    rnd = ch.rounds[0]
    assert ch.round == 2 and (rnd.number, rnd.head, rnd.approver) == (2, head, "miles")
    assert integ.verification["integration"].attestation in rnd.failure
    assert f.ce.latest("control/rounds/2.json") == 1
    assert integ.status == PENDING and integ.escalation is None
    assert "plan@2" in f.ce.nodes and f.ce.nodes["plan@2"].round == 2
    assert f.status("H-2") == PENDING and f.ce.nodes["H-2"].status == ACCEPTED   # round 1's stands
    assert f.status("build:T1") == ACCEPTED
    # a fresh planner, told about the round
    plan = f.run("plan")
    note = plan.template_vars["fix_round"]
    assert "## Fix round 2" in note and "greet.txt needs a trailing newline" in note
    assert "T1 (round 1)" in note and head[:12] in note
    assert rnd.attestation in plan.context_refs and set(rnd.failure) <= set(plan.context_refs)
    assert plan.template_vars["feedback"] == "(none)"          # earlier rounds' feedback is history
    out = await f.plan(tasks=TASKS + [T2])
    assert out.accepted, out
    critic = f.run("plan", "critic")
    assert "## Fix round 2" in critic.template_vars["fix_round"]
    await f.critique("PASS")
    t2 = f.ce.nodes["build:T2"]
    assert t2.round == 2 and t2.status == PENDING and f.ce.nodes["build:T1"].round == 1
    assert f.status("H-E1") == AWAITING_HUMAN                 # the round's plan is approved
    h2 = {s.subject: s.status for s in governance.h2_status(f.ce)}
    assert h2["plan"] == ApprovalState.STALE and h2["oracle"] == ApprovalState.APPROVED
    await f.human("H-E1", "approve")
    assert f.status("oracle") == ACCEPTED                     # unchanged: it stands
    assert f.status("H-2") == AWAITING_HUMAN
    await f.human("H-2", "approve")
    impl = f.run("build:T2", "implementer")
    assert impl.base_sha == head and rnd.first_build_at
    del f.runner.commands[cmd]
    await f.implement("T2")
    await f.review("T2")
    assert t2.status == ACCEPTED and integ.status == RUNNING
    assert f.run("integration").judged_sha == ch.integration_head != head
    assert rnd.attestation in f.run("integration").context_refs
    await f.integration("PASS")
    assert integ.status == ACCEPTED
    await f.human("H-3", "accept")
    assert ch.status == ChangeStatus.READY, ch.ship
    contract = ch.ship.gates["task_contract"]
    assert contract.status == "satisfied" and "2 task contract(s)" in contract.detail, contract


@scenario(fix_rounds=1, max_report_attempts=20)
async def test_the_round_must_append_tasks_and_the_round_budget_is_kept(f: Flow):
    await _to_round(f)
    r = f.run("plan")
    for tasks, expect in (
            ([dict(TASKS[0], title="renamed")], ["G-FR2: task T1 was changed", "adds no task"]),
            ([T2 | {"depends_on": []}], ["G-FR2: task T1 was removed"]),
            (TASKS + [T2 | {"requirements": ["REQ-9"]}], ["G-FR1: task T2 cites unknown requirement"]),
            (TASKS + [T2, T2 | {"id": "T3", "depends_on": []}], ["G-P6: parallel tasks T2 and T3"])):
        out = await f.plan(tasks=tasks)
        assert not out.accepted, tasks
        for e in expect:
            assert any(e in v for v in out.violations), (e, out.violations)
    assert f.run("plan").id == r.id and (await f.plan(tasks=TASKS + [T2])).accepted
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.human("H-2", "approve")
    await f.implement("T2")
    await f.review("T2")
    await f.integration("PASS")                                # the oracle command still fails
    with pytest.raises(WorkflowError, match=r"limits.fix_rounds \(1\) reached"):
        await f.human("integration", "plan_fix")


@scenario()
async def test_plan_fix_needs_an_integration_failure(f: Flow):
    await f.to_build(tier="R2")
    node = f.ce.nodes["build:T1"]
    f.ce.escalate(node, "blocked", "needs a decision", resume=Resume.REWORK)
    with pytest.raises(WorkflowError, match="only available when integration failed"):
        await f.human("build:T1", "plan_fix")


@scenario()
async def test_revise_sends_the_round_back_and_restart_restarts_the_plan(f: Flow):
    await _to_round(f)
    await f.plan(tasks=TASKS + [T2])
    await f.critique("PASS")
    assert "build:T2" in f.ce.nodes
    await f.human("H-E1", "revise", feedback="T2 must also fix the docs")
    plan = f.ce.nodes["plan@2"]                        # the round's design starts over
    assert plan.status == RUNNING and plan.cycle == 2 and f.ce.change.round == 2
    assert f.status("build:T2") == PENDING and f.status("build:T1") == ACCEPTED
    assert "T2 must also fix the docs" in f.run("plan").template_vars["feedback"]
    await f.plan(tasks=TASKS + [T2])
    await f.critique("PASS")
    assert f.status("H-E1") == AWAITING_HUMAN
    assert "send_back:stage:plan" in f.ce.workflow.actions("H-E1@2")
    await f.human("H-E1", "send_back", stage="plan", feedback="start again from the requirements")
    ch = f.ce.change
    assert ch.round == 1 and ch.rounds == [] and "plan@2" not in f.ce.nodes
    assert f.status("plan") == RUNNING and f.ce.nodes["plan"].round == 1
    assert [b.id for b in f.ce.build_nodes()] == ["build:T1"] and f.status("build:T1") == PENDING
    assert ch.integration_head == ch.base_sha and ch.oracle_sha is None


@scenario()
async def test_a_revised_oracle_runs_again_from_the_integration_head(f: Flow):
    await _to_round(f)
    head = f.ce.change.integration_head
    oracle = dict(ORACLE, criteria=ORACLE["criteria"] + [
        {"id": "AC-2", "requirement": "REQ-1", "text": "greet.txt ends with a newline"}])
    assert (await f.plan(tasks=TASKS + [T2], oracle=oracle)).accepted
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    node = f.ce.nodes["oracle@2"]                      # changed: the round's oracle runs
    assert node.status == RUNNING
    run = f.run("oracle")
    assert run.base_sha == head and run.branch.endswith("/oracle-2")
    await f.oracle({"tests/acceptance/test_greet.sh":
                    "grep -q hello src/greet.txt\ngrep -c '' src/greet.txt\n"})
    assert node.status == ACCEPTED and f.ce.change.oracle_sha == node.accepted_sha
    assert await f.ctl.git.is_ancestor(head, f.ce.change.integration_head)
    assert f.status("H-2") == AWAITING_HUMAN
    h2 = {s.subject: s.status for s in governance.h2_status(f.ce)}
    assert h2["oracle"] == ApprovalState.STALE
    await f.human("H-2", "approve")
    assert f.ce.change.oracle.criteria[-1].id == "AC-2"


async def _baseline(f: Flow) -> None:
    r = f.run("baseline")
    doc = await f.write(r, "baseline/report.md", f"results before the build (cycle {r.cycle})")
    out = await f.report(r, [f"git:{REPO}@{r.judged_sha}", doc], {"results": [
        {"command": c, "exit_code": 1} for c in f.ce.baseline_commands()]})
    assert out.accepted, out


async def _attest_baseline(f: Flow) -> None:
    await f.change_action("attest", kind="independent-baseline", verdict="pass",
                          sources=[f.ce.latest_att("baseline/report.md")])


@scenario()
async def test_r3_reruns_the_baseline_and_needs_a_new_attestation(f: Flow):
    await f.to_h2(tier="R3")
    cmd = f.ce.oracle_commands()[0]
    f.runner.commands[cmd] = (1, "FAIL\n")
    await _baseline(f)
    await _attest_baseline(f)
    await f.human("H-2", "approve")
    await f.implement()
    for role in f.ce.change.panel:
        await f.review(role=role)
    await f.integration("PASS")
    assert f.ce.nodes["integration"].escalation.kind == "integration_failed"
    await f.human("integration", "plan_fix")
    await f.plan(tasks=TASKS + [T2])
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    base = f.ce.nodes["baseline@2"]                    # the round's baseline, at its head
    assert base.status == RUNNING
    await _baseline(f)
    assert base.status == ACCEPTED and f.status("H-2") == AWAITING_HUMAN
    row = next(s for s in views.h2_rows(f.ce) if s["subject"] == "independent-baseline")
    assert row["status"] == "stale"                          # round 1's attestation is too old
    res = await f.human("H-2", "approve")                    # approve all attests it again
    assert res.pending == [] and f.status("H-2") == ACCEPTED
    assert f.ce.change.attestations["independent-baseline"].at > base.verification["baseline"].at


@scenario()
async def test_a_change_in_a_fix_round_is_rebuilt_from_the_store(f: Flow):
    await _to_round(f, comment="why")
    await f.plan(tasks=TASKS + [T2])
    await f.critique("PASS")
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    ce2 = ctl2.changes[f.ce.cid]
    assert ce2.change.round == 2 and ce2.change.rounds[0].comment == "why"
    assert ce2.nodes["build:T2"].round == 2 and ce2.nodes["plan@2"].round == 2
    assert ce2.nodes["H-E1@2"].status == AWAITING_HUMAN and REWORK != ce2.nodes["plan@2"].status
    # the workflow is rebuilt from its record: the same state, the same actions
    assert ce2.workflow.to_json() == f.ce.workflow.to_json()
    assert ce2.workflow.actions("H-E1@2") == f.ce.workflow.actions("H-E1@2")


@scenario(environment={})
async def test_the_environment_stands_unless_the_round_changes_it(f: Flow):
    await _to_round(f)
    assert (await f.plan(tasks=TASKS + [T2], keep_environment=True)).accepted
    await f.critique("PASS")
    build = f.ce.change.environment
    await f.human("H-E1", "approve")
    assert f.status("provision") == ACCEPTED and f.ce.change.environment == build
    assert f.status("H-2") == AWAITING_HUMAN


@scenario(environment={})
async def test_a_round_that_changes_the_environment_is_approved_and_provisioned_again(f: Flow):
    from engine_harness import ENVIRONMENT
    await _to_round(f)
    changed = dict(ENVIRONMENT, rationale=ENVIRONMENT["rationale"] + "; the fix needs more memory")
    assert (await f.plan(tasks=TASKS + [T2], environment=changed)).accepted
    await f.critique("PASS")
    assert f.status("H-E1") == AWAITING_HUMAN and f.status("H-2") == PENDING
    before = f.builder.built[:]
    await f.human("H-E1", "approve")
    assert f.status("provision") == ACCEPTED and f.status("H-2") == AWAITING_HUMAN
    assert f.ce.change.environment.spec != f.ce.nodes["provision"].id    # rebuilt from the round's
    assert f.builder.recipes and len(f.builder.built) >= len(before)


# ====================================================================== R3: baseline at H-2
@scenario()
async def test_r3_h2_lists_the_baseline_attestation_and_approves_it_in_one_step(f: Flow):
    await f.to_h2(tier="R3")
    await _baseline(f)
    [row] = [s for s in views.h2_rows(f.ce) if s["kind"] == "attestation"]
    ver = f.ce.nodes["baseline"].verification["baseline"]
    assert row["subject"] == "independent-baseline" and row["status"] == "pending"
    assert row["sources"] == [f.ce.latest_att("baseline/report.md"), ver.attestation]
    assert [r["command"] for r in row["results"]] == [r.command for r in ver.results]
    res = await f.human("H-2", "approve", subjects=["oracle"])   # the baseline is still to decide
    assert "independent-baseline" in res.pending and f.status("H-2") == AWAITING_HUMAN
    res = await f.human("H-2", "approve", subjects=["independent-baseline"])
    assert res.approved == ["independent-baseline"] and res.pending == []
    rec = f.ce.change.attestations["independent-baseline"]
    assert rec.verdict == "pass" and rec.sources == row["sources"]
    assert f.status("H-2") == ACCEPTED           # the oracle approved, its precondition met


@scenario()
async def test_r3_baseline_row_before_the_baseline_finishes(f: Flow):
    await f.to_h2(tier="R3")
    [row] = [s for s in views.h2_rows(f.ce) if s["kind"] == "attestation"]
    assert row["status"] == "pending" and row["note"] == "the baseline has not finished yet"
    assert row["results"] == [] and f.status("H-2") != AWAITING_HUMAN


# ====================================================================== plan revised from an escalation
@scenario()
async def test_a_baseline_escalation_can_send_the_plan_back_to_the_planner(f: Flow):
    """The oracle's commands cannot run at the baseline (G-V2): H-2 is never reached, so the
    plan is revised from the escalation, and the oracle and baseline start over from it."""
    await f.to_h2(tier="R3")
    cmd = f.ce.baseline_commands()[0]
    f.runner.commands[cmd] = (127, "bun: command not found\n")
    await _baseline(f)
    base = f.ce.nodes["baseline"]
    assert base.status == ESCALATED and base.escalation.kind == "verify_unrunnable"
    with pytest.raises(WorkflowError, match="feedback is required"):
        await f.human("baseline", "revise_plan")
    plan_cycle, base_cycle = f.ce.nodes["plan"].cycle, base.cycle
    await f.human("baseline", "revise_plan", feedback="make every acceptance command self-contained")
    plan = f.ce.nodes["plan"]
    assert plan.cycle == plan_cycle + 1 and f.status("plan") == RUNNING
    assert any("self-contained" in fb.text for fb in plan.feedback)
    assert base.escalation is None and base.status == PENDING
    assert f.status("oracle") == PENDING and f.ce.change.oracle_sha is None
    del f.runner.commands[cmd]
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.oracle()
    await _baseline(f)
    assert base.status == ACCEPTED and base.cycle == base_cycle + 1
    assert f.status("H-2") == AWAITING_HUMAN


@scenario()
async def test_revise_plan_needs_work_built_on_the_design(f: Flow):
    """A build task's work is built on the Plan stage as a whole, not on its Design: sending the
    Design back is not offered there (sending the Plan back is)."""
    await f.to_build()
    f.ce.escalate(f.ce.nodes["build:T1"], "blocked", "stuck", resume=Resume.READY)
    await f.settle()
    with pytest.raises(WorkflowError, match="was not built on a design stage"):
        await f.human("build:T1", "revise_plan", feedback="replan please")
    assert "send_back:stage:plan" in f.ce.workflow.actions("build:T1")
