"""Step types (engine/steps.py): each kind of node as a composition – an agent, a sandbox plan,
phases configured by policies, waits – and the StepRunner that moves a node through its phases."""
from __future__ import annotations

from engine_harness import Flow, scenario

from legion_orch.workflow.engine.sandboxes.plan import JUDGE
from legion_orch.workflow.engine.steps.phases import Ok, Panel, Verify
from legion_orch.workflow.engine.steps.step_types import (
    STEP_TYPES,
    EnvironmentQuiescent,
    NoOverlap,
    ReflectionsDone,
    sandbox_plan,
    step_for,
)
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
    PLAN,
    PREFLIGHT,
    PROVISION,
    READY,
    REVIEWING,
    REWORK,
    ROLE_FOR_KIND,
    RUNNING,
    VERIFYING,
    CommandResult,
    Node,
    NodeKind,
    Oracle,
    Resume,
    Verification,
)

ORACLE_DOC = Oracle(acceptance_commands=["bun test acc"], integration_commands=["bun install"])
CAND = "c" * 40


# ---------------------------------------------------------------------------- the declarations
def shape(kind: NodeKind) -> list[str]:
    return [type(p).__name__ for p in STEP_TYPES[kind].phases]


def test_every_kind_is_a_list_of_phases():
    assert set(STEP_TYPES) == set(NodeKind)
    assert {k: shape(k) for k in STEP_TYPES} == {
        NodeKind.INTAKE: ["Produce"],
        PLAN: ["Produce", "Panel"],
        PROVISION: ["BuildImages"],
        ORACLE: ["Produce"],
        PREFLIGHT: ["Verify"],
        BASELINE: ["Produce", "Verify"],
        BUILD: ["Produce", "Verify", "Panel", "Merge"],
        INTEGRATION: ["Produce", "Verify"],
        NodeKind.RELEASE: ["Produce"],
        NodeKind.RETROSPECTIVE: ["Produce"],
        HUMAN: ["Approval"],
    }


def test_agent_steps_are_the_roles_kinds_and_only_they_have_a_sandbox_plan():
    assert {k: s.role for k, s in STEP_TYPES.items() if s.role} == ROLE_FOR_KIND
    assert all((s.role is None) == (s.sandbox is None) for s in STEP_TYPES.values())
    for s in STEP_TYPES.values():
        if s.role:
            assert sandbox_plan(s.role) is s.sandbox
    assert sandbox_plan("reviewer") is JUDGE and sandbox_plan("critic") is JUDGE


def test_verifications_differ_only_in_their_policies():
    verify = {k: s.phase(Verify) for k, s in STEP_TYPES.items() if s.phase(Verify)}
    assert set(verify) == {PREFLIGHT, BASELINE, BUILD, INTEGRATION}
    assert {k for k, v in verify.items() if v.expect_failures} == {PREFLIGHT, BASELINE}
    assert {k for k, v in verify.items() if v.at_head} == {PREFLIGHT}
    assert {k for k, v in verify.items() if v.on_rework} == {INTEGRATION}
    assert {k for k, v in verify.items() if v.on_setup_failed} == {PREFLIGHT}
    assert {k for k, v in verify.items() if v.notes} == {PREFLIGHT, BASELINE}


def test_waits_and_resume_points():
    assert {k: type(s.dispatch_when[0]) for k, s in STEP_TYPES.items() if s.dispatch_when} == {
        BUILD: NoOverlap, NodeKind.RETROSPECTIVE: ReflectionsDone,
        PROVISION: EnvironmentQuiescent}
    assert [k for k, s in STEP_TYPES.items() if s.runs_after_end] == [NodeKind.RETROSPECTIVE]
    assert {k for k, s in STEP_TYPES.items() if s.rework_resume == Resume.REWORK} == {BUILD}
    assert {k for k, s in STEP_TYPES.items() if s.judges_head} == {BASELINE, INTEGRATION,
                                                                    NodeKind.RELEASE}


@scenario()
async def test_judges_are_the_critic_for_the_plan_and_the_panel_for_a_task(f: Flow):
    f.ce.change.panel = ["reviewer", "security"]
    judges = {k: s.judges(f.ce) for k, s in STEP_TYPES.items() if s.judges(f.ce)}
    assert judges == {PLAN: ("critic",), BUILD: ("reviewer", "security")}
    assert not STEP_TYPES[PLAN].phase(Panel).decides        # the critic's handler decides


@scenario()
async def test_each_verification_resolves_its_commands(f: Flow):
    f.ce.change.oracle = ORACLE_DOC
    n = Node(id="x", kind=BASELINE, title="x")
    cmds = {k: STEP_TYPES[k].phase(Verify).commands_for(f.ce, n)
            for k in (BASELINE, PREFLIGHT, INTEGRATION)}
    assert cmds[BASELINE] == ("bun test acc", "bun install")
    assert cmds[PREFLIGHT] == ("bun test acc",)
    assert cmds[INTEGRATION][:2] == ("bun test acc", "bun install")


def test_verification_is_recorded_per_task_cycle_or_per_node():
    class T:
        id = "T3"
    build = Node(id="T3", kind=BUILD, title="t", cycle=2)
    build.task = T()  # type: ignore[assignment]
    assert Verify.record(build) == ("tasks/T3/verification.json", "2")
    for kind in (BASELINE, PREFLIGHT, INTEGRATION):
        path, key = Verify.record(Node(id=f"{kind}-node", kind=kind, title="x"))
        assert key == str(kind) and path.startswith("control/verification/")


def test_the_current_phase_follows_the_node_status():
    build = STEP_TYPES[BUILD]
    phase = {s: build.current(Node(id="T", kind=BUILD, title="t", status=s))
             for s in (PENDING, READY, REWORK, RUNNING, VERIFYING, REVIEWING, MERGING, ACCEPTED,
                       ESCALATED)}
    assert {s: type(p).__name__ if p else None for s, p in phase.items()} == {
        PENDING: None, READY: "Produce", REWORK: "Produce", RUNNING: "Produce",
        VERIFYING: "Verify", REVIEWING: "Panel", MERGING: "Merge", ACCEPTED: None,
        ESCALATED: None}


# ---------------------------------------------------------------------------- the runner
def node(f: Flow, kind: NodeKind, **kw) -> Node:
    n = Node(**({"id": f"N-{kind}", "kind": kind, "title": "x", "status": RUNNING,
                 "candidate_sha": CAND} | kw))
    f.ce.nodes[n.id] = n
    return n


async def report(f: Flow, n: Node, result=None, role: str = "implementer"):
    """An agent's turn ended after an accepted report in the node's current phase, with its
    handler's result (what the agent's work returns to its phase)."""
    from legion_orch.workflow.engine.services.agent_runs import Ended
    from legion_orch.workflow.model import R_REPORTED, Run
    run = Run(id=f"r{9100 + len(f.ce.runs)}", node_id=n.id, role=role, soul="s", cycle=n.cycle,
              sandbox=f"sb-{len(f.ce.runs)}", status=R_REPORTED)
    f.ce.runs[run.id] = run
    rt = f.ce._runner
    phase = rt.of(n).current()
    ended = Ended(run, Ok() if result is None else result)
    if isinstance(phase.phase, Panel):                   # a judge's: collated by the panel
        phase.judged(ended)
        phase.conclude(await phase.result({role: ended}))
    else:
        phase.conclude(phase.ended(ended))
    return run


def capture_work(f: Flow) -> list[str]:
    """The phases whose work the runner was asked to start (not run)."""
    rt, started = f.ce._runner, []

    of = rt.of

    def capturing(node):
        step = of(node)
        step.run = lambda phase, work=None: started.append(type(phase.phase).__name__)
        return step
    rt.of = capturing
    return started


@scenario()
async def test_a_task_goes_from_its_agent_to_verify_panel_merge_and_acceptance(f: Flow):
    rt, n = f.ce._runner, node(f, BUILD)
    f.ce.change.panel = ["reviewer"]
    started = capture_work(f)
    await report(f, n)                                         # the implementer's report
    assert n.status == VERIFYING and started == ["Verify"]
    rt.of(n).advance(rt.of(n).current())                 # verification passed
    assert n.status == REVIEWING and started[-1] == "Panel"     # its judge's run
    judge = await report(f, n, result=None, role="reviewer")   # its handler records the review only
    assert n.status == REVIEWING                         # the panel decides: no review yet
    assert judge.status == "ACCEPTED" and judge.concluded
    assert judge.sandbox in f.ce._sandboxes.retiring     # the judge's sandbox goes
    rt.of(n).advance(rt.of(n).current())                 # the panel passed
    assert n.status == MERGING and started[-1] == "Merge"
    rt.of(n).advance(rt.of(n).current())                 # merged
    assert n.status == ACCEPTED


@scenario()
async def test_an_empty_panel_is_passed_straight_to_the_merge(f: Flow):
    rt, n = f.ce._runner, node(f, BUILD, status=VERIFYING)
    f.ce.change.panel = []
    capture_work(f)
    rt.of(n).advance(rt.of(n).current())
    assert n.status == MERGING


@scenario()
async def test_a_one_phase_agent_step_is_accepted_when_its_report_is(f: Flow):
    n = node(f, NodeKind.RELEASE)
    await report(f, n, role="release")
    assert n.status == ACCEPTED


@scenario()
async def test_a_handlers_rework_or_escalation_is_applied_by_its_phase(f: Flow):
    from legion_orch.workflow.engine.steps.phases import Escalate, Rework
    from legion_orch.workflow.model import Feedback, Resume
    capture_work(f)
    n = node(f, NodeKind.INTEGRATION)
    await report(f, n, Escalate("integration_failed", "FAIL", Resume.READY, run="r1"), "integration")
    assert n.status == ESCALATED and n.escalation.run == "r1"
    p = node(f, PLAN, status=REVIEWING)                  # the critic's verdict at the panel
    fb = [Feedback(cycle=1, source="critique", text="thin")]
    await report(f, p, Rework(fb, "critique", budget_checked=True), "critic")
    assert (p.status, p.cycle, p.feedback) == (REWORK, 2, fb)


@scenario()
async def test_entering_a_step_sets_its_first_phase_and_starts_its_work(f: Flow):
    rt = f.ce._runner
    started = capture_work(f)
    gate = Node(id="H-1", kind=HUMAN, title="x")
    pre, task = (Node(id=f"x-{k}", kind=k, title="x") for k in (PREFLIGHT, BUILD))
    for n in (gate, pre, task):
        rt.enter(n)
    assert gate.status == pre.status == task.status == READY   # each starts its work
    assert started == ["Approval", "Verify", "Produce"]        # a gate's: its checks, then ask


@scenario()
async def test_a_steps_first_work_waits_for_its_waits(f: Flow):
    rt = f.ce._runner
    retro = node(f, NodeKind.RETROSPECTIVE, id="retrospective", status=READY)
    f.ce._sandboxes.reflector.pending.add("sb")              # an agent still reflects
    assert not rt.of(retro).waits_hold()
    assert not rt.of(retro).at(step_for(retro).phases[0]).ready()
    f.ce._sandboxes.reflector.pending.clear()
    assert rt.of(retro).at(step_for(retro).phases[0]).ready()


@scenario()
async def test_a_waiting_step_goes_on_at_a_save_once_its_waits_hold(f: Flow):
    import asyncio
    rt = f.ce._runner
    n = node(f, NodeKind.RELEASE, status=READY)
    step, holds = rt.of(n), [False]
    phase = step_for(n).phases[0].at(step, step.dispatch)
    waiting = asyncio.ensure_future(step.wait_until(phase, lambda: holds[0]))
    await asyncio.sleep(0)
    assert rt.waiting == 1 and not waiting.done()
    holds[0] = True
    f.ce.notify()                                            # a save
    assert await waiting is True and rt.waiting == 0
    step.release()                                           # the step is over: it waits no more
    assert await step.wait_until(phase, lambda: False) is False


@scenario()
async def test_a_step_holds_one_reservation_until_it_is_over(f: Flow):
    rt = f.ce._runner
    n = node(f, NodeKind.RELEASE, status=READY)
    step = rt.of(n)
    work = step.dispatch
    assert step.dispatch is work and not work.released
    step.escalate("x", "why", Resume.READY)
    assert work.released and step.dispatch is not work       # resuming takes a new one


@scenario()
async def test_after_a_restart_a_step_takes_its_work_up_again(f: Flow):
    rt = f.ce._runner
    started = capture_work(f)
    prov = node(f, PROVISION, id="p", status=RUNNING)
    rt.recover(prov)
    assert started == ["BuildImages"]                        # its provisioning again
    agent = node(f, BUILD, status=READY)
    rt.recover(agent)
    assert started[-1] == "Produce"                         # it waits for its turn again


@scenario()
async def test_a_run_live_at_a_restart_escalates_its_step(f: Flow):
    from legion_orch.workflow.model import FailureSource, Run
    rt, n = f.ce._runner, node(f, BUILD)
    run = Run(id="r9999", node_id=n.id, role="implementer", soul="s", cycle=1)
    f.ce.runs[run.id] = run
    f.ce._lifecycle.fail_run(run, "controller restarted", FailureSource.CONTROLLER)
    rt.recover(n, run)
    assert n.status == ESCALATED and n.escalation.kind == "run_failed"


# ---------------------------------------------------------------------------- what results mean
def ver(*rcs: int, setup: int | None = None) -> Verification:
    results = ([CommandResult(command="mise run setup", exit_code=setup, output="boom",
                              setup=True)] if setup is not None else [])
    results += [CommandResult(command=f"cmd{i}", exit_code=rc) for i, rc in enumerate(rcs)]
    return Verification(sha=CAND, ok=all(r.exit_code == 0 for r in results), results=results)


async def conclude(f: Flow, n: Node, res: Verification) -> None:
    """The verification's result, as its phase's work hands it over."""
    rt = f.ce._runner
    phase = rt.of(n).at(step_for(n).phase(Verify))
    phase.conclude(await phase.result(res))


@scenario()
async def test_a_failing_integration_escalates_instead_of_reworking(f: Flow):
    n = node(f, INTEGRATION, status=VERIFYING)
    await conclude(f, n, ver(1, 0, 2))
    assert n.status == ESCALATED and n.escalation.kind == "integration_failed"
    assert "2 oracle command(s) failed" in n.escalation.reason and n.cycle == 1


@scenario()
async def test_a_task_that_fails_verification_is_reworked(f: Flow):
    n = node(f, BUILD, status=VERIFYING)
    await conclude(f, n, ver(1))
    assert (n.status, n.cycle) == (REWORK, 2)


@scenario()
async def test_a_baseline_and_a_preflight_pass_with_expected_failures_and_note_passes(f: Flow):
    f.ce.change.oracle = ORACLE_DOC
    for kind in (BASELINE, PREFLIGHT):
        n = node(f, kind, status=VERIFYING)
        await conclude(f, n, ver(1, 0))
        assert n.status == ACCEPTED, kind
    timeline = "\n".join(f.ce._timeline)
    assert "acceptance command `cmd1` already passes before any build" in timeline


@scenario()
async def test_a_preflight_setup_failure_redoes_its_rounds_oracle(f: Flow):
    """The policy's result: the round's oracle again, with the output, within its budget; a
    human decides after. (The core's redo itself: test_workflow_tree, test_preflight.)"""
    from legion_orch.workflow.engine.steps.phases import Escalate, OracleReworks
    from legion_orch.workflow.results import Redo
    oracle = node(f, ORACLE, id=ORACLE, status=ACCEPTED)
    n = node(f, PREFLIGHT, status=VERIFYING)
    setup = CommandResult(command="mise run setup", exit_code=1, output="boom", setup=True)
    esc = Escalate("setup_failed", "G-V3", Resume.VERIFYING)
    result = OracleReworks()(f.ce, n, setup, "att", esc)
    assert isinstance(result, Redo) and result.step == ORACLE
    assert "BLOCKER (preflight)" in result.feedback.text and result.feedback.attestations == ["att"]
    oracle.cycle = f.cfg.limits.review_max_cycles               # no cycle left
    assert OracleReworks()(f.ce, n, setup, "att", esc) is esc


@scenario()
async def test_a_baseline_setup_failure_escalates(f: Flow):
    n = node(f, BASELINE, status=VERIFYING)
    await conclude(f, n, ver(setup=1))
    assert n.status == ESCALATED and n.escalation.kind == "setup_failed"


# ---------------------------------------------------------------------------- waits
@scenario()
async def test_the_retrospective_waits_for_the_last_reflections(f: Flow):
    retro = Node(id="retro-x", kind=NodeKind.RETROSPECTIVE, title="x")
    wait = STEP_TYPES[NodeKind.RETROSPECTIVE].dispatch_when[0]
    assert wait.holds(f.ce._runner.of(retro))
    f.ce._sandboxes.reflector.pending.add("sb")
    assert not wait.holds(f.ce._runner.of(retro))


@scenario(max_parallel=1)
async def test_a_step_waits_while_the_dispatcher_has_no_room(f: Flow):
    """The sandbox limit is the dispatcher's: a step's work waits for a slot there (the step
    layer knows no limit)."""
    assert f.run("intake")                                   # the one run allowed is live
    n = node(f, NodeKind.RELEASE, status=READY)
    f.ce._runner.enter(n)
    await f.settle()
    assert n.status == READY and f.ctl.svc.dispatcher.waiting == 1
    assert f.ctl.svc.dispatcher.in_use == 1


# ---------------------------------------------------------------------------- gates
async def run_gate(f: Flow, n: Node, answered: bool | None = None) -> None:
    """Enter the gate step (``answered`` None), or resume it – answered by a human, or retried."""
    rt = f.ce._runner
    phase = step_for(n).phases[0]
    if answered is None:
        rt.of(n).move(READY)
    if answered:
        n.approved = True                                # what resuming answered does
    step = rt.of(n)
    await phase.at(step, step.dispatch).task()


@scenario()
async def test_a_gate_step_asks_a_human_and_passes_once_answered(f: Flow):
    f.ce._runner.runtime.completed = lambda node: None        # the core is not involved here
    f.ce._runner.runtime.escalated = lambda node: None
    f.ce._runner.runtime.resumed = lambda node: None
    n = node(f, HUMAN, id="H-1", status=PENDING, title="Accept requirements & tier")
    await run_gate(f, n)
    assert n.status == AWAITING_HUMAN and n.escalation.kind == "awaiting_approval"
    f.ce._runner.states.resume(n, READY)
    await run_gate(f, n, answered=False)                      # a re-check is no approval
    assert n.status == AWAITING_HUMAN and not n.approved
    f.ce._runner.states.resume(n, READY)
    await run_gate(f, n, answered=True)
    assert n.status == ACCEPTED and n.approved


@scenario()
async def test_an_approved_gate_with_unmet_checks_stays_approved_when_rechecked(f: Flow):
    from legion_orch.workflow.engine.steps.phases import ShipGates
    rt = f.ce._runner
    rt.runtime.completed = rt.runtime.escalated = rt.runtime.resumed = lambda node: None
    n = node(f, HUMAN, id="H-3", status=PENDING, title="Accept the change")
    f.ce.change.tier = "R2"
    unmet = [("protected_oracle",), ("protected_oracle",), ()]
    real_evaluate, real_unmet = ShipGates.evaluate, ShipGates.unmet

    async def evaluate(self, c):
        return None
    ShipGates.evaluate = evaluate
    ShipGates.unmet = lambda self, c, node, evaluated: unmet.pop(0)
    try:
        await run_gate(f, n)
        assert n.status == AWAITING_HUMAN and "not met yet: protected_oracle" in n.escalation.reason
        rt.states.resume(n, READY)
        await run_gate(f, n, answered=True)                   # approved, still unmet
        assert n.escalation.kind == "preconditions_unmet" and n.approved
        rt.states.resume(n, READY)
        await run_gate(f, n, answered=False)                  # re-checked: met now
        assert n.status == ACCEPTED
    finally:
        ShipGates.evaluate, ShipGates.unmet = real_evaluate, real_unmet


@scenario()
async def test_a_step_cannot_escalate_to_resume_at_a_phase_it_lacks(f: Flow):
    """The resume point is checked when it is set: a step resumes only in one of its phases."""
    import pytest

    from legion_orch.workflow.engine.steps.node_states import IllegalNodeTransition
    rt, n = f.ce._runner, node(f, PROVISION, status=RUNNING)
    with pytest.raises(IllegalNodeTransition):
        rt.of(n).escalate("x", "why", Resume.REVIEWING)           # provisioning has no panel
    assert n.status == RUNNING
    rt.of(n).escalate("x", "why", Resume.READY)
    assert n.status == ESCALATED and n.escalation.resume == Resume.READY


@scenario()
async def test_a_stale_approval_resumes_where_the_agent_was_paused(f: Flow):
    """G-B1 at a first run resumes it READY (a new run); at a rework, REWORK."""
    agents = f.ce._agents
    agents._approval_fresh = lambda: ["H-2 is stale"]
    first, again = node(f, BUILD, status=READY), node(f, BUILD, id="N-b2", status=REWORK)
    assert agents.preconditions(first, "implementer").resume == Resume.READY
    assert agents.preconditions(again, "implementer").resume == Resume.REWORK
