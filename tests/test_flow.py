"""The workflow core with dummy steps (plan: stages-and-steps.md §4): scenarios for each behaviour
the engine has today, expressed with generic actions on a stage structure, and a property test
driving random structures with random actions while every invariant is checked after each event."""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from legion_orch.workflow.flow import (
    CHANGE,
    ActionError,
    AppendRound,
    EndChange,
    FlowState,
    Gate,
    Parallel,
    Record,
    ResumeStep,
    RetryStep,
    Stage,
    StartStep,
    Step,
    StopStep,
    Waiting,
    Workflow,
)
from legion_orch.workflow.flow.testing import ScriptedRuntime


def round_(n: int) -> Stage:
    """A fix round: design the fix, check the oracle again, build the new tasks."""
    return Stage(f"round-{n}", [
        Stage(f"design-{n}", [Step(f"planner-{n}")], gate=Gate(f"H-E1-{n}")),
        Stage(f"oracle-tests-{n}", [Step(f"oracle-{n}"), Step(f"preflight-{n}"),
                                    Step(f"baseline-{n}")],
              gate=Gate(f"H-2-{n}", preconditions=True), feedback_to=f"oracle-{n}"),
        Parallel(f"tasks-{n}", [Step(f"T{n}a", overridable=True)]),
    ], gate=Gate(f"round-{n}-approved", automatic=True), feedback_to=f"planner-{n}")


def legion(tier: str = "R3") -> Workflow:
    """The change's structure (plan §3): requirements, plan (design, oracle), delivery."""
    low = tier in ("R0", "R1")
    oracle = [] if low else [Stage("oracle-tests", [Step("provision"), Step("oracle"),
                                                     Step("preflight"), Step("baseline")],
                                   gate=Gate("H-2", subjects=["oracle"], preconditions=True),
                                   feedback_to="oracle")]
    tail = [] if low else [Step("integration"), Step("release")]
    return Workflow([
        Stage("requirements", [Step("intake")], gate=Gate("H-1")),
        Stage("plan", [
            Stage("design", [Step("planner")],
                  gate=Gate("H-E1", subjects=["plan", "REQ-1", "environment"])),
            *oracle,
        ], gate=Gate("plan-approved", automatic=True)),
        Stage("delivery", [
            Parallel("tasks", [Step("T1", overridable=True),
                               Step("T2", overridable=True, depends_on=["T1"])]),
            *tail,
        ], gate=Gate("H-3", preconditions=True, rejectable=True, automatic=low),
            rework=AppendRound(round_, before="integration") if tail else
            AppendRound(round_, before="tasks")),
    ])


def to_h2(rt: ScriptedRuntime) -> ScriptedRuntime:
    rt.act("H-1", "approve").act("H-E1", "approve")
    return rt


def to_h3(rt: ScriptedRuntime) -> ScriptedRuntime:
    return to_h2(rt).act("H-2", "approve")


# ====================================================================== the happy path
def test_a_change_runs_stage_by_stage_through_its_gates():
    rt = ScriptedRuntime(legion()).start()
    assert rt.waiting() == [Waiting("H-1", "approval")]
    assert rt.actions("H-1") == ["approve", "send_back:requirements", "abandon"]
    rt.act("H-1", "approve")
    assert rt.waiting() == [Waiting("H-E1", "approval")]
    rt.act("H-E1", "approve", subjects=["plan", "REQ-1"])         # one subject still open
    assert rt.waiting() == [Waiting("H-E1", "approval")] and rt.status("provision") == "pending"
    rt.act("H-E1", "approve", subjects=["environment"], by="ana")
    assert rt.waiting() == [Waiting("H-2", "approval")]           # preconditions met
    rt.act("H-2", "approve")
    assert Record("approval", "plan-approved", "controller", "automatic") in rt.log   # outer gate
    assert [s for s, _ in rt.started][-4:] == ["T1", "T2", "integration", "release"]
    assert rt.waiting() == [Waiting("H-3", "approval")]
    rt.act("H-3", "approve")
    assert rt.ended == "completed" and rt.actions(CHANGE) == []


def test_tasks_run_as_their_dependencies_allow():
    rt = ScriptedRuntime(legion(), hold={"T1"}).start()
    to_h3(rt)
    assert rt.status("T1") == "running" and rt.status("T2") == "pending"
    rt.finish("T1")
    assert rt.status("T2") == "done" and rt.status("integration") == "done"


def test_a_steps_own_rework_loop_stays_inside_it():
    rt = ScriptedRuntime(legion(), steps={"T1": ["rework", "rework", "ok"]}).start()
    to_h3(rt)
    assert rt.attempt("T1") == 1 and rt.flow.step("T1").cycles == 2 and rt.status("T1") == "done"


# ====================================================================== escalations
def test_an_escalated_step_offers_every_enclosing_stage_to_send_back():
    rt = ScriptedRuntime(legion(), steps={"preflight": ["escalate:verify_unrunnable"]}).start()
    to_h2(rt)
    assert rt.waiting() == [Waiting("preflight", "verify_unrunnable", "preflight: verify_unrunnable")]
    # what its work was built on, nearest first: its stage, the design before it, the plan, …
    assert rt.actions("preflight") == ["answer", "retry", "send_back:oracle-tests",
                                       "send_back:design", "send_back:plan",
                                       "send_back:requirements", "abandon"]
    assert rt.actions("H-2") == []                                  # not reached


def test_answer_resumes_the_step_with_the_decision():
    rt = ScriptedRuntime(legion(), steps={"T1": ["escalate:blocked", "ok"]}).start()
    to_h3(rt)
    with pytest.raises(ActionError, match="answer is required"):
        rt.act("T1", "answer", text=" ")
    rt.act("T1", "answer", text="use the CLI flag")
    assert ResumeStep("T1", "use the CLI flag") in rt.log
    assert Record("decision", "T1", "human", "use the CLI flag") in rt.log
    assert rt.status("T1") == "done" and rt.attempt("T1") == 1


def test_retry_runs_the_step_again_in_the_same_attempt():
    rt = ScriptedRuntime(legion(), steps={"intake": ["escalate:run_failed", "ok"]}).start()
    rt.act("intake", "retry")
    assert RetryStep("intake") in rt.log and rt.attempt("intake") == 1
    assert rt.waiting() == [Waiting("H-1", "approval")]


def test_override_accepts_a_build_and_its_dependants_go_on():
    rt = ScriptedRuntime(legion(), steps={"T1": ["escalate:stale_loop"]}).start()
    to_h3(rt)
    assert "override" in rt.actions("T1") and "override" not in rt.actions("H-3")
    rt.act("T1", "override", text="reviewed by hand against the incident log")
    assert rt.status("T1") == "done" and rt.status("T2") == "done"
    assert any(isinstance(e, Record) and e.kind == "override" for e in rt.log)


# ====================================================================== sending back
def test_sending_design_back_replans_and_resets_what_was_built_on_it():
    rt = ScriptedRuntime(legion()).start()
    to_h2(rt)
    assert rt.actions("H-2") == ["approve", "evaluate", "send_back:oracle-tests",
                                 "send_back:design", "send_back:plan",
                                 "send_back:requirements", "abandon"]
    rt.act("H-2", "send_back:plan", text="smaller tasks")
    assert rt.attempt("planner") == 2 and rt.starts_of("planner")[-1].feedback == ("smaller tasks",)
    assert rt.status("oracle") == "pending" and rt.flow.gate("H-E1").approvals == {}
    assert rt.waiting() == [Waiting("H-E1", "approval")]


def test_sending_the_oracle_back_reruns_the_tests_not_the_plan():
    rt = ScriptedRuntime(legion()).start()
    to_h2(rt)
    rt.act("H-2", "send_back:oracle-tests", text="the tests import src/")
    assert rt.attempt("planner") == 1                                # the plan stands
    assert rt.attempt("oracle") == 2
    assert rt.starts_of("oracle")[-1].feedback == ("the tests import src/",)   # feedback_to
    assert rt.flow.gate("H-E1").approvals                           # its approvals stand
    assert rt.waiting() == [Waiting("H-2", "approval")]


def test_any_stage_the_work_was_built_on_can_be_sent_back():
    rt = ScriptedRuntime(legion(), steps={"planner": ["escalate:blocked"]}).start()
    rt.act("H-1", "approve")
    assert rt.actions("planner")[2:] == ["send_back:design", "send_back:plan",
                                         "send_back:requirements", "abandon"]
    with pytest.raises(ActionError):
        rt.act("H-1", "send_back:requirements")                     # H-1 is approved: no actions
    rt.act("planner", "send_back:design", text="cover REQ-2")
    assert rt.attempt("planner") == 2 and rt.flow.gate("H-1").approvals   # H-1 stands
    rt.act("H-E1", "send_back:requirements", text="REQ-2 is wrong")
    assert rt.attempt("intake") == 2 and rt.flow.gate("H-1").approvals == {}
    assert rt.status("planner") == "pending" and rt.waiting() == [Waiting("H-1", "approval")]


def test_an_integration_failure_appends_a_round_and_keeps_the_merged_tasks():
    rt = ScriptedRuntime(legion(), steps={"integration": ["escalate:integration_failed", "ok"]})
    to_h3(rt.start())
    assert "send_back:delivery" in rt.actions("integration")
    rt.act("integration", "send_back:delivery", text="the board view breaks at 7 columns")
    assert [c.id for c in rt.flow.nodes["delivery"].children] == \
        ["tasks", "round-2", "integration", "release"]
    assert rt.attempt("T1") == rt.attempt("T2") == 1                 # merged work kept
    assert rt.starts_of("planner-2")[0].feedback == ("the board view breaks at 7 columns",)
    assert rt.waiting() == [Waiting("H-E1-2", "approval")]
    rt.act("H-E1-2", "approve").act("H-2-2", "approve")
    assert rt.attempt("integration") == 2 and rt.waiting() == [Waiting("H-3", "approval")]


def test_restarting_the_plan_from_a_round_removes_every_round():
    rt = ScriptedRuntime(legion(), steps={"integration": ["escalate:integration_failed"],
                                           "preflight-2": ["escalate:verify_unrunnable"]})
    to_h3(rt.start())
    rt.act("integration", "send_back:delivery", text="fix it")
    rt.act("H-E1-2", "approve")
    assert rt.actions("preflight-2") == ["answer", "retry", "send_back:oracle-tests-2",
                                         "send_back:design-2", "send_back:round-2",
                                         "send_back:delivery", "send_back:plan",
                                         "send_back:requirements", "abandon"]
    rt.act("preflight-2", "send_back:plan", text="start over")
    assert "round-2" not in rt.flow.nodes and "planner-2" not in rt.flow.state
    assert [c.id for c in rt.flow.nodes["delivery"].children] == ["tasks", "integration", "release"]
    assert rt.attempt("planner") == 2 and rt.status("T1") == "pending"
    assert rt.waiting() == [Waiting("H-E1", "approval")]


def test_a_round_made_again_after_a_restart_continues_its_attempts():
    """Sending the plan back drops round 2; the next integration failure makes round 2 again
    with the same ids – its steps' attempts continue, so no attempt number is ever reused."""
    rt = ScriptedRuntime(legion(), steps={
        "integration": ["escalate:integration_failed", "escalate:integration_failed"],
        "preflight-2": ["escalate:verify_unrunnable"]})
    to_h3(rt.start())
    rt.act("integration", "send_back:delivery", text="fix it")
    rt.act("H-E1-2", "approve")
    assert rt.attempt("oracle-2") == 1
    rt.act("preflight-2", "send_back:plan", text="start over")
    assert "oracle-2" not in rt.flow.state
    rt.act("H-E1", "approve").act("H-2", "approve")       # the plan again (H-1 stands)
    rt.act("integration", "send_back:delivery", text="still broken")
    rt.act("H-E1-2", "approve")
    assert rt.attempt("planner-2") == 2 and rt.attempt("oracle-2") == 2
    assert [s.attempt for s in rt.starts_of("oracle-2")] == [1, 2]


def test_every_status_change_is_a_declared_transition():
    rt = ScriptedRuntime(legion(), steps={"T1": ["rework", "escalate:blocked", "ok"]}).start()
    to_h3(rt).act("T1", "answer", text="go on").act("H-3", "approve")
    names = {(n, t) for n, t, _, _ in rt.flow.log}
    assert {("T1", "start"), ("T1", "rework"), ("T1", "escalate"), ("T1", "resume"),
            ("T1", "complete"), ("H-3", "open"), ("H-3", "complete"),
            ("plan-approved", "complete")} <= names


def test_a_send_back_stops_running_work_and_ignores_its_late_result():
    wf = Workflow([Stage("a", [Parallel("p", [Step("x"), Step("y")])], Gate("g"))])
    rt = ScriptedRuntime(wf, steps={"y": ["escalate:blocked"]}, hold={"x"}).start()
    assert rt.status("x") == "running" and rt.status("y") == "escalated"
    rt.act("y", "send_back:a", text="again")
    assert StopStep("x") in rt.log and rt.attempt("x") == 2 and rt.status("x") == "running"
    rt.finish("x", attempt=1)                                    # attempt 1's late result
    assert rt.status("x") == "running"                           # ignored
    rt.finish("x")
    assert rt.status("x") == "done" and rt.waiting() == [Waiting("g", "approval")]


def test_abandon_stops_everything():
    rt = ScriptedRuntime(legion(), hold={"T1"}).start()
    to_h3(rt)
    rt.act(CHANGE, "abandon")
    assert StopStep("T1") in rt.log and rt.ended == "abandoned"
    rt.finish("T1")                                              # its late result: ignored
    assert rt.actions("T1") == [] and rt.waiting() == []


# ====================================================================== gates
def test_unmet_preconditions_keep_the_last_gate_open():
    rt = ScriptedRuntime(legion(), preconditions={
        "H-3": [("rollback_or_forward_fix_evidence",), ("rollback_or_forward_fix_evidence",), ()]})
    to_h3(rt.start())
    assert rt.waiting() == [Waiting("H-3", "preconditions", "rollback_or_forward_fix_evidence")]
    rt.act("H-3", "approve")                                         # approved, still unmet
    assert rt.ended is None and rt.waiting()[0].reason == "preconditions"
    rt.act("H-3", "evaluate")                                        # e.g. after an attestation
    assert rt.ended == "completed"


def test_low_tiers_gate_automatically():
    rt = ScriptedRuntime(legion("R1")).start()
    rt.act("H-1", "approve").act("H-E1", "approve")
    assert rt.ended == "completed"
    assert Record("approval", "H-3", "controller", "automatic") in rt.log


def test_rejecting_the_last_gate_ends_the_change():
    rt = ScriptedRuntime(legion()).start()
    to_h3(rt)
    rt.act("H-3", "reject", text="not what we need")
    assert rt.ended == "rejected" and EndChange("rejected") in rt.log
    assert "reject" not in ScriptedRuntime(legion()).start().actions("H-1")


def test_unavailable_actions_are_refused():
    rt = ScriptedRuntime(legion()).start()
    for target, action in (("intake", "answer"), ("H-E1", "approve"), ("H-1", "override"),
                           ("H-1", "reject"), ("nope", "approve")):
        with pytest.raises(ActionError):
            rt.act(target, action, text="x")
    with pytest.raises(ActionError, match="unknown subject"):
        rt.act("H-1", "approve", subjects=["other"])


def test_structures_are_validated():
    with pytest.raises(ValueError, match="duplicate"):
        FlowState(Workflow([Stage("a", [Step("x")], Gate("g")), Stage("b", [Step("x")], Gate("h"))]))
    with pytest.raises(ValueError, match="outside"):
        FlowState(Workflow([Stage("a", [Parallel("p", [Step("x", depends_on=["y"])])], Gate("g"))]))


def test_a_start_carries_the_next_attempt_and_its_feedback():
    rt = ScriptedRuntime(legion()).start()
    assert rt.log[0] == StartStep("intake", 1)


# ====================================================================== properties
@st.composite
def _structures(draw):
    """Random workflows: up to three stages, each possibly holding one level of smaller stages,
    of steps and parallel groups, with random gates; some stages append rounds when sent back.
    Ids are numbered within each workflow (deterministic for hypothesis)."""
    n = iter(range(10_000))

    def step():
        return Step(f"s{next(n)}", overridable=draw(st.booleans()))

    def gate():
        return Gate(f"g{next(n)}", automatic=draw(st.booleans()),
                    preconditions=draw(st.booleans()), rejectable=draw(st.booleans()))

    def par():
        return Parallel(f"p{next(n)}", [step() for _ in range(draw(st.integers(1, 3)))])

    def stage(depth: int) -> Stage:
        kinds = ["step", "par"] + (["stage"] if depth == 0 else [])
        children = []
        for _ in range(draw(st.integers(1, 3))):
            k = draw(st.sampled_from(kinds))
            children.append(step() if k == "step" else par() if k == "par" else stage(1))
        stg = Stage(f"st{next(n)}", children, gate())
        if depth == 0 and draw(st.booleans()):
            def template(r, base=stg.id):
                return Stage(f"{base}-r{r}", [Step(f"{base}-r{r}-s")], Gate(f"{base}-r{r}-g"))
            stg.rework = AppendRound(template, before=children[-1].id)
        return stg

    return Workflow([stage(0) for _ in range(draw(st.integers(1, 3)))])


OUTCOMES = st.sampled_from(["ok", "ok", "ok", "rework", "escalate:blocked", "escalate:failed"])


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_structures(), st.data())
def test_any_sequence_of_actions_keeps_the_invariants(wf, data):
    steps = [n.id for s in wf.stages for n in _steps(s)]
    gates = [n.id for s in wf.stages for n in _gates(s)]
    rt = ScriptedRuntime(
        wf, steps={s: data.draw(st.lists(OUTCOMES, max_size=3)) for s in steps},
        preconditions={g: data.draw(st.lists(st.sampled_from([(), ("unmet",)]), max_size=2))
                       for g in gates})
    rt.start()
    for _ in range(40):
        if rt.ended:
            break
        offered = [(w.node, a) for w in rt.waiting() for a in rt.actions(w.node)]
        if not offered:
            break
        node, action = data.draw(st.sampled_from(offered))
        rt.act(node, action, text="because")          # ScriptedRuntime checks every invariant
    if rt.ended:
        assert rt.actions(CHANGE) == [] and rt.waiting() == []
        assert rt.ended in ("abandoned", "rejected", "completed")


def _steps(node):
    from legion_orch.workflow.flow import walk
    return [n for n in walk(node) if isinstance(n, Step)]


def _gates(node):
    from legion_orch.workflow.flow import walk
    return [n for n in walk(node) if isinstance(n, Gate)]
