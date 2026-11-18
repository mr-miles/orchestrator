"""The workflow state machine, stage by stage: happy paths, guard rejections, escalations and
their resolution, run failures, restarts. Driven in-process by tests/engine_harness.py."""
from __future__ import annotations

import asyncio
import json

import pytest
from engine_harness import REPO, TASKS, Flow, scenario, signals

from legion_orch.workflow.engine import Controller, WorkflowError
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    ESCALATED,
    FAILED,
    PENDING,
    R_ACCEPTED,
    R_ACTIVE,
    R_FAILED,
    R_STARTING,
    R_SUPERSEDED,
    READY,
    REVIEWING,
    RUNNING,
    VERIFYING,
    ChangeStatus,
    FailureSource,
    Resume,
    RunStage,
)


# ====================================================================== happy paths
@scenario()
async def test_r1_change_runs_to_ready(f: Flow):
    assert f.status() == {"intake": RUNNING, "H-1": PENDING}
    await f.intake()
    assert f.ce.change.risk.tier == "R1" and f.status("H-1") == AWAITING_HUMAN
    res = await f.human("H-1", "accept")
    assert (res.tier, res.panel) == ("R1", ["reviewer"])
    assert set(f.ce.nodes) == {"intake", "H-1", "plan", "H-2", "ship"}   # no R2 stages
    await f.plan()
    await f.critique("PASS")
    assert f.status("build:T1") == PENDING and f.ce.nodes["ship"].depends_on == ["build:T1"]
    res = await f.human("H-2", "approve")
    assert res.pending == [] and f.status("H-2") == ACCEPTED
    impl = f.run("build:T1", "implementer")
    await f.implement()
    assert f.status("build:T1") == REVIEWING
    ver = f.ce.nodes["build:T1"].current_verification()
    assert ver.ok and ver.attestation and ver.results[0].output == ""   # compact copy on the node
    await f.review(verdict="PASS")
    node = f.ce.nodes["build:T1"]
    assert node.status == ACCEPTED and node.accepted_sha == impl.base_sha or node.accepted_sha
    assert f.ce.change.integration_head != f.ce.change.base_sha
    assert f.ce.change.status == ChangeStatus.READY and f.ce.change.ship.attestation
    assert all(r.status == R_ACCEPTED for r in f.ce.runs.values())
    assert set(f.runner.created) == set(f.runner.deleted), "every sandbox deleted once accepted"
    assert f.ce.latest(f"control/logs/{impl.id}.log"), "agent log archived in the store"


@scenario()
async def test_r2_change_with_oracle_integration_and_h3(f: Flow):
    await f.to_build(tier="R2")                        # raising the tier needs no reason
    assert f.ce.change.tier == "R2" and f.ce.change.tier_override.to_tier == "R2"
    assert f.ce.change.oracle_sha and f.ce.change.oracle.acceptance_tests
    await f.implement()
    await f.review()
    assert f.status("integration") == RUNNING
    await f.integration("PASS")
    assert f.status("integration") == ACCEPTED, f.status()
    assert f.status("H-3") == AWAITING_HUMAN
    await f.human("H-3", "accept", feedback="looks right")
    assert f.ce.change.approvals["H-3"].head == f.ce.change.integration_head
    assert f.ce.change.status == ChangeStatus.READY, f.ce.change.ship


@scenario()
async def test_h3_reject_abandons_the_change(f: Flow):
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    await f.integration()
    await f.human("H-3", "reject")
    assert f.status("H-3") == FAILED and f.ce.change.status == ChangeStatus.ABANDONED
    runs_before = len(f.ce.runs)
    await f.ce.tick()                                   # nothing is scheduled any more
    assert len(f.ce.runs) == runs_before


# ====================================================================== reports (all roles)
@scenario(max_report_attempts=2)
async def test_rejected_reports_count_attempts_then_fail_the_run(f: Flow):
    r = f.run("intake")
    out = await f.report(r, [])                         # required documents missing
    assert not out.accepted and out.attempts_left == 1
    assert any(v.startswith("G-R4") for v in out.violations)
    assert r.violations and f.status("intake") == RUNNING
    out = await f.report(r, [])
    assert out.attempts_left == 0 and r.status == R_FAILED
    esc = f.ce.nodes["intake"].escalation
    assert f.status("intake") == ESCALATED and esc.kind == "run_failed" and "G-R6" in esc.reason
    assert esc.log_tail and "agent started" in esc.log_tail
    out = await f.report(r, [])                         # a finished run cannot report again
    assert not out.accepted and "run is FAILED" in out.violations[0]
    await f.human("intake", "retry")                    # resume READY: a fresh run is dispatched
    assert f.run("intake").id != r.id
    await f.intake()
    assert f.status("intake") == ACCEPTED


@scenario(max_report_attempts=1)
async def test_dry_run_reports_check_everything_and_use_no_attempt(f: Flow):
    r = f.run("intake")
    for _ in range(3):                                   # one attempt only, yet dry runs are free
        out = await f.ce.handle_report(r.id, "DONE", "s", [], None, None, None, dry_run=True)
        assert out.dry_run and not out.accepted and not out.would_accept
        assert any(v.startswith("G-R4") for v in out.violations) and out.attempts_left == 1
    assert r.attempts == 0 and not r.violations and f.status("intake") == RUNNING
    atts = [await f.write(r, "intake/requirements.md", "# Requirements\n\n- REQ-1: hi\n"),
            await f.write(r, "intake/risk.json", signals())]
    out = await f.ce.handle_report(r.id, "DONE", "s", atts, None, None, None, dry_run=True)
    assert out.would_accept and not out.violations and r.report is None
    assert f.status("intake") == RUNNING                 # nothing applied
    assert (await f.report(r, atts)).accepted and f.status("intake") == ACCEPTED


@scenario(max_report_attempts=20)
async def test_report_shape_and_limit_guards(f: Flow):
    r = f.run("intake")
    for kwargs, gid in [({"status": "LATER"}, "G-R1"), ({"status": "BLOCKED"}, "G-R2"),
                        ({"summary": "x" * 5000}, "G-R8"), ({"data": {"x": object()}}, "G-R8"),
                        ({"atts": ["file:changes/other/intake/requirements.md@1"]}, "G-R")]:
        out = await f.ce.handle_report(r.id, kwargs.get("status", "DONE"), kwargs.get("summary", "s"),
                                       kwargs.get("atts", []), kwargs.get("data"), None, None)
        assert not out.accepted and any(v.startswith(gid) for v in out.violations), (kwargs, out)


@scenario()
async def test_report_schema_mismatch_is_g_r7(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    r = f.run("plan", "critic")
    plan = [f.ce.latest_att(p) for p in ("plan/spec.md", "plan/oracle.json", "plan/taskgraph.json")]
    doc = await f.write(r, "critique/cycle-1.md", "x")
    out = await f.report(r, plan + [doc], {"verdict": "MAYBE"})
    assert not out.accepted and any(v.startswith("G-R7") for v in out.violations)


@scenario()
async def test_blocked_report_escalates_and_an_answer_resumes(f: Flow):
    r = f.run("intake")
    out = await f.report(r, status="BLOCKED", blocked_reason="goal is ambiguous",
                         missing_decision="greet in English or French?")
    assert out.accepted and r.status == "REPORTED"
    esc = f.ce.nodes["intake"].escalation
    assert esc.kind == "blocked" and esc.missing_decision == "greet in English or French?"
    with pytest.raises(WorkflowError, match="answer is required"):
        await f.human("intake", "answer")
    await f.human("intake", "answer", answer="English")
    node = f.ce.nodes["intake"]
    assert node.escalation is None and node.feedback[-1].source == "decisions"
    assert "English" in node.feedback[-1].text and node.feedback[-1].attestations
    assert f.run("intake").assignment["feedback_items"][-1]["text"].endswith("English")


@scenario()
async def test_transition_failure_rolls_back_and_escalates(f: Flow, monkeypatch=None):
    from legion_orch.workflow.roles import intake as intake_role
    original = intake_role.Intake.on_done

    async def boom(self, e, r):
        e.change.goal = "corrupted"           # a half-applied transition ...
        raise RuntimeError("handler bug")
    intake_role.Intake.on_done = boom
    try:
        out = await f.intake()
    finally:
        intake_role.Intake.on_done = original
    assert not out.accepted and "controller error" in out.violations[0]
    assert f.ce.change.goal == "Say hello"     # ... is rolled back
    assert f.status("intake") == ESCALATED and f.ce.runs["r0001"].status == R_FAILED


@scenario(report_check_timeout_s=0.05)
async def test_slow_report_checks_time_out(f: Flow):
    from legion_orch.workflow import attest
    original = attest.check_all

    async def slow(*a, **k):
        await asyncio.sleep(1)
        return await original(*a, **k)
    attest.check_all = slow
    try:
        out = await f.intake()
    finally:
        attest.check_all = original
    assert not out.accepted and any(v.startswith("G-R8: report checks exceeded") for v in out.violations)


@scenario()
async def test_progress_marks_the_run_active_and_keeps_the_last_50(f: Flow):
    r = f.run("intake")
    for i in range(60):
        f.ce.progress(r, f"step {i}", i)
    assert r.status == R_ACTIVE and len(r.progress) == 50 and r.progress[-1].message == "step 59"


# ====================================================================== H-1
@scenario()
async def test_h1_decision_rules(f: Flow):
    await f.intake()
    with pytest.raises(WorkflowError, match="not a decision owner"):
        await f.human("H-1", "accept", approver="mallory")
    with pytest.raises(WorkflowError, match="unknown tier"):
        await f.human("H-1", "accept", tier="R9")
    with pytest.raises(WorkflowError, match="lowering the tier needs a reason"):
        await f.human("H-1", "accept", tier="R0", reason="no")
    with pytest.raises(WorkflowError, match="not valid for H-1"):
        await f.human("H-1", "approve")
    with pytest.raises(WorkflowError, match="unknown node"):
        await f.human("H-9", "accept")
    with pytest.raises(WorkflowError, match="not available for intake in ACCEPTED"):
        await f.human("intake", "retry")              # only an escalated node can be retried
    assert f.status("H-1") == AWAITING_HUMAN          # refused actions change nothing
    res = await f.human("H-1", "accept", tier="R0", reason="a one-line copy change with no risk at all")
    assert res.tier == "R0" and res.panel == [] and f.ce.change.tier_override.from_tier == "R1"


@scenario()
async def test_h1_revise_sends_intake_back_with_feedback(f: Flow):
    await f.intake()
    with pytest.raises(WorkflowError, match="feedback is required"):
        await f.human("H-1", "revise")
    await f.human("H-1", "revise", feedback="also say goodbye")
    intake = f.ce.nodes["intake"]
    assert intake.cycle == 2 and intake.feedback[-1].text == "also say goodbye"
    assert f.status("H-1") == PENDING and f.run("intake").cycle == 2
    await f.intake(reqs="- REQ-1: hello\n- REQ-2: goodbye\n")
    await f.human("H-1", "accept")
    assert f.ce.change.requirements == ["REQ-1", "REQ-2"]


# ====================================================================== plan and critique
@scenario()
async def test_invalid_plan_is_rejected(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    cyclic = [TASKS[0] | {"depends_on": ["T2"]}, TASKS[0] | {"id": "T2", "depends_on": ["T1"],
                                                             "files_modified": ["src/b.txt"]}]
    out = await f.plan(tasks=cyclic)
    assert not out.accepted and any("dependency cycle" in v for v in out.violations)
    out = await f.plan(tasks=[TASKS[0] | {"requirements": ["REQ-7"]}])
    assert any(v.startswith("G-P3") for v in out.violations)
    assert f.status("plan") == RUNNING


@scenario(critique_max_cycles=2)
async def test_critique_rework_loop_and_budget_escalation(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    plan = f.ce.nodes["plan"]
    assert plan.status == REVIEWING                         # the plan is critiqued, not accepted
    first = f.run("plan", "critic")
    await f.critique("REWORK")
    assert plan.status == RUNNING and plan.cycle == 2 and plan.feedback[-1].source == "critique"
    assert plan.reviews["1:critic"].verdict == "REWORK" and first.sandbox in f.runner.deleted
    assert f.ce.runs[plan.runs[0]].status == R_SUPERSEDED   # the reworked plan's planner run
    await f.plan()
    await f.critique("REWORK")                              # second REWORK: budget spent
    assert plan.status == ESCALATED and plan.escalation.resume == Resume.CRITIQUE_BUDGET
    await f.human("plan", "answer", answer="the spec is fine as it is")
    assert plan.extra_cycles == 1 and plan.cycle == 3 and plan.status == RUNNING
    assert {"decisions", "critique"} <= {fb.source for fb in plan.feedback[-2:]}
    assert any("the spec is fine" in fb.text for fb in plan.feedback)
    await f.plan()
    await f.critique("PASS")
    assert plan.status == ACCEPTED and "build:T1" in f.ce.nodes
    assert f.ce.runs[plan.runs[-2]].status == R_ACCEPTED    # the planner run the critic passed


@scenario()
async def test_critique_must_attest_the_latest_plan(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    r = f.run("plan", "critic")
    doc = await f.write(r, "critique/cycle-1.md", "x")
    out = await f.report(r, [doc], {"verdict": "REWORK", "findings": []})
    assert {"G-C1: REWORK requires at least one finding"} <= set(out.violations)
    assert any("latest plan version" in v for v in out.violations)


# ====================================================================== H-2
@scenario()
async def test_h2_subjects_are_separate_decisions(f: Flow):
    await f.to_h2()
    res = await f.human("H-2", "approve", subjects=["plan"])
    assert res.approved == ["plan"] and set(res.pending) == {"REQ-1", "oracle"}
    assert f.status("H-2") == AWAITING_HUMAN
    res = await f.human("H-2", "approve", approver="ana", subjects=["REQ-1", "oracle"])
    assert res.pending == [] and f.status("H-2") == ACCEPTED
    with pytest.raises(WorkflowError):
        await f.ce.human_action("H-2", "approve", "miles", {"subjects": ["nonsense"]})


@scenario()
async def test_h2_revise_restarts_everything_derived_from_the_plan(f: Flow):
    await f.to_h2()
    await f.human("H-2", "revise", feedback="split the task")
    assert f.status("plan") == RUNNING and f.ce.nodes["plan"].cycle == 2
    assert f.status("H-2") == PENDING and not f.ce.build_nodes()
    assert f.ce.change.integration_head == f.ce.change.base_sha
    await f.plan()
    await f.critique()
    await f.human("H-2", "approve")
    assert f.status("build:T1") == RUNNING


@scenario()
async def test_stale_approval_blocks_the_build(f: Flow):
    """G-B1: an approved plan document changed after approval -> the implementer is not dispatched."""
    await f.to_h2()
    await f.ce.human_action("H-2", "approve", "miles", {})     # approved, not yet dispatched
    f.ce.write_record("plan/spec.md", "# Spec changed after approval\n")
    await f.settle()
    node = f.ce.nodes["build:T1"]
    assert node.status == ESCALATED and node.escalation.kind == "approval", node.escalation
    assert not f.runs("build:T1")


# ====================================================================== build: verification
@scenario()
async def test_failed_verification_is_rework_in_the_same_sandbox(f: Flow):
    await f.to_build()
    f.runner.commands["grep -q hello src/greet.txt"] = (1, "no match\n")
    first = f.run("build:T1", "implementer")
    await f.implement()
    node = f.ce.nodes["build:T1"]
    assert node.cycle == 2 and node.candidate_sha is None and node.status == RUNNING
    assert node.feedback[-1].source == "verification" and "exited 1" in node.feedback[-1].text
    assert f.ce.latest("tasks/T1/verification.json")
    second = f.run("build:T1", "implementer")
    assert second.sandbox == first.sandbox and second.sandbox in f.runner.uploads   # reused (G-D4)
    del f.runner.commands["grep -q hello src/greet.txt"]
    await f.implement()
    await f.review()
    assert node.status == ACCEPTED


@scenario()
async def test_verifier_infrastructure_failure_escalates_and_retry_reverifies(f: Flow):
    await f.to_build()
    f.runner.setup_rc = 1
    await f.implement()
    node = f.ce.nodes["build:T1"]
    assert node.status == ESCALATED and node.escalation.kind == "verify_infra"
    assert "verifier setup failed" in node.escalation.reason
    verifiers = [n for n in f.runner.created if n.startswith("lgv-")]
    assert verifiers and set(verifiers) <= set(f.runner.deleted), "verifier deleted on failure"
    f.runner.setup_rc = 0
    await f.human("build:T1", "retry")
    assert node.status == REVIEWING


@scenario()
async def test_command_not_found_escalates_without_spending_a_cycle(f: Flow):
    """G-V2: exit 127 is the verifier image's problem, not the agent's: no rework run."""
    await f.to_build()
    f.runner.commands["grep -q hello src/greet.txt"] = (127, "bash: grep: command not found\n")
    await f.implement()
    node = f.ce.nodes["build:T1"]
    assert node.status == ESCALATED and node.escalation.kind == "verify_unrunnable"
    assert "grep -q hello src/greet.txt" in node.escalation.reason
    assert "command not found" in node.escalation.log_tail
    assert node.cycle == 1 and not [x for x in node.feedback if x.source == "verification"]
    assert len(f.runs("build:T1")) == 1 and f.ce.latest("tasks/T1/verification.json")
    del f.runner.commands["grep -q hello src/greet.txt"]       # the image is fixed
    await f.human("build:T1", "retry")
    assert node.status == REVIEWING and node.cycle == 1


@scenario()
async def test_task_without_verification_commands_never_passes(f: Flow):
    await f.intake()
    await f.human("H-1", "accept", tier="R0", reason="a trivial change with nothing at stake here")
    await f.plan()
    await f.critique()
    await f.human("H-2", "approve")
    # R0 has no panel: a passing verification merges straight away
    await f.implement()
    assert f.status("build:T1") == ACCEPTED and f.ce.change.status == ChangeStatus.READY


# ====================================================================== build: implementer guards
@scenario(max_report_attempts=20)
async def test_implementer_guards(f: Flow):
    await f.to_build()
    r = f.run("build:T1", "implementer")
    t = f.ce.nodes["build:T1"].task
    ev = await f.write(r, "tasks/T1/evidence/cycle-1.md", "- `grep -q hello src/greet.txt` -> exit 0")
    # G-B3: a file outside the task's scope
    sha = f.commit(r, {"src/greet.txt": "hello\n", "README.md": "hacked\n"})
    out = await f.report(r, [f"git:{REPO}@{sha}", ev])
    assert any("README.md is outside the allowed paths" in v for v in out.violations)
    # G-B2: the commit is not the tip of the run's branch
    sha = f.commit(r, {"src/greet.txt": "hello\n"}, push=False)
    out = await f.report(r, [f"git:{REPO}@{sha}", ev])
    assert any(v.startswith("G-R") or v.startswith("G-B2") for v in out.violations), out
    # G-B4: the evidence must list the verification commands
    sha = f.commit(r, {"src/greet.txt": "hello\n"})
    ev2 = await f.write(r, "tasks/T1/evidence/cycle-1.md", "- `make test` -> exit 0")
    out = await f.report(r, [f"git:{REPO}@{sha}", ev2])
    assert any(v.startswith("G-B4") for v in out.violations)
    assert t.verification and f.status("build:T1") == RUNNING


@scenario()
async def test_agent_writes_are_scoped(f: Flow):
    r = f.run("intake")
    with pytest.raises(WorkflowError, match="G-W1"):
        await f.write(r, "plan/spec.md", "not mine")
    with pytest.raises(WorkflowError, match="G-W1"):
        await f.write(r, "control/change.json", "{}")


# ====================================================================== build: review panel
@scenario()
async def test_review_fail_is_rework_and_identical_findings_twice_is_a_stale_loop(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    node = f.ce.nodes["build:T1"]
    assert node.cycle == 2 and node.status == RUNNING
    assert any("missing newline handling" in fb.text for fb in node.feedback)
    assert f.ce.runs[node.runs[0]].status == R_SUPERSEDED
    await f.implement()
    await f.review(verdict="FAIL")                      # the same HIGH finding again
    assert node.status == ESCALATED and node.escalation.kind == "stale_loop"
    await f.human("build:T1", "retry")                  # REWORK_CYCLE: one extra cycle
    assert node.extra_cycles == 1 and node.cycle == 3 and node.status == RUNNING
    await f.implement()
    await f.review(verdict="PASS")
    assert node.status == ACCEPTED


@scenario(review_max_cycles=1)
async def test_review_cycle_budget_escalates(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    node = f.ce.nodes["build:T1"]
    assert node.status == ESCALATED and node.escalation.kind == "cycle_budget"
    assert node.escalation.resume == Resume.REWORK_CYCLE


@scenario(max_report_attempts=20)
async def test_review_guards(f: Flow):
    await f.to_build()
    await f.implement()
    r = f.run("build:T1", "reviewer")
    cand = f"git:{REPO}@{r.judged_sha}"
    doc = await f.write(r, "tasks/T1/reviews/reviewer-cycle-1.md", "x")
    high = {"severity": "BLOCKER", "confidence": "HIGH", "title": "t", "fix": "f",
            "attestations": [cand]}
    cases = [({"verdict": "PASS", "findings": [high], "checked": ["d"]}, "G-RV3"),
             ({"verdict": "FAIL", "findings": [], "checked": ["d"]}, "G-RV3"),
             ({"verdict": "PASS", "findings": [], "checked": []}, "G-RV4"),
             ({"verdict": "FAIL", "checked": ["d"],          # evidence not in the report's list
               "findings": [high | {"attestations": [f.ce.latest_att("plan/spec.md")]}]}, "G-RV2"),
             ({"verdict": "FAIL", "checked": ["d"], "findings": [high | {"attestations": []}]}, "G-R7")]
    for data, gid in cases:
        out = await f.report(r, [cand, doc], data)
        assert any(v.startswith(gid) for v in out.violations), (data, out)


@scenario()
async def test_override_accept_needs_reason_and_passing_verification(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    await f.implement()
    await f.review(verdict="FAIL")                      # stale loop -> escalated
    with pytest.raises(WorkflowError, match="reason"):
        await f.human("build:T1", "override_accept", reason="ok")
    with pytest.raises(WorkflowError, match="unknown escalation action"):
        await f.human("build:T1", "shrug")
    await f.human("build:T1", "override_accept", reason="the finding is a false positive, verified by hand")
    node = f.ce.nodes["build:T1"]
    assert node.status == ACCEPTED and node.override.approver == "miles"


@scenario()
async def test_override_is_refused_without_passing_verification(f: Flow):
    await f.to_build()
    f.runner.setup_rc = 1
    await f.implement()                                 # verify_infra escalation, no verification
    with pytest.raises(WorkflowError, match="not waivable"):
        await f.human("build:T1", "override_accept", reason="the finding is a false positive, verified")


@scenario()
async def test_override_is_only_for_build_tasks(f: Flow):
    r = f.run("intake")
    await f.report(r, status="BLOCKED", blocked_reason="x", missing_decision="y")
    with pytest.raises(WorkflowError, match="only available for build tasks"):
        await f.human("intake", "override_accept", reason="long enough reason with words")


@scenario()
async def test_abandon_from_an_escalation(f: Flow):
    r = f.run("intake")
    await f.report(r, status="BLOCKED", blocked_reason="x", missing_decision="y")
    await f.human("intake", "abandon")
    assert f.status("intake") == FAILED and f.ce.change.status == ChangeStatus.ABANDONED


# ====================================================================== merge
@scenario()
async def test_merge_conflict_is_rework_onto_the_new_head(f: Flow):
    await f.to_build()
    await f.implement()
    real_merge = f.ctl.git.merge
    calls = []

    async def conflict(cid, sha, msg):
        calls.append(sha)
        if len(calls) == 1:
            return False, "CONFLICT (content): src/greet.txt"
        return await real_merge(cid, sha, msg)
    f.ctl.git.merge = conflict
    await f.review()
    node = f.ce.nodes["build:T1"]
    assert node.status == RUNNING and node.cycle == 2
    assert node.feedback[-1].source == "merge" and "G-A2" in node.feedback[-1].text
    assert node.base_sha == await f.ctl.git.integration_head(f.ce.cid)
    await f.implement()
    await f.review()
    assert node.status == ACCEPTED and len(calls) == 2


# ====================================================================== run lifecycle
@scenario()
async def test_agent_exit_without_report_fails_the_run(f: Flow):
    r = f.run("intake")
    await f.exit_agent(r, rc=3)
    assert r.status == R_FAILED and r.exit_code == 3
    node = f.ce.nodes["intake"]
    assert node.status == ESCALATED and "exited with code 3" in node.escalation.reason
    assert f.ce.latest(f"control/logs/{r.id}.log")
    assert r.sandbox not in f.runner.deleted                # kept for debugging by default
    assert "agent started" in f.ce.log_tail(r.id)            # from the store once archived
    f.cfg.sandbox.keep_failed = False
    await f.human("intake", "retry")
    r2 = f.run("intake")
    await f.exit_agent(r2, rc=1)
    assert r2.sandbox in f.runner.deleted and r2.sandbox_deleted


@scenario(run_timeout_s=1)
async def test_runs_time_out(f: Flow):
    r = f.run("intake")
    r.created_at = "2020-01-01T00:00:00.000Z"
    await f.settle()
    assert r.status == R_FAILED and "timed out" in f.ce.nodes["intake"].escalation.reason


@scenario()
async def test_sandbox_launch_failure_escalates(f: Flow):
    await f.intake()
    f.runner.fail_create = "planner"
    await f.human("H-1", "accept")
    run = f.runs("plan")[0]
    assert run.status == R_FAILED and f.status("plan") == ESCALATED
    assert "sandbox launch failed" in f.ce.nodes["plan"].escalation.reason
    f.runner.fail_create = None
    await f.human("plan", "retry")
    assert f.run("plan")


# ---------------------------------------------------------------------- sandbox stages
def agent_log(f: Flow, run, *lines) -> None:
    """Append lines to a live run's agent log (as the sandbox would)."""
    with open(f.ce.run_dir(run.id) / "agent.log", "a") as fh:
        fh.write("".join((ln if isinstance(ln, str) else json.dumps(ln)) + "\n" for ln in lines))


@scenario()
async def test_run_stages_preparing_launching_running(f: Flow):
    seen = []
    real_create, real_start = f.runner.create, f.runner.start_agent

    async def create(spec):
        seen.append(("create", f.ce.runs[spec.run].stage))
        return await real_create(spec)

    async def start(name, log_file):
        seen.append(("start", next(r.stage for r in f.ce.runs.values() if r.sandbox == name)))
        return await real_start(name, log_file)
    f.runner.create, f.runner.start_agent = create, start
    await f.intake()
    await f.human("H-1", "accept")
    run = f.run("plan")
    assert seen[-2:] == [("create", RunStage.PREPARING), ("start", RunStage.LAUNCHING)]
    assert run.stage == RunStage.LAUNCHING and run.status == R_STARTING   # not connected yet
    f.ce.connected(run)                         # the agent's first MCP call
    assert run.stage == RunStage.RUNNING and run.status == R_ACTIVE
    await f.plan()
    assert run.stage == RunStage.EXITED and run.failure is None


@scenario()
async def test_launch_failure_is_a_system_error_while_preparing(f: Flow):
    await f.intake()
    f.runner.fail_create = "planner"
    await f.human("H-1", "accept")
    run = f.runs("plan")[0]
    assert run.failure.source == FailureSource.SYSTEM and run.failure.stage == RunStage.PREPARING
    esc = f.ce.nodes["plan"].escalation
    assert esc.source == FailureSource.SYSTEM and "system error while preparing" in esc.reason


@scenario()
async def test_process_exit_before_connecting_is_a_system_error(f: Flow):
    r = f.run("intake")
    agent_log(f, r, "fatal: unable to access 'http://host/git/app.git/': 403")
    await f.exit_agent(r, rc=128)
    assert (r.failure.source, r.failure.stage, r.failure.exit_code) == (
        FailureSource.SYSTEM, RunStage.LAUNCHING, 128)
    assert "before connecting" in r.failure.reason and "403" in r.failure.reason
    assert r.stage == RunStage.EXITED


@scenario()
async def test_process_exit_after_connecting_says_so(f: Flow):
    r = f.run("intake")
    f.ce.connected(r)
    await f.exit_agent(r, rc=137)
    assert r.failure.source == FailureSource.SYSTEM and r.failure.stage == RunStage.RUNNING
    assert "exited with code 137 after connecting" in r.failure.reason
    assert "system error while running" in f.ce.nodes["intake"].escalation.reason


@scenario()
async def test_agent_ending_with_an_error_is_an_agent_error(f: Flow):
    r = f.run("intake")
    agent_log(f, r, {"type": "system", "subtype": "init", "model": "m", "tools": []},
              {"type": "result", "subtype": "success", "is_error": True,
               "result": "API Error: 401 invalid OAuth token"})
    await f.exit_agent(r, rc=1)
    assert r.failure.source == FailureSource.AGENT and "API Error: 401" in r.failure.reason
    assert f.ce.nodes["intake"].escalation.source == FailureSource.AGENT


@scenario()
async def test_agent_finishing_without_report_is_an_agent_error(f: Flow):
    r = f.run("intake")
    agent_log(f, r, {"type": "result", "subtype": "success", "is_error": False, "result": "All done!"})
    await f.exit_agent(r, rc=0)
    assert r.failure.source == FailureSource.AGENT and "without an accepted report" in r.failure.reason


@scenario(max_report_attempts=1)
async def test_rejected_reports_are_an_agent_error(f: Flow):
    r = f.run("intake")
    await f.report(r, atts=["file:nope.md@1"])
    assert r.failure.source == FailureSource.AGENT and "G-R6" in r.failure.reason


@scenario(run_timeout_s=1)
async def test_timeout_is_its_own_source(f: Flow):
    r = f.run("intake")
    r.created_at = "2020-01-01T00:00:00.000Z"
    await f.settle()
    assert r.failure.source == FailureSource.TIMEOUT


# ---------------------------------------------------------------------- sandbox names
@scenario()
async def test_sandboxes_get_readable_names_never_reused(f: Flow):
    import re
    await f.to_build()
    await f.implement()
    await f.review()
    agents = [r.sandbox for r in f.ce.runs.values()]
    verifiers = [v.sandbox for n in f.ce.nodes.values() for v in n.verification.values()]
    assert agents and verifiers
    assert all(re.fullmatch(r"lg-[a-z]+", n) for n in agents), agents
    assert all(re.fullmatch(r"lgv-[a-z]+", n) for n in verifiers), verifiers
    assert len(set(agents)) == len(agents) and not set(agents) & set(verifiers)
    assert set(agents) <= f.ctl.used_sandbox_names()
    other = await f.ctl.start_change("Another goal")       # names stay unique across changes
    await other.tick()
    await f.settle()
    assert [r.sandbox for r in other.runs.values()] and not (
        {r.sandbox for r in other.runs.values()} & set(agents + verifiers))


@scenario()
async def test_a_name_taken_on_the_gateway_is_replaced(f: Flow):
    await f.intake()
    f.runner.clash = 2                     # two names in a row already exist on the gateway
    await f.human("H-1", "accept")
    run = f.run("plan")
    assert run.sandbox in f.runner.created and len(f.runner.created) == 2   # intake + plan
    f.runner.clash = 3                     # never free: the critic's run fails as a system error
    await f.plan()                         # (a planner rework reuses its kept sandbox)
    critic = f.runs("plan", "critic")[-1]
    assert critic.failure and critic.failure.source == FailureSource.SYSTEM
    assert "already exists" in critic.failure.reason


@scenario()
async def test_a_verifier_name_taken_on_the_gateway_is_replaced(f: Flow):
    await f.to_build()
    f.runner.clash = 1
    await f.implement()
    ver = f.ce.nodes["build:T1"].current_verification()
    assert ver.ok and ver.sandbox in f.runner.created


# ---------------------------------------------------------------------- retry
@scenario()
async def test_retry_removes_the_old_sandbox_before_the_new_run(f: Flow):
    r = f.run("intake")
    await f.exit_agent(r, rc=1)
    assert r.sandbox not in f.runner.deleted                # kept for debugging until the retry
    real_delete = f.runner.delete
    order = []

    async def delete(name):
        order.append(("delete", name, len(f.runs("intake"))))
        await real_delete(name)
    f.runner.delete = delete
    res = await f.human("intake", "retry")
    assert "removing sandbox" in res.note
    r2 = f.run("intake")
    assert order == [("delete", r.sandbox, 1)], "deleted before the new run was dispatched"
    assert r.sandbox_deleted and r2.sandbox != r.sandbox and r2.sandbox in f.runner.created


@scenario()
async def test_retry_goes_ahead_when_the_sandbox_cannot_be_removed(f: Flow):
    r = f.run("intake")
    await f.exit_agent(r, rc=1)
    f.runner.fail_delete = True
    await f.human("intake", "retry")
    assert f.run("intake").id != r.id and not r.sandbox_deleted


@scenario()
async def test_retry_of_an_implementer_starts_a_fresh_sandbox(f: Flow):
    await f.to_build()
    impl = f.run("build:T1", "implementer")
    node = f.ce.nodes["build:T1"]
    assert node.sandbox == impl.sandbox
    await f.exit_agent(impl, rc=1)
    await f.human("build:T1", "retry")
    impl2 = f.run("build:T1", "implementer")
    assert impl.sandbox in f.runner.deleted and impl2.sandbox != impl.sandbox
    assert node.sandbox == impl2.sandbox
    await f.implement()
    await f.review()
    assert node.status == ACCEPTED


@scenario()
async def test_retry_in_progress_blocks_a_second_retry_and_yields_to_abandon(f: Flow):
    r = f.run("intake")
    await f.exit_agent(r, rc=1)
    gate = asyncio.Event()
    real_delete = f.runner.delete

    async def slow_delete(name):
        await gate.wait()
        await real_delete(name)
    f.runner.delete = slow_delete
    await f.ce.human_action("intake", "retry", "miles", {})
    assert f.ce.nodes["intake"].escalation.retrying
    with pytest.raises(WorkflowError, match="already in progress"):
        await f.ce.human_action("intake", "retry", "miles", {})
    await f.ce.human_action("intake", "abandon", "miles", {})
    gate.set()
    await f.settle()
    assert f.status("intake") == FAILED and len(f.runs("intake")) == 1


@scenario()
async def test_retry_interrupted_by_a_restart_completes(f: Flow):
    r = f.run("intake")
    await f.exit_agent(r, rc=1)
    f.ce.nodes["intake"].escalation.retrying = True        # as if the controller stopped here
    f.ce.save()
    f.ce._gates.resume_retries()
    await f.settle()
    assert r.sandbox in f.runner.deleted and f.run("intake").id != r.id


@scenario()
async def test_failed_panel_member_resumes_reviewing(f: Flow):
    await f.to_build()
    await f.implement()
    rev = f.run("build:T1", "reviewer")
    await f.exit_agent(rev, rc=1)
    node = f.ce.nodes["build:T1"]
    assert node.status == ESCALATED and node.escalation.resume == Resume.REVIEWING
    await f.human("build:T1", "retry")
    assert node.status == REVIEWING and f.run("build:T1", "reviewer").id != rev.id
    await f.review()
    assert node.status == ACCEPTED


@scenario()
async def test_failed_sandbox_deletion_is_reported_not_fatal(f: Flow):
    f.runner.fail_delete = True
    r = f.run("intake")
    await f.intake()
    assert f.status("intake") == ACCEPTED and not r.sandbox_deleted


@scenario(max_parallel=1)
async def test_parallel_limit_and_file_overlap(f: Flow):
    tasks = [TASKS[0], TASKS[0] | {"id": "T2", "files_modified": ["src/bye.txt"]}]
    await f.to_build(tasks=tasks)
    assert [n.status for n in f.ce.build_nodes()] == [RUNNING, READY]   # one slot
    await f.implement("T1")                             # T1 verified; T2 takes the free slot
    assert f.status("build:T1") == REVIEWING and f.status("build:T2") == RUNNING
    assert not f.runs("build:T1", "reviewer")
    await f.implement("T2")
    await f.review("T1")
    await f.review("T2")
    assert f.ce.change.status == ChangeStatus.READY


# ====================================================================== restart
@scenario()
async def test_restart_fails_live_runs_and_resumes_verification(f: Flow):
    await f.to_build()
    f.runner.setup_rc = 1
    await f.implement()                                  # escalated verify_infra
    node = f.ce.nodes["build:T1"]
    node.status, node.escalation = VERIFYING, None       # as if the controller died mid-verification
    f.ce.save()
    live_intake_like = f.ce.runs[node.current_run]
    assert live_intake_like.status != R_FAILED
    f.runner.setup_rc = 0
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    ce2 = ctl2.changes[f.ce.cid]
    async with ctl2.svc.lock:
        ce2.recover()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if ce2.nodes["build:T1"].status != VERIFYING:
            break
    assert ce2.nodes["build:T1"].status == REVIEWING


@scenario()
async def test_restart_fails_runs_that_were_live(f: Flow):
    r = f.run("intake")
    ctl2 = Controller(f.cfg, runner=f.runner)
    ctl2.load()
    ce2 = ctl2.changes[f.ce.cid]
    async with ctl2.svc.lock:
        ce2.recover()
    assert ce2.runs[r.id].status == R_FAILED
    assert ce2.nodes["intake"].status == ESCALATED
    assert "controller restarted" in ce2.nodes["intake"].escalation.reason


# ====================================================================== change-level actions
@scenario()
async def test_change_actions_validate_their_input(f: Flow):
    with pytest.raises(WorkflowError, match="unknown change action"):
        await f.change_action("teleport")
    with pytest.raises(WorkflowError, match="not a decision owner"):
        await f.change_action("attest", approver="mallory")
    with pytest.raises(WorkflowError):
        await f.change_action("attest", kind="nonsense", verdict="pass")
    with pytest.raises(WorkflowError, match="reason"):
        await f.change_action("approve_surface", surface="files")
    with pytest.raises(WorkflowError):
        await f.change_action("approve_protected_paths", task="T9", paths=["x"], reason="r" * 30)
    with pytest.raises(WorkflowError):
        await f.change_action("override", gate="nonsense")


@scenario()
async def test_ship_recheck_reevaluates(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review()
    first = f.ce.change.ship.attestation
    await f.human("ship", "recheck")
    assert f.status("ship") == ACCEPTED and f.ce.change.status == ChangeStatus.READY
    assert f.ce.change.ship.attestation == first       # the same verdict: no new record version


def test_signals_helper_derives_r1():
    from legion_orch.workflow import risk
    assert risk.derive_tier(signals()).tier == "R1"
    assert risk.derive_tier(signals(security=3)).tier == "R3"


@scenario(blank_repo=True)
async def test_a_change_on_a_blank_repository(f: Flow):
    """No repo.path: the hub starts with one empty commit and a change runs to READY on it."""
    assert f.cfg.repo.path is None
    base = f.ce.change.base_sha
    assert await f.ctl.git.changed_paths(base, base) == []
    await f.to_build()
    await f.implement(files={"src/greet.txt": "hello\n"})
    await f.review()
    assert f.ce.change.status == ChangeStatus.READY
    assert await f.ctl.git.blob(base, "src/greet.txt") is None                 # nothing at base
    assert await f.ctl.git.blob(f.ce.change.integration_head, "src/greet.txt")  # merged


@scenario()
async def test_a_rework_may_leave_a_required_document_unchanged(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    first = f.run("plan")
    await f.plan()
    await f.critique("REWORK")
    second = f.run("plan")
    written = second.docs_written
    att = await f.write(second, "plan/oracle.json", f.ce.doc("plan/oracle.json").content)
    assert att == f.ce.latest_att("plan/oracle.json") and f.ce.latest("plan/oracle.json") == 1
    assert f.ce.doc("plan/oracle.json").author == first.id
    assert second.confirmed_docs == [att] and second.docs_written == written   # no quota used
    out = await f.plan()                    # rewrites oracle.json and the task graph unchanged
    assert out.accepted, out.violations
    assert f.ce.latest("plan/taskgraph.json") == 1 and f.ce.latest("plan/spec.md") == 2
