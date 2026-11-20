"""Step types (engine/steps.py): what each kind of node is made of – its agent, judges and
verification – and what a phase's result means for it."""
from __future__ import annotations

import pytest
from engine_harness import Flow, scenario

from legion_orch.workflow.engine.phases import Escalate, Ok, Rework
from legion_orch.workflow.engine.steps import STEP_TYPES, AgentStep, step_for
from legion_orch.workflow.model import (
    ACCEPTED,
    AWAITING_HUMAN,
    BASELINE,
    BUILD,
    ESCALATED,
    HUMAN,
    INTEGRATION,
    MERGING,
    ORACLE,
    PENDING,
    PREFLIGHT,
    REVIEWING,
    REWORK,
    ROLE_FOR_KIND,
    VERIFYING,
    CommandResult,
    Feedback,
    Node,
    NodeKind,
    Oracle,
    Resume,
    Verification,
)
from legion_orch.workflow.roles import handler_for

ORACLE_DOC = Oracle(acceptance_commands=["bun test acc"], integration_commands=["bun install"])


# ---------------------------------------------------------------------------- composition
def test_every_kind_has_a_step_type_and_agent_steps_are_the_roles_kinds():
    assert set(STEP_TYPES) == set(NodeKind)
    agents = {k: s.role for k, s in STEP_TYPES.items() if isinstance(s, AgentStep)}
    assert agents == ROLE_FOR_KIND
    assert all(s.role is None for s in STEP_TYPES.values() if not isinstance(s, AgentStep))


def test_only_the_retrospective_runs_after_the_end_and_never_judges_the_head():
    assert [k for k, s in STEP_TYPES.items() if s.runs_after_end] == [NodeKind.RETROSPECTIVE]
    for kind, s in STEP_TYPES.items():
        assert s.judges_head == (s.role is not None and not s.runs_after_end
                                 and handler_for(s.role).judges_head), kind


def test_a_failed_build_run_resumes_in_rework_others_start_again():
    assert {k for k, s in STEP_TYPES.items() if s.rework_resume == Resume.REWORK} == {BUILD}


@scenario()
async def test_judges_are_the_critic_for_the_plan_and_the_panel_for_a_task(f: Flow):
    f.ce.change.panel = ["reviewer", "security"]
    judges = {k: s.judges(f.ce) for k, s in STEP_TYPES.items() if s.judges(f.ce)}
    assert judges == {NodeKind.PLAN: ("critic",), BUILD: ("reviewer", "security")}


@scenario()
async def test_each_verified_kind_runs_its_commands(f: Flow):
    f.ce.change.oracle = ORACLE_DOC
    task = node(f, BUILD)
    phases = {k: STEP_TYPES[k].verify(f.ce, task) for k in (BASELINE, PREFLIGHT, INTEGRATION)}
    assert phases[BASELINE].commands == ("bun test acc", "bun install")
    assert phases[PREFLIGHT].commands == ("bun test acc",)
    assert phases[INTEGRATION].commands[:2] == ("bun test acc", "bun install")
    assert {k for k, p in phases.items() if p.expect_failures} == {BASELINE, PREFLIGHT}
    with pytest.raises(NotImplementedError):
        STEP_TYPES[ORACLE].verify(f.ce, task)


def test_verification_is_recorded_per_task_cycle_or_per_node():
    class T:
        id = "T3"
    build = Node(id="T3", kind=BUILD, title="t", cycle=2)
    build.task = T()  # type: ignore[assignment]
    assert step_for(build).verification_record(build) == ("tasks/T3/verification.json", "2")
    for kind in (BASELINE, PREFLIGHT, INTEGRATION):
        n = Node(id=f"{kind}-node", kind=kind, title="x")
        path, key = step_for(n).verification_record(n)
        assert key == str(kind) and path.startswith("control/verification/")


# ---------------------------------------------------------------------------- results
def node(f: Flow, kind: NodeKind, **kw) -> Node:
    n = Node(**({"id": f"N-{kind}", "kind": kind, "title": "x", "status": VERIFYING,
                 "candidate_sha": "c" * 40} | kw))
    f.ce.nodes[n.id] = n
    return n


def ver(*rcs: int) -> Verification:
    return Verification(sha="c" * 40, ok=not any(rcs),
                        results=[CommandResult(command=f"cmd{i}", exit_code=rc)
                                 for i, rc in enumerate(rcs)])


@scenario()
async def test_a_failing_integration_escalates_instead_of_reworking(f: Flow):
    n = node(f, INTEGRATION)
    rework = Rework([Feedback(cycle=1, source="verification", text="x")], "verification")
    step_for(n).verified(f.ce._acceptance, n, ver(1, 0, 2), rework, "att")
    assert n.status == ESCALATED and n.escalation.kind == "integration_failed"
    assert "2 oracle command(s) failed" in n.escalation.reason and n.cycle == 1


@scenario()
async def test_a_passing_task_goes_to_its_panel_or_straight_to_merge(f: Flow):
    n = node(f, BUILD)
    f.ce.change.panel = ["reviewer"]
    step_for(n).verified(f.ce._acceptance, n, ver(0), Ok(), "att")
    assert n.status == REVIEWING
    n.status, f.ce.change.panel = VERIFYING, []
    step_for(n).verified(f.ce._acceptance, n, ver(0), Ok(), "att")
    assert n.status in (MERGING, ESCALATED)        # the merge runs in the background


@scenario()
async def test_a_task_that_fails_verification_is_reworked(f: Flow):
    n = node(f, BUILD)
    step_for(n).verified(f.ce._acceptance, n, ver(1), Rework([], "verification"), "att")
    assert (n.status, n.cycle) == (REWORK, 2)


@scenario()
async def test_a_baseline_and_a_preflight_pass_with_expected_failures(f: Flow):
    for kind in (BASELINE, PREFLIGHT):
        n = node(f, kind)
        step_for(n).verified(f.ce._acceptance, n, ver(1, 0), Ok(), "att")
        assert n.status == ACCEPTED, kind


@scenario()
async def test_a_preflight_setup_failure_goes_back_to_the_oracle(f: Flow):
    oracle = node(f, ORACLE, id=ORACLE, status=ACCEPTED)
    n = node(f, PREFLIGHT)
    res = Verification(sha="c" * 40, results=[
        CommandResult(command="mise run setup", exit_code=1, output="boom", setup=True)])
    esc = Escalate("setup_failed", "G-V3: ...", Resume.VERIFYING)
    step_for(n).verified(f.ce._acceptance, n, res, esc, "att")
    assert (oracle.status, oracle.cycle, n.status) == (REWORK, 2, PENDING)
    assert "BLOCKER (preflight)" in oracle.feedback[-1].text
    # without a cycle left, a human decides
    n.status, oracle.cycle = VERIFYING, f.cfg.limits.review_max_cycles
    step_for(n).verified(f.ce._acceptance, n, res, esc, "att")
    assert n.status == ESCALATED and n.escalation.kind == "setup_failed"


# ---------------------------------------------------------------------------- scheduling
@scenario()
async def test_a_human_gate_waits_for_a_human_and_a_preflight_verifies_the_head(f: Flow):
    gate = Node(id="H-x", kind=HUMAN, title="x")
    step_for(gate).promote(f.ce._scheduler, gate)
    assert gate.status == AWAITING_HUMAN
    f.ce.change.oracle = ORACLE_DOC
    pre = Node(id="preflight-x", kind=PREFLIGHT, title="x")
    f.ce.nodes[pre.id] = pre
    async with f.ce.lock:
        step_for(pre).promote(f.ce._scheduler, pre)
        assert (pre.status, pre.candidate_sha) == (VERIFYING, f.ce.change.integration_head)


@scenario()
async def test_the_retrospective_waits_for_the_last_reflections(f: Flow):
    retro = Node(id="retro-x", kind=NodeKind.RETROSPECTIVE, title="x")
    s = f.ce._scheduler
    assert step_for(retro).may_dispatch(s, retro)
    f.ce._sandboxes.reflector.pending.add("sb")
    assert not step_for(retro).may_dispatch(s, retro)
