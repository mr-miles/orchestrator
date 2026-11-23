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
        Stage(f"design-{n}", [Step(f"planner-{n}")], gate=Step(f"H-E1-{n}")),
        Stage(f"oracle-tests-{n}", [Step(f"oracle-{n}"), Step(f"preflight-{n}"),
                                    Step(f"baseline-{n}")],
              gate=Step(f"H-2-{n}"), feedback_to=f"oracle-{n}"),
        Parallel(f"tasks-{n}", [Step(f"T{n}a", overridable=True)]),
    ], feedback_to=f"planner-{n}")                         # approved with its gates


def legion(tier: str = "R3") -> Workflow:
    """The change's structure (plan §3): requirements, plan (design, oracle), delivery."""
    low = tier in ("R0", "R1")
    oracle = [] if low else [Stage("oracle-tests", [Step("provision", rerunnable=True), Step("oracle"),
                                                     Step("preflight"), Step("baseline")],
                                   gate=Step("H-2"),
                                   feedback_to="oracle")]
    tail = [] if low else [Step("integration"), Step("release")]
    return Workflow([
        Stage("requirements", [Step("intake")], gate=Step("H-1")),
        Stage("plan", [
            Stage("design", [Step("planner")],
                  gate=Step("H-E1")),
            *oracle,
        ]),                                                  # no gate of its own
        Stage("delivery", [
            Parallel("tasks", [Step("T1", overridable=True),
                               Step("T2", overridable=True, depends_on=["T1"])]),
            *tail,
        ], gate=Step("H-3", rejectable=True),
            rework=AppendRound(round_, before="integration") if tail else
            AppendRound(round_, before="tasks")),
    ])


def approves(tier: str = "R3"):
    """The gate steps a human approves (at R0/R1, H-3 approves itself once its checks hold)."""
    low = tier in ("R0", "R1")
    return lambda nid: nid.startswith("H-") and not (low and nid == "H-3")


def runtime(tier: str = "R3", **kw) -> ScriptedRuntime:
    return ScriptedRuntime(legion(tier), needs_approval=approves(tier), **kw)


def gate(nid: str) -> Waiting:
    """A gate step waiting for its approval."""
    return Waiting(nid, "awaiting_approval", f"{nid}: awaiting_approval")


def to_h2(rt: ScriptedRuntime) -> ScriptedRuntime:
    return rt.approve("H-1").approve("H-E1")


def to_h3(rt: ScriptedRuntime) -> ScriptedRuntime:
    return to_h2(rt).approve("H-2")


# ====================================================================== the happy path
def test_a_change_runs_stage_by_stage_through_its_gates():
    rt = runtime().start()
    assert rt.waiting() == [gate("H-1")]          # a gate is a step, escalated for a human
    assert rt.actions("H-1") == ["answer", "retry", "send_back:requirements", "abandon"]
    rt.approve("H-1")
    assert Record("decision", "H-1", "human", "approved") in rt.log
    assert rt.waiting() == [gate("H-E1")] and rt.status("provision") == "pending"
    rt.approve("H-E1", by="ana")
    assert rt.waiting() == [gate("H-2")]
    rt.approve("H-2")
    assert rt.status("plan") == "done"                            # a stage without a gate
    assert [s for s, _ in rt.started][-5:] == ["T1", "T2", "integration", "release", "H-3"]
    assert rt.waiting() == [gate("H-3")]
    rt.approve("H-3")
    assert rt.ended == "completed" and rt.actions(CHANGE) == []


def test_tasks_run_as_their_dependencies_allow():
    rt = runtime(hold={"T1"}).start()
    to_h3(rt)
    assert rt.status("T1") == "running" and rt.status("T2") == "pending"
    rt.finish("T1")
    assert rt.status("T2") == "done" and rt.status("integration") == "done"


# ====================================================================== escalations
def test_an_escalated_step_offers_every_enclosing_stage_to_send_back():
    rt = runtime(steps={"preflight": ["escalate:verify_unrunnable"]}).start()
    to_h2(rt)
    assert rt.waiting() == [Waiting("preflight", "verify_unrunnable", "preflight: verify_unrunnable")]
    # what its work was built on, nearest first: its stage, the design before it, the plan, …
    assert rt.actions("preflight") == ["answer", "retry", "rerun:provision", "send_back:oracle-tests",
                                       "send_back:design", "send_back:plan",
                                       "send_back:requirements", "abandon"]
    assert rt.actions("H-2") == [] and rt.status("H-2") == "pending"   # not reached


def test_answer_resumes_the_step_with_the_decision():
    rt = runtime(steps={"T1": ["escalate:blocked", "ok"]}).start()
    to_h3(rt)
    with pytest.raises(ActionError, match="answer is required"):
        rt.act("T1", "answer", text=" ")
    rt.act("T1", "answer", text="use the CLI flag")
    assert ResumeStep("T1", "use the CLI flag") in rt.log
    assert Record("decision", "T1", "human", "use the CLI flag") in rt.log
    assert rt.status("T1") == "done" and rt.attempt("T1") == 1


def test_retry_runs_the_step_again_in_the_same_attempt():
    rt = runtime(steps={"intake": ["escalate:run_failed", "ok"]}).start()
    rt.act("intake", "retry")
    assert RetryStep("intake") in rt.log and rt.attempt("intake") == 1
    assert rt.waiting() == [gate("H-1")]


def test_an_escalation_answered_by_running_an_earlier_step_again_in_place():
    """Provisioning built again: it runs a new attempt while what was built on it stands (no
    reset, no stage reopened), and the escalated step is retried."""
    rt = runtime(steps={"T1": ["escalate:blocked", "ok"]},
                 hold={"provision"}).start()
    to_h2(rt)
    rt.finish("provision")
    rt.approve("H-2")
    assert rt.status("T1") == "escalated"
    assert "rerun:provision" in rt.actions("T1")
    assert rt.actions("H-2") == []                                  # approved: done
    rt.hold.add("provision")
    rt.act("T1", "rerun:provision", text="the uv library entry was fixed")
    assert StartStep("provision", 2, ("the uv library entry was fixed",), rerun=True) in rt.log
    assert RetryStep("T1") in rt.log and Record("rerun", "provision", "human",
                                                "the uv library entry was fixed") in rt.log
    assert rt.status("provision") == "running" and rt.status("oracle") == "done"
    assert rt.status("plan") == "done" and rt.flow.violations() == []
    rt.finish("provision")
    assert rt.status("provision") == "done" and rt.status("T1") == "done"
    assert rt.flow.violations() == []


def test_override_accepts_a_build_and_its_dependants_go_on():
    rt = runtime(steps={"T1": ["escalate:stale_loop"]}).start()
    to_h3(rt)
    assert "override" in rt.actions("T1") and "override" not in rt.actions("H-1")
    rt.act("T1", "override", text="reviewed by hand against the incident log")
    assert rt.status("T1") == "done" and rt.status("T2") == "done"
    assert any(isinstance(e, Record) and e.kind == "override" for e in rt.log)


# ====================================================================== sending back
def test_sending_design_back_replans_and_resets_what_was_built_on_it():
    rt = runtime().start()
    to_h2(rt)
    assert rt.actions("H-2") == ["answer", "retry", "rerun:provision", "send_back:oracle-tests",
                                 "send_back:design", "send_back:plan",
                                 "send_back:requirements", "abandon"]
    rt.act("H-2", "send_back:plan", text="smaller tasks")
    assert rt.attempt("planner") == 2 and rt.starts_of("planner")[-1].feedback == ("smaller tasks",)
    assert rt.status("oracle") == "pending" and rt.status("H-E1") == "escalated"
    assert rt.attempt("H-E1") == 2                                   # asked again


def test_sending_the_oracle_back_reruns_the_tests_not_the_plan():
    rt = runtime().start()
    to_h2(rt)
    rt.act("H-2", "send_back:oracle-tests", text="the tests import src/")
    assert rt.attempt("planner") == 1                                # the plan stands
    assert rt.attempt("oracle") == 2
    assert rt.starts_of("oracle")[-1].feedback == ("the tests import src/",)   # feedback_to
    assert rt.status("H-E1") == "done" and rt.attempt("H-E1") == 1   # its approval stands
    assert rt.status("H-2") == "escalated"


def test_any_stage_the_work_was_built_on_can_be_sent_back():
    rt = runtime(steps={"planner": ["escalate:blocked"]}).start()
    rt.approve("H-1")
    assert rt.actions("planner")[2:] == ["send_back:design", "send_back:plan",
                                         "send_back:requirements", "abandon"]
    with pytest.raises(ActionError):
        rt.act("H-1", "send_back:requirements")                     # H-1 is approved: no actions
    rt.act("planner", "send_back:design", text="cover REQ-2")
    assert rt.attempt("planner") == 2 and rt.status("H-1") == "done"     # H-1 stands
    rt.act("H-E1", "send_back:requirements", text="REQ-2 is wrong")
    assert rt.attempt("intake") == 2 and rt.attempt("H-1") == 2
    assert rt.status("planner") == "pending" and rt.waiting() == [gate("H-1")]


def test_an_integration_failure_appends_a_round_and_keeps_the_merged_tasks():
    rt = runtime(steps={"integration": ["escalate:integration_failed", "ok"]})
    to_h3(rt.start())
    assert "send_back:delivery" in rt.actions("integration")
    rt.act("integration", "send_back:delivery", text="the board view breaks at 7 columns")
    assert [c.id for c in rt.flow.nodes["delivery"].children] == \
        ["tasks", "round-2", "integration", "release"]
    assert rt.attempt("T1") == rt.attempt("T2") == 1                 # merged work kept
    assert rt.starts_of("planner-2")[0].feedback == ("the board view breaks at 7 columns",)
    assert rt.waiting() == [gate("H-E1-2")]
    rt.approve("H-E1-2").approve("H-2-2")
    assert rt.attempt("integration") == 2 and rt.waiting() == [gate("H-3")]


def test_restarting_the_plan_from_a_round_removes_every_round():
    rt = runtime(steps={"integration": ["escalate:integration_failed"],
                                           "preflight-2": ["escalate:verify_unrunnable"]})
    to_h3(rt.start())
    rt.act("integration", "send_back:delivery", text="fix it")
    rt.approve("H-E1-2")
    assert rt.actions("preflight-2") == ["answer", "retry", "rerun:provision", "send_back:oracle-tests-2",
                                         "send_back:design-2", "send_back:round-2",
                                         "send_back:delivery", "send_back:plan",
                                         "send_back:requirements", "abandon"]
    rt.act("preflight-2", "send_back:plan", text="start over")
    assert "round-2" not in rt.flow.nodes and "planner-2" not in rt.flow.state
    assert [c.id for c in rt.flow.nodes["delivery"].children] == ["tasks", "integration", "release"]
    assert rt.attempt("planner") == 2 and rt.status("T1") == "pending"
    assert rt.waiting() == [gate("H-E1")]


def test_a_round_made_again_after_a_restart_continues_its_attempts():
    """Sending the plan back drops round 2; the next integration failure makes round 2 again
    with the same ids – its steps' attempts continue, so no attempt number is ever reused."""
    rt = runtime(steps={
        "integration": ["escalate:integration_failed", "escalate:integration_failed"],
        "preflight-2": ["escalate:verify_unrunnable"]})
    to_h3(rt.start())
    rt.act("integration", "send_back:delivery", text="fix it")
    rt.approve("H-E1-2")
    assert rt.attempt("oracle-2") == 1
    rt.act("preflight-2", "send_back:plan", text="start over")
    assert "oracle-2" not in rt.flow.state
    rt.approve("H-E1").approve("H-2")                     # the plan again (H-1 stands)
    rt.act("integration", "send_back:delivery", text="still broken")
    rt.approve("H-E1-2")
    assert rt.attempt("planner-2") == 2 and rt.attempt("oracle-2") == 2
    assert [s.attempt for s in rt.starts_of("oracle-2")] == [1, 2]


def test_every_status_change_is_a_declared_transition():
    rt = runtime(steps={"T1": ["escalate:blocked", "ok"]}).start()
    to_h3(rt).act("T1", "answer", text="go on").approve("H-3")
    names = {(n, t) for n, t, _, _ in rt.flow.log}
    assert {("T1", "start"), ("T1", "escalate"), ("T1", "resume"),
            ("T1", "complete"), ("H-3", "start"), ("H-3", "escalate"), ("H-3", "complete"),
            ("plan", "complete")} <= names


def test_a_send_back_stops_running_work_and_ignores_its_late_result():
    wf = Workflow([Stage("a", [Parallel("p", [Step("x"), Step("y")])], Step("g"))])
    rt = ScriptedRuntime(wf, steps={"y": ["escalate:blocked"]}, hold={"x"},
                         needs_approval=lambda nid: nid == "g").start()
    assert rt.status("x") == "running" and rt.status("y") == "escalated"
    rt.act("y", "send_back:a", text="again")
    assert StopStep("x") in rt.log and rt.attempt("x") == 2 and rt.status("x") == "running"
    rt.finish("x", attempt=1)                                    # attempt 1's late result
    assert rt.status("x") == "running"                           # ignored
    rt.finish("x")
    assert rt.status("x") == "done" and rt.waiting() == [gate("g")]


def test_abandon_stops_everything():
    rt = runtime(hold={"T1"}).start()
    to_h3(rt)
    rt.act(CHANGE, "abandon")
    assert StopStep("T1") in rt.log and rt.ended == "abandoned"
    rt.finish("T1")                                              # its late result: ignored
    assert rt.actions("T1") == [] and rt.waiting() == []


# ====================================================================== gates
def test_unmet_preconditions_keep_the_last_gate_waiting_until_rechecked():
    """H-3's checks (the ship gates) are its step's work: unmet, it escalates; a re-check is a
    retry (e.g. after an attestation)."""
    rt = runtime(steps={"H-3": ["escalate:preconditions_unmet", "escalate:preconditions_unmet"]})
    to_h3(rt.start())
    assert rt.waiting() == [Waiting("H-3", "preconditions_unmet", "H-3: preconditions_unmet")]
    rt.act("H-3", "retry")                                          # still unmet
    assert rt.ended is None and rt.waiting()[0].reason == "preconditions_unmet"
    rt.approve("H-3")                                               # met, and approved
    assert rt.ended == "completed"


def test_low_tiers_gate_automatically():
    rt = runtime("R1").start()
    rt.approve("H-1").approve("H-E1")
    assert rt.ended == "completed"                       # H-3's step passed by itself
    assert [s for s, _ in rt.started][-1] == "H-3"


def test_rejecting_the_last_gate_ends_the_change():
    rt = runtime().start()
    to_h3(rt)
    assert "reject" in rt.actions("H-3")
    rt.act("H-3", "reject", text="not what we need")
    assert rt.ended == "rejected" and EndChange("rejected") in rt.log
    assert "reject" not in runtime().start().actions("H-1")


def test_unavailable_actions_are_refused():
    rt = runtime().start()
    for target, action in (("intake", "answer"), ("H-E1", "answer"), ("H-1", "override"),
                           ("H-1", "reject"), ("nope", "answer"), ("H-1", "approve")):
        with pytest.raises(ActionError):
            rt.act(target, action, text="x")


def test_structures_are_validated():
    with pytest.raises(ValueError, match="duplicate"):
        FlowState(Workflow([Stage("a", [Step("x")], Step("g")), Stage("b", [Step("x")], Step("h"))]))
    with pytest.raises(ValueError, match="outside"):
        FlowState(Workflow([Stage("a", [Parallel("p", [Step("x", depends_on=["y"])])], Step("g"))]))


def test_a_start_carries_the_next_attempt_and_its_feedback():
    rt = runtime().start()
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
        if draw(st.booleans()):
            return None                                   # a stage without a gate
        return Step(f"g{next(n)}", rejectable=draw(st.booleans()))

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
                return Stage(f"{base}-r{r}", [Step(f"{base}-r{r}-s")], Step(f"{base}-r{r}-g"))
            stg.rework = AppendRound(template, before=children[-1].id)
        return stg

    return Workflow([stage(0) for _ in range(draw(st.integers(1, 3)))])


OUTCOMES = st.sampled_from(["ok", "ok", "ok", "escalate:blocked", "escalate:failed"])


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(_structures(), st.data())
def test_any_sequence_of_actions_keeps_the_invariants(wf, data):
    steps = [n.id for s in wf.stages for n in _steps(s)]
    rt = ScriptedRuntime(
        wf, steps={s: data.draw(st.lists(OUTCOMES, max_size=3)) for s in steps},
        needs_approval=lambda nid: nid.startswith("g"))
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

