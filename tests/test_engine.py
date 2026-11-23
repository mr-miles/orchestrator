"""The engine on tickets, case by case: reports and their guards, sign-offs, the plan and its
critique, verification, the implementer's and the judges' guards, merges, the run lifecycle
(stages, failures and who they are from, retries, names), restarts and the turn that ends a run.
Driven in-process by tests/engine_harness.py; whole changes are tests/test_ticket_engine.py's."""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from engine_harness import REPO, TASKS, Flow, scenario, signals

from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.model import (
    R_ACTIVE,
    R_FAILED,
    R_STARTING,
    ChangeStatus,
    FailureSource,
    RunStage,
)


def waiting(f: Flow, tid: str) -> tuple[str, str | None]:
    t = f.ticket(tid)
    return str(t.status), (str(t.waiting) if t.waiting else None)


def why(f: Flow, tid: str) -> str:
    return " ".join(f.ticket(tid).findings)


async def to_critique(f: Flow):
    await f.intake()
    await f.signoff("H-1")
    await f.plan()
    return f.run("critique", "critic")


# ====================================================================== reports (all roles)
@scenario(max_report_attempts=2)
async def test_rejected_reports_use_up_the_run_and_its_ticket_tries_again(f: Flow):
    r = f.run("intake")
    out = await f.report(r, [])                         # required outputs missing
    assert not out.accepted and out.attempts_left == 1
    assert r.violations and f.status("intake") == "working"
    out = await f.report(r, [])
    assert out.attempts_left == 0 and r.status == R_FAILED
    assert r.failure.source == FailureSource.AGENT and "G-R6" in r.failure.reason
    out = await f.report(r, [])                         # a finished run cannot report again
    assert not out.accepted and "run is FAILED" in out.violations[0]
    await f.settle()
    t = f.ticket("intake")                              # the agent's failure: an attempt
    assert (t.status, t.attempt) == ("working", 2) and f.run("intake").id != r.id
    assert any("G-R6" in x for x in f.run("intake").assignment["findings"])
    await f.intake()
    assert f.status("intake") == "checking"


@scenario(max_report_attempts=1)
async def test_dry_run_reports_check_everything_and_use_no_attempt(f: Flow):
    r = f.run("intake")
    for _ in range(3):                                   # one attempt only, yet dry runs are free
        out = await f.ce.handle_report(r.id, "DONE", "s", [], None, None, None, dry_run=True)
        assert out.dry_run and not out.accepted and not out.would_accept
        assert out.violations and out.attempts_left == 1
    assert r.attempts == 0 and not r.violations and f.status("intake") == "working"
    req = await f.write(r, "requirements.md", "# Requirements\n\n- REQ-1: hi\n")
    risk = await f.write(r, "risk.json", signals())
    outputs = {"requirements": req, "risk": risk}
    out = await f.ce.handle_report(r.id, "DONE", "s", [req, risk], None, None, None, True,
                                   outputs)
    assert out.would_accept and not out.violations and r.report is None
    assert f.status("intake") == "working"               # nothing applied
    assert (await f.report(r, [req, risk], outputs=outputs)).accepted
    assert f.status("intake") == "checking"


@scenario(max_report_attempts=20)
async def test_report_shape_and_limit_guards(f: Flow):
    r = f.run("intake")
    for kwargs, gid in [({"status": "LATER"}, "G-R1"), ({"status": "BLOCKED"}, "G-R2"),
                        ({"summary": "x" * 5000}, "G-R8"), ({"data": {"x": object()}}, "G-R8"),
                        ({"atts": ["file:changes/other/tickets/intake/requirements.md@1"]},
                         "G-R")]:
        out = await f.ce.handle_report(r.id, kwargs.get("status", "DONE"),
                                       kwargs.get("summary", "s"), kwargs.get("atts", []),
                                       kwargs.get("data"), None, None)
        assert not out.accepted and any(v.startswith(gid) for v in out.violations), (kwargs, out)


@scenario(max_report_attempts=20)
async def test_a_report_must_name_its_roles_outputs(f: Flow):
    r = f.run("intake")
    req = await f.write(r, "requirements.md", "# Requirements\n\n- REQ-1: hi\n")
    out = await f.report(r, [req], outputs={"requirements": req})
    assert not out.accepted and any("risk" in v for v in out.violations), out.violations
    out = await f.report(r, [req], outputs={"requirements": req, "risk": req, "extra": req})
    assert not out.accepted and any("extra" in v for v in out.violations), out.violations


@scenario()
async def test_report_schema_mismatch_is_g_r7(f: Flow):
    r = await to_critique(f)
    design = f.ticket("design")
    plan = [x for v in design.outputs.values() for x in (v if isinstance(v, list) else [v])]
    doc = await f.write(r, "critique.md", "x")
    out = await f.report(r, plan + [doc], {"verdict": "MAYBE"}, outputs={"critique": doc})
    assert not out.accepted and any(v.startswith("G-R7") for v in out.violations)


@scenario()
async def test_a_blocked_report_waits_and_a_retry_carries_the_answer(f: Flow):
    r = f.run("intake")
    out = await f.report(r, status="BLOCKED", blocked_reason="goal is ambiguous",
                         missing_decision="greet in English or French?")
    assert out.accepted and r.status == "REPORTED"
    assert waiting(f, "intake") == ("waiting", "blocked")
    assert "greet in English or French?" in why(f, "intake")
    with pytest.raises(WorkflowError, match="not a remedy"):
        await f.human("intake", "waive", reason="nothing to waive here at all")
    await f.human("intake", "retry", answer="English")
    t = f.ticket("intake")
    assert t.status == "working" and t.attempt == 1          # a blocked round is no attempt
    assert any(x.endswith("English") for x in f.run("intake").assignment["findings"])


@scenario()
async def test_a_role_handler_that_fails_is_the_controllers_error(f: Flow):
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
    # the report is accepted; what it means is applied when the agent's turn ends
    assert out.accepted and "a2a_reply" in out.instruction
    assert f.ce.change.goal == "Say hello"     # ... is rolled back
    assert f.ce.runs["r0001"].status == R_FAILED
    assert f.ce.runs["r0001"].failure.source == FailureSource.CONTROLLER
    await f.settle()
    again = f.run("intake")                                  # retried by itself, at once
    assert again.id != "r0001" and f.ticket("intake").attempt == 1   # never an attempt
    await f.intake()
    assert f.status("intake") == "checking"


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
    assert not out.accepted and any(v.startswith("G-R8: report checks exceeded")
                                    for v in out.violations)


@scenario()
async def test_progress_marks_the_run_active_and_keeps_the_last_50(f: Flow):
    r = f.run("intake")
    for i in range(60):
        f.ce.progress(r, f"step {i}", i)
    assert r.status == R_ACTIVE and len(r.progress) == 50 and r.progress[-1].message == "step 59"


# ====================================================================== sign-offs
@scenario()
async def test_sign_off_decision_rules(f: Flow):
    await f.intake()
    with pytest.raises(WorkflowError, match="not a decision owner"):
        await f.signoff("H-1", approver="mallory")
    with pytest.raises(WorkflowError, match="needs a reason"):
        await f.signoff("H-1", approve=False, reason=" ")
    with pytest.raises(WorkflowError, match="not waiting for a sign-off"):
        await f.human("H-E1", "approve")
    with pytest.raises(WorkflowError, match="unknown action"):
        await f.human("H-1", "teleport")
    with pytest.raises(WorkflowError, match="not a remedy"):
        await f.human("intake", "retry")                    # only a waiting ticket's
    assert f.status("H-1") == "working"                     # refused actions change nothing
    await f.signoff("H-1")
    rec = f.ce.json_doc("tickets/H-1/signoff.json")
    assert (rec["decision"], rec["approver"]) == ("approved", "miles")
    assert set(rec["subjects"]) == {"requirements", "risk"}  # exactly what was shown, pinned
    assert f.ce.change.tier == "R1"                          # applied: the tier


@scenario()
async def test_a_rejected_sign_off_reworks_what_it_signs_with_the_reason(f: Flow):
    await f.intake()
    first = f.ticket("intake").sandbox
    await f.signoff("H-1", approve=False, reason="also say goodbye")
    t = f.ticket("intake")
    assert (t.status, t.attempt) == ("working", 2)
    assert any("also say goodbye" in x for x in f.run("intake").assignment["findings"])
    assert f.run("intake").sandbox == first                  # its own sandbox and session
    await f.intake(reqs="- REQ-1: hello\n- REQ-2: goodbye\n")
    await f.signoff("H-1")
    assert "REQ-2" in f.ce.doc("tickets/intake/requirements.md").content
    assert f.ce.tickets.root.pointers["requirements"] == f.ticket("intake").outputs["requirements"]


# ====================================================================== plan and critique
@scenario()
async def test_an_invalid_plan_is_refused_at_its_report(f: Flow):
    await f.intake()
    await f.signoff("H-1")
    bad = [dict(TASKS[0], files_modified=["../etc/passwd"])]
    out = await f.plan(tasks=bad)
    assert not out.accepted and out.violations
    assert f.status("design") == "working"


@scenario()
async def test_a_critique_rework_loop_then_its_attempts_wait_on_a_human(f: Flow):
    first = await to_critique(f)
    assert f.status("design") == "checking"                 # the plan is critiqued first
    await f.critique("REWORK")
    design = f.ticket("design")
    assert (design.status, design.attempt) == ("working", 2)
    assert any("spec too thin" in x for x in design.findings)
    # the critic's sandbox is kept, stopped, for A2A questions until a fresh one replaces it
    assert first.sandbox in f.runner.stopped and first.sandbox not in f.runner.deleted
    await f.plan()
    second = f.run("critique", "critic")
    assert second.sandbox != first.sandbox                   # a fresh judge
    assert first.sandbox in f.runner.deleted
    await f.critique("REWORK")                               # the same finding again
    assert waiting(f, "design") == ("waiting", "repeated")
    await f.human("design", "waive:critique", reason="the spec is fine as it is, for this change")
    await f.settle()
    assert f.status("critique") == "passed" and f.ticket("critique").waived
    assert f.status("H-E1") == "working"                    # on to the next criterion


@scenario(max_report_attempts=20)
async def test_the_critique_must_attest_the_latest_plan(f: Flow):
    r = await to_critique(f)
    doc = await f.write(r, "critique.md", "x")
    out = await f.report(r, [doc], {"verdict": "REWORK", "findings": []},
                         outputs={"critique": doc})
    assert {"G-C1: REWORK requires at least one finding"} <= set(out.violations)
    assert any("latest plan version" in v for v in out.violations)


# ====================================================================== build: verification
@scenario()
async def test_failed_verification_is_rework_in_the_same_sandbox(f: Flow):
    await f.to_build()
    f.runner.commands["grep -q hello src/greet.txt"] = (1, "no match\n")
    first = f.run("T1", "implementer")
    await f.implement()
    t = f.ticket("T1")
    assert (t.status, t.attempt) == ("working", 2) and "exited 1" in why(f, "T1")
    second = f.run("T1", "implementer")
    assert second.sandbox == first.sandbox and second.sandbox in f.runner.uploads   # (G-D4)
    del f.runner.commands["grep -q hello src/greet.txt"]
    await f.implement()
    await f.review()
    await f.settle()
    assert f.status("T1") == "passed"


@scenario()
async def test_a_verifier_infrastructure_failure_waits_and_a_retry_verifies_again(f: Flow):
    await f.to_build()
    f.runner.setup_rc = 1
    await f.implement()
    assert waiting(f, "verify:T1") == ("waiting", "system")
    assert "clone.sh" in why(f, "verify:T1")
    verifiers = [n for n in f.runner.created if n.startswith("lgv-")]
    assert verifiers and set(verifiers) <= set(f.runner.deleted), "verifier deleted on failure"
    assert f.ticket("T1").attempt == 1                      # never the implementer's attempt
    f.runner.setup_rc = 0
    await f.human("verify:T1", "retry")
    assert f.status("verify:T1") == "passed" and f.status("review:T1") == "working"


@scenario()
async def test_a_command_not_found_waits_without_spending_an_attempt(f: Flow):
    """G-V2: exit 127 is the verifier image's problem, not the agent's: no rework run."""
    await f.to_build()
    f.runner.commands["grep -q hello src/greet.txt"] = (127, "bash: grep: command not found\n")
    await f.implement()
    assert waiting(f, "verify:T1") == ("waiting", "system")
    assert "G-V2" in why(f, "verify:T1") and "grep -q hello src/greet.txt" in why(f, "verify:T1")
    assert f.ticket("T1").attempt == 1 and len(f.runs("T1")) == 1
    del f.runner.commands["grep -q hello src/greet.txt"]       # the image is fixed
    await f.human("verify:T1", "retry")
    assert f.status("verify:T1") == "passed" and f.ticket("T1").attempt == 1


@scenario()
async def test_at_r0_a_passing_verification_merges_without_a_review(f: Flow):
    await f.to_end(risk=signals(verification_quality=0, scope_breadth=0, novelty_uncertainty=0))
    assert f.ce.change.tier == "R0" and "review:T1" not in f.ce.st.tickets
    assert f.ce.change.status == ChangeStatus.READY


# ====================================================================== build: implementer guards
@scenario(max_report_attempts=20)
async def test_implementer_guards(f: Flow):
    await f.to_build()
    r = f.run("T1", "implementer")
    ev = await f.write(r, "evidence.md", "- `grep -q hello src/greet.txt` -> exit 0")
    # G-B3: a file outside the task's scope
    sha = f.commit(r, {"src/greet.txt": "hello\n", "README.md": "hacked\n"})
    out = await f.report(r, [f"git:{REPO}@{sha}", ev], outputs={"evidence": ev})
    assert any("README.md is outside the allowed paths" in v for v in out.violations)
    # G-B2: the commit is not the tip of the run's branch
    sha = f.commit(r, {"src/greet.txt": "hello\n"}, push=False)
    out = await f.report(r, [f"git:{REPO}@{sha}", ev], outputs={"evidence": ev})
    assert any(v.startswith("G-R") or v.startswith("G-B2") for v in out.violations), out
    # G-B4: the evidence must list the verification commands
    sha = f.commit(r, {"src/greet.txt": "hello\n"})
    ev2 = await f.write(r, "evidence.md", "- `make test` -> exit 0")
    out = await f.report(r, [f"git:{REPO}@{sha}", ev2], outputs={"evidence": ev2})
    assert any(v.startswith("G-B4") for v in out.violations)
    assert f.status("T1") == "working"


@scenario()
async def test_agent_writes_are_scoped_to_their_ticket(f: Flow):
    r = f.run("intake")
    with pytest.raises(WorkflowError, match="G-W1"):
        await f.ce.agent_write(r, "tickets/design/spec.md", "not mine", None)
    with pytest.raises(WorkflowError, match="G-W1"):
        await f.ce.agent_write(r, "control/change.json", "{}", None)


@scenario()
async def test_a_builds_context_is_its_inputs_and_its_own_picks(f: Flow):
    """A build is given the approved versions of what its ticket consumes (``inputs``) and the
    documents its work names – not everything the change has."""
    await f.intake()
    await f.signoff("H-1")
    await f.plan()
    spec_v1 = f.ticket("design").outputs["spec"]
    await f.critique("REWORK")                          # the plan is rewritten: spec.md v2
    await f.plan(tasks=[dict(TASKS[0], context=[spec_v1])])
    await f.critique("PASS")
    await f.signoff("H-E1")
    ctx = f.run("T1", "implementer").context_refs
    ptr = f.ce.tickets.root.pointers
    assert ptr["spec"] in ctx and ptr["spec"] != spec_v1
    assert spec_v1 not in ctx                           # the approved version is given once
    assert all(s in ctx for s in ptr["specs"])
    assert not any("risk.json" in a for a in ctx)
    await f.implement("T1")
    review = f.run("review:T1", "reviewer").context_refs
    assert f.ticket("T1").outputs["evidence"] in review   # the work it judges


# ====================================================================== build: judges
@scenario()
async def test_a_review_fail_is_rework_and_the_same_findings_twice_wait_on_a_human(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    t = f.ticket("T1")
    assert (t.status, t.attempt) == ("working", 2)
    assert any("missing newline handling" in x for x in t.findings)
    await f.implement()
    await f.review(verdict="FAIL")                      # the same HIGH finding again
    assert waiting(f, "T1") == ("waiting", "repeated")
    await f.human("T1", "waive:review:T1", reason="a false positive: verified by hand, twice")
    await f.settle()
    assert f.status("review:T1") == "passed" and f.status("T1") == "passed"


@scenario(max_report_attempts=20)
async def test_review_guards(f: Flow):
    await f.to_build()
    await f.implement()
    r = f.run("review:T1", "reviewer")
    cand = f"git:{REPO}@{r.judged_sha}"
    doc = await f.write(r, "review.md", "x")
    high = {"severity": "BLOCKER", "confidence": "HIGH", "title": "t", "fix": "f",
            "attestations": [cand]}
    spec = f.ce.tickets.root.pointers["spec"]
    cases = [({"verdict": "PASS", "findings": [high], "checked": ["d"]}, "G-RV3"),
             ({"verdict": "FAIL", "findings": [], "checked": ["d"]}, "G-RV3"),
             ({"verdict": "PASS", "findings": [], "checked": []}, "G-RV4"),
             ({"verdict": "FAIL", "checked": ["d"],          # evidence not in the report's list
               "findings": [high | {"attestations": [spec]}]}, "G-RV2"),
             ({"verdict": "FAIL", "checked": ["d"], "findings": [high | {"attestations": []}]},
              "G-R7")]
    for data, gid in cases:
        out = await f.report(r, [cand, doc], data, outputs={"review": doc})
        assert any(v.startswith(gid) for v in out.violations), (data, out)


@scenario()
async def test_a_verification_failure_cannot_be_waived(f: Flow):
    await f.to_build()
    f.runner.commands["grep -q hello src/greet.txt"] = (1, "no match\n")
    while f.status("T1") == "working":
        await f.implement()
    assert waiting(f, "T1") == ("waiting", "repeated")
    with pytest.raises(WorkflowError):
        await f.human("T1", "waive:verify:T1", reason="it works on my machine, honestly it does")


@scenario()
async def test_abandoning_at_an_escalation_ends_the_change(f: Flow):
    r = f.run("intake")
    await f.report(r, status="BLOCKED", blocked_reason="x", missing_decision="y")
    await f.change_action("abandon", reason="not needed")
    await f.settle()
    assert f.ce.change.status == ChangeStatus.ABANDONED
    assert f.status("intake") == "cancelled"


# ====================================================================== merge
@scenario()
async def test_a_merge_conflict_is_rework_onto_the_new_head(f: Flow):
    await f.to_build()
    await f.implement()
    from legion_orch.git.branches import MergeCheck
    branches = f.ce._flow.runner.merger.branches
    real = branches.merge
    calls = []

    async def conflict(sha, into, message):
        calls.append(sha)
        if len(calls) == 1:                       # someone merged first: it conflicts now
            return MergeCheck(False, await branches.head(into) or "", ("src/greet.txt",))
        return await real(sha, into, message)
    branches.merge = conflict
    await f.review()
    t = f.ticket("T1")
    assert (t.status, t.attempt) == ("working", 2), f.status()
    assert "src/greet.txt" in why(f, "T1")
    await f.implement()
    await f.review()
    await f.settle()
    assert f.status("T1") == "passed" and len(calls) == 2


# ====================================================================== run lifecycle
@scenario()
async def test_an_agent_that_exits_before_connecting_is_a_system_error(f: Flow):
    r = f.run("intake")
    with open(f.ce.run_dir(r.id) / "agent.log", "a") as fh:
        fh.write("fatal: unable to access 'http://host/git/app.git/': 403\n")
    await f.exit_agent(r, rc=128)
    assert (r.failure.source, r.failure.stage, r.failure.exit_code) == (
        FailureSource.SYSTEM, RunStage.LAUNCHING, 128)
    assert "before connecting" in r.failure.reason and "403" in r.failure.reason
    assert r.stage == RunStage.EXITED
    assert f.ce.latest(f"control/runs/{r.id}/log")
    await f.settle()
    for _ in range(f.cfg.limits.system_retries - 1):          # retried, then a human's
        await f.exit_agent(f.run("intake"), rc=128)
        await f.settle()
    assert waiting(f, "intake") == ("waiting", "system")
    assert len(f.runs("intake")) == f.cfg.limits.system_retries
    assert f.ticket("intake").attempt == 1 and "before connecting" in why(f, "intake")


@scenario()
async def test_an_agent_that_exits_after_connecting_says_so(f: Flow):
    r = f.run("intake")
    f.ce.connected(r)
    await f.exit_agent(r, rc=137)
    assert r.failure.source == FailureSource.SYSTEM and r.failure.stage == RunStage.RUNNING
    assert "exited with code 137 after connecting" in r.failure.reason


@scenario()
async def test_an_agent_ending_with_an_error_is_its_attempt(f: Flow):
    r = f.run("intake")
    with open(f.ce.run_dir(r.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init", "model": "m", "tools": []})
                 + "\n" + json.dumps({"type": "result", "subtype": "success", "is_error": True,
                                      "result": "API Error: 401 invalid OAuth token"}) + "\n")
    await f.exit_agent(r, rc=1)
    assert r.failure.source == FailureSource.AGENT and "API Error: 401" in r.failure.reason
    await f.settle()
    assert f.ticket("intake").attempt == 2


@scenario()
async def test_a_turn_without_a_report_is_asked_again_then_the_agents_failure(f: Flow):
    """A turn that ends with the run's task open is answered by the orchestrator – another turn
    of the same run, in the agent's session – within the report attempts."""
    r = f.run("intake")
    limit = f.cfg.limits.max_report_attempts
    for turn in range(1, limit + 2):
        with open(f.ce.run_dir(r.id) / "agent.log", "a") as fh:
            fh.write(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                                 "result": "All done!"}) + "\n")
        await f.exit_agent(r, rc=0)
        if r.failure:
            break
        assert len(r.asked) == turn and "no accepted report" in r.asked[-1]
        assert r.id in f.ctl.svc.procs                       # its next turn runs
    assert len(r.asked) == limit and r.failure.source == FailureSource.AGENT
    assert "not ended" in r.failure.reason


@scenario(run_timeout_s=1)
async def test_a_run_that_times_out_exceeds_its_budget(f: Flow):
    r = f.run("intake")
    r.created_at = "2020-01-01T00:00:00.000Z"
    f.ce.notify()                                   # its timer reads the state again
    await f.settle()
    assert r.status == R_FAILED and r.failure.source == FailureSource.TIMEOUT
    assert waiting(f, "intake") == ("waiting", "budget")


@scenario()
async def test_a_sandbox_launch_failure_is_a_system_error_while_preparing(f: Flow):
    await f.intake()
    f.runner.fail_create = "planner"
    await f.signoff("H-1")
    run = f.runs("design")[0]
    assert run.status == R_FAILED and run.failure.source == FailureSource.SYSTEM
    assert run.failure.stage == RunStage.PREPARING
    assert waiting(f, "design") == ("waiting", "system")
    f.runner.fail_create = None
    await f.human("design", "retry")
    assert f.run("design")


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
    await f.signoff("H-1")
    run = f.run("design")
    assert seen[-2:] == [("create", RunStage.PREPARING), ("start", RunStage.LAUNCHING)]
    assert run.stage == RunStage.LAUNCHING and run.status == R_STARTING   # not connected yet
    f.ce.connected(run)                         # the agent's first MCP call
    assert run.stage == RunStage.RUNNING and run.status == R_ACTIVE
    await f.plan()
    assert run.stage == RunStage.EXITED and run.failure is None


# ---------------------------------------------------------------------- sandbox names
@scenario()
async def test_sandboxes_get_readable_names_never_reused(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review()
    agents = [r.sandbox for r in f.ce.runs.values()]
    verifiers = [n for n in f.runner.created if n.startswith("lgv-")]
    assert agents and verifiers
    assert all(re.fullmatch(r"lg-[a-z]+", n) for n in agents), agents
    assert all(re.fullmatch(r"lgv-[a-z]+", n) for n in verifiers), verifiers
    assert not set(agents) & set(verifiers)
    assert set(agents) <= f.ctl.used_sandbox_names()
    other = await f.ctl.start_change("Another goal", REPO)       # unique across changes
    await f.settle()
    assert [r.sandbox for r in other.runs.values()] and not (
        {r.sandbox for r in other.runs.values()} & set(agents + verifiers))


@scenario()
async def test_a_name_taken_on_the_gateway_is_replaced(f: Flow):
    await f.intake()
    f.runner.clash = 2                     # two names in a row already exist on the gateway
    await f.signoff("H-1")
    run = f.run("design")
    assert run.sandbox in f.runner.created and len(f.runner.created) == 2   # intake + design


@scenario()
async def test_a_verifier_name_taken_on_the_gateway_is_replaced(f: Flow):
    await f.to_build()
    f.runner.clash = 1
    await f.implement()
    assert f.status("verify:T1") == "passed"


# ---------------------------------------------------------------------- retry
@scenario()
async def test_a_retry_after_a_system_error_starts_afresh(f: Flow):
    await f.intake()
    f.runner.fail_create = "planner"
    await f.signoff("H-1")
    f.runner.fail_create = None
    failed = f.runs("design")
    await f.human("design", "retry")
    r2 = f.run("design")
    assert r2.id not in {r.id for r in failed} and f.ticket("design").attempt == 1


@scenario()
async def test_a_failed_judge_is_retried_without_reworking_the_build(f: Flow):
    await f.to_build()
    await f.implement()
    rev = f.run("review:T1", "reviewer")
    for _ in range(f.cfg.limits.system_retries):           # the system's failure, each try
        r = f.run("review:T1", "reviewer")
        f.ce.connected(r)
        await f.exit_agent(r, rc=137)
        await f.settle()
    assert waiting(f, "review:T1") == ("waiting", "system")
    assert f.ticket("T1").attempt == 1
    await f.human("review:T1", "retry")
    assert f.run("review:T1", "reviewer").id != rev.id
    await f.review()
    await f.settle()
    assert f.status("T1") == "passed"


@scenario()
async def test_a_failed_sandbox_deletion_is_reported_not_fatal(f: Flow):
    f.runner.fail_delete = True
    r = f.run("intake")
    await f.intake()
    await f.signoff("H-1")
    assert f.status("intake") == "passed" and not r.sandbox_deleted


@scenario(max_parallel=1)
async def test_the_parallel_limit_queues_work(f: Flow):
    tasks = [TASKS[0], dict(TASKS[0], id="T2", files_modified=["src/bye.txt"])]
    await f.to_build(tasks=tasks)
    assert f.status("T1") == f.status("T2") == "working"   # both under way ...
    assert len([t for t in ("T1", "T2") if f.runs(t)]) == 1  # ... one sandbox at a time
    assert f.ctl.svc.dispatcher.waiting == 1


# ====================================================================== restart
@scenario()
async def test_a_restart_runs_live_work_again_at_the_same_attempt(f: Flow):
    r = f.run("intake")
    ctl2, ce2 = await f.restart(recover=True)
    f.ctl, f.ce = ctl2, ce2
    await f.settle()
    assert ce2.runs[r.id].status == R_FAILED
    assert ce2.runs[r.id].failure.source == FailureSource.CONTROLLER
    assert "controller restarted" in ce2.runs[r.id].failure.reason
    again = f.run("intake")
    assert again.id != r.id and f.ticket("intake").attempt == 1


@scenario()
async def test_a_restart_verifies_again(f: Flow):
    await f.to_build()
    gate = asyncio.Event()
    real = f.runner.exec

    async def exec_(name, argv, **kw):
        if argv[-1] == "grep -q hello src/greet.txt":
            await gate.wait()
        return await real(name, argv, **kw)
    f.runner.exec = exec_
    task = asyncio.create_task(f.implement())
    for _ in range(250):
        await asyncio.sleep(0.02)
        if f.ce.verifying:
            break
    assert f.status("verify:T1") == "working"
    await f.ctl.stop()
    task.cancel()
    f.runner.exec = real
    ctl2, ce2 = await f.restart(recover=True)
    f.ctl, f.ce = ctl2, ce2
    await f.settle()
    assert f.status("verify:T1") == "passed" and f.status("review:T1") == "working"


# ====================================================================== change-level actions
@scenario()
async def test_change_actions_validate_their_input(f: Flow):
    with pytest.raises(WorkflowError, match="unknown action"):
        await f.change_action("teleport")
    with pytest.raises(WorkflowError, match="not a decision owner"):
        await f.change_action("pause", approver="mallory")
    with pytest.raises(WorkflowError):
        await f.change_action("replan", plan={"add": "nonsense"})


def test_signals_helper_derives_r1():
    from legion_orch.workflow import risk
    assert risk.derive_tier(signals()).tier == "R1"
    assert risk.derive_tier(signals(security=3)).tier == "R3"


@scenario(blank_repo=True)
async def test_a_change_on_a_blank_repository(f: Flow):
    """No repo.path: the hub starts with one empty commit and a change runs to READY on it,
    starting with a scaffold task (§5.3d)."""
    assert f.cfg.workspaces[0].source is None
    base = f.ce.change.base_sha
    assert await f.git.tree_paths(base) == [] and f.ce.change.blank_repo
    scaffold = dict(TASKS[0], id="T0", title="scaffold", files_modified=["Makefile"],
                    verification=["make -n"], scaffold=True)
    await f.to_end(tasks=[scaffold, dict(TASKS[0], depends_on=["T0"])])
    assert f.ce.change.status == ChangeStatus.READY
    assert await f.git.blob(base, "src/greet.txt") is None                 # nothing at base
    head = (await f.git.git("rev-parse", f"refs/heads/legion/{f.ce.cid}/integration")).strip()
    assert await f.git.blob(head, "src/greet.txt")                          # merged


@scenario()
async def test_a_rework_may_leave_a_required_document_unchanged(f: Flow):
    first = await to_critique(f)
    del first
    await f.critique("REWORK")
    second = f.run("design")
    written = second.docs_written
    path = "tickets/design/oracle.json"
    att = await f.write(second, "oracle.json", f.ce.doc(path).content)
    assert att == f.ce.latest_att(path) and f.ce.latest(path) == 1
    assert second.confirmed_docs == [att] and second.docs_written == written   # no quota used
    out = await f.plan()                    # rewrites oracle.json and the tickets unchanged
    assert out.accepted, out.violations
    assert f.ce.latest("tickets/design/tickets.json") == 1
    assert f.ce.latest("tickets/design/spec.md") == 2


@scenario()
async def test_only_roles_that_push_get_a_branch(f: Flow):
    """The controller prepares each sandbox in steps: a role that pushes is put on its branch,
    every other role gets its commit detached (a judge, the commit it judges)."""
    await f.to_build()
    await f.implement()
    [impl], [rev] = f.runs("T1", "implementer"), f.runs("review:T1", "reviewer")
    t = f.ticket("T1")
    assert f.steps(impl) == [["clone.sh"], ["branch.sh", f"legion/{f.ce.cid}/t/T1",
                                            impl.base_sha]]
    assert f.steps(rev) == [["clone.sh"], ["checkout.sh", t.sha]]
    assert rev.branch is None and "(none: a detached checkout" in rev.template_vars["branch"]
    [intake] = f.runs("intake")
    assert f.steps(intake) == [["clone.sh"], ["checkout.sh", f.ce.change.base_sha]]
    log = (f.ce.run_dir(rev.id) / "agent.log").read_text()
    assert f"[legion] check out {t.sha[:12]} (detached): exit 0" in log


@scenario()
async def test_a_new_attempt_continues_its_pushed_branch(f: Flow):
    """An implementer's next attempt starts its branch at its tip on the hub (what the failed
    run pushed), not at the base."""
    await f.to_build()
    r = f.run("T1", "implementer")
    pushed = f.commit(r, {"src/greet.txt": "half done\n"})
    f.ce.connected(r)
    with open(f.ce.run_dir(r.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "result", "subtype": "success", "is_error": True,
                             "result": "gave up"}) + "\n")
    await f.exit_agent(r, rc=1)                         # the agent's failure: an attempt
    await f.settle()
    r2 = f.run("T1", "implementer")
    assert r2.id != r.id and f.ticket("T1").attempt == 2
    assert f.steps(r2)[-1] == ["branch.sh", r2.branch, pushed]


@scenario()
async def test_without_an_environment_the_tools_are_the_default_images(f: Flow):
    await f.to_build()
    tools = f.run("T1", "implementer").template_vars["sandbox_tools"]
    assert "default image" in tools and "do not assume python3, node or rsync" in tools


@scenario()
async def test_the_controllers_mcp_tools_load_up_front(f: Flow):
    """Not deferred behind a tool search (the agents' recurring first-turn detour)."""
    server = json.loads(f.runner.mcp_configs[0])["mcpServers"]["legion"]
    assert server["alwaysLoad"] is True and server["url"] == f.ce.mcp_url


# ====================================================================== the turn ends the run
async def intake_reported(f: Flow):
    run = f.run("intake")
    req = await f.write(run, "requirements.md", "# Requirements\n\n- REQ-1: hello\n")
    risk = await f.write(run, "risk.json", signals())
    out = await f.report(run, [req, risk], exit_agent=False,
                         outputs={"requirements": req, "risk": risk})
    return run, out


@scenario()
async def test_an_accepted_report_is_taken_when_the_agents_turn_ends(f: Flow):
    run, out = await intake_reported(f)
    assert out.accepted and "a2a_reply" in out.instruction
    assert run.status == "REPORTED" and not run.concluded
    assert f.status("intake") == "working" and f.status("H-1") == "pending"  # nothing moved yet
    await f.exit_agent(run)                                  # the turn ends: control returns
    assert run.concluded and f.status("intake") == "checking"
    assert f.status("H-1") == "working"


@scenario()
async def test_an_agent_that_does_not_end_its_turn_is_stopped(f: Flow):
    from legion_orch.workflow.engine.services.runs import RunLifecycle
    run, _ = await intake_reported(f)
    await f.reply(run)                                  # the task ended: the grace starts
    grace, RunLifecycle.END_TURN_GRACE_S = RunLifecycle.END_TURN_GRACE_S, 0
    f.ce.notify()                                       # its timer reads the state again
    try:
        await f.settle()
    finally:
        RunLifecycle.END_TURN_GRACE_S = grace
    assert run.concluded and f.status("intake") == "checking"
    assert "did not end its turn" in f.ce.timeline()


@scenario()
async def test_a_turn_that_ended_with_the_controller_is_concluded_after_a_restart(f: Flow):
    run, _ = await intake_reported(f)
    ctl2, ce2 = await f.restart(recover=True)
    f.ctl, f.ce = ctl2, ce2
    await f.settle()
    assert ce2.runs[run.id].concluded and f.status("intake") == "checking"


@scenario()
async def test_an_integration_that_fails_is_an_attempt_not_a_pass(f: Flow):
    await f.to_build()
    for task in [t for t, x in f.ce.st.tickets.items() if x.type == "agent:implementer"]:
        await f.implement(task)
        if f"review:{task}" in f.ce.st.tickets:
            await f.review(task)
    res = await f.integration("FAIL")
    assert res.accepted, res
    await f.settle()
    t = f.ticket("integration")
    assert (t.status, t.attempt) == ("working", 2), f.status()   # round again, told why
    assert any("never printed" in x for x in t.findings), t.findings
    assert f.status("describe") == "pending"                     # evaluate has not started
    await f.integration()
    await f.describe()
    await f.signoff("H-3")
    assert f.ce.change.status == ChangeStatus.READY
