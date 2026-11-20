"""Step types (engine/steps.py): each kind of node as a composition – an agent, a sandbox plan,
phases configured by policies, waits – and the StepRunner that moves a node through its phases."""
from __future__ import annotations

from engine_harness import Flow, scenario

from legion_orch.workflow.engine.phases import Ok, Panel, Verify
from legion_orch.workflow.engine.sandbox_plan import JUDGE
from legion_orch.workflow.engine.steps import (
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
        NodeKind.INTAKE: ["AgentRun"],
        PLAN: ["AgentRun", "Panel"],
        PROVISION: ["BuildImages"],
        ORACLE: ["AgentRun"],
        PREFLIGHT: ["Verify"],
        BASELINE: ["AgentRun", "Verify"],
        BUILD: ["AgentRun", "Verify", "Panel", "Merge"],
        INTEGRATION: ["AgentRun", "Verify"],
        NodeKind.RELEASE: ["AgentRun"],
        NodeKind.RETROSPECTIVE: ["AgentRun"],
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
        PENDING: None, READY: "AgentRun", REWORK: "AgentRun", RUNNING: "AgentRun",
        VERIFYING: "Verify", REVIEWING: "Panel", MERGING: "Merge", ACCEPTED: None,
        ESCALATED: None}


# ---------------------------------------------------------------------------- the runner
def node(f: Flow, kind: NodeKind, **kw) -> Node:
    n = Node(**({"id": f"N-{kind}", "kind": kind, "title": "x", "status": RUNNING,
                 "candidate_sha": CAND} | kw))
    f.ce.nodes[n.id] = n
    return n


def report(f: Flow, n: Node, result=None, role: str = "implementer"):
    """An agent's accepted report in the node's current phase, with its handler's result."""
    from legion_orch.workflow.model import Run
    run = Run(id=f"r{9100 + len(f.ce.runs)}", node_id=n.id, role=role, soul="s", cycle=n.cycle,
              sandbox=f"sb-{len(f.ce.runs)}")
    f.ce.runs[run.id] = run
    f.ce._runner.reported(n, run, Ok() if result is None else result)
    return run


def capture_spawns(f: Flow) -> list[str]:
    spawned: list[str] = []

    def spawn(coro):
        spawned.append(coro.__qualname__)
        coro.close()
    f.ce.spawn = spawn
    return spawned


@scenario()
async def test_a_task_goes_from_its_agent_to_verify_panel_merge_and_acceptance(f: Flow):
    rt, n = f.ce._runner, node(f, BUILD)
    f.ce.change.panel = ["reviewer"]
    spawned = capture_spawns(f)
    report(f, n)                                         # the implementer's report
    assert n.status == VERIFYING and spawned == ["Acceptance.verify_node"]
    rt.advance(n, rt.step(n).current(n))                 # verification passed
    assert n.status == REVIEWING
    judge = report(f, n, result=None, role="reviewer")   # its handler records the review only
    assert n.status == REVIEWING                         # the panel decides: no review yet
    assert judge.status == "ACCEPTED"
    assert judge.sandbox in f.ce._sandboxes.retiring     # the judge's sandbox goes
    rt.advance(n, rt.step(n).current(n))                 # the panel passed
    assert n.status == MERGING and spawned[-1] == "Acceptance.merge_node"
    rt.advance(n, rt.step(n).current(n))                 # merged
    assert n.status == ACCEPTED


@scenario()
async def test_an_empty_panel_is_passed_straight_to_the_merge(f: Flow):
    rt, n = f.ce._runner, node(f, BUILD, status=VERIFYING)
    f.ce.change.panel = []
    capture_spawns(f)
    rt.advance(n, rt.step(n).current(n))
    assert n.status == MERGING


@scenario()
async def test_a_one_phase_agent_step_is_accepted_when_its_report_is(f: Flow):
    n = node(f, NodeKind.RELEASE)
    report(f, n, role="release")
    assert n.status == ACCEPTED


@scenario()
async def test_a_handlers_rework_or_escalation_is_applied_by_its_phase(f: Flow):
    from legion_orch.workflow.engine.phases import Escalate, Rework
    from legion_orch.workflow.model import Feedback, Resume
    n = node(f, NodeKind.INTEGRATION)
    report(f, n, Escalate("integration_failed", "FAIL", Resume.READY, run="r1"), "integration")
    assert n.status == ESCALATED and n.escalation.run == "r1"
    p = node(f, PLAN, status=REVIEWING)                  # the critic's verdict at the panel
    fb = [Feedback(cycle=1, source="critique", text="thin")]
    report(f, p, Rework(fb, "critique", budget_checked=True), "critic")
    assert (p.status, p.cycle, p.feedback) == (REWORK, 2, fb)


@scenario()
async def test_entering_a_step_sets_its_first_phase_and_dispatch_starts_its_work(f: Flow):
    rt = f.ce._runner
    spawned = capture_spawns(f)
    gate, pre, task = (Node(id=f"x-{k}", kind=k, title="x") for k in (HUMAN, PREFLIGHT, BUILD))
    for n in (gate, pre, task):
        rt.enter(n)
    assert gate.status == AWAITING_HUMAN and task.status == READY
    assert pre.status == READY and spawned == []           # its verification waits its turn
    f.ce.nodes[pre.id] = pre
    async with f.ce.lock:
        assert rt.dispatch(pre, 4) == []
    assert (pre.status, pre.candidate_sha) == (VERIFYING, f.ce.change.integration_head)
    assert spawned == ["Acceptance.verify_node"]


@scenario()
async def test_a_steps_first_work_waits_for_its_waits(f: Flow):
    rt = f.ce._runner
    retro = node(f, NodeKind.RETROSPECTIVE, id="retrospective", status=READY)
    f.ce._sandboxes.reflector.pending.add("sb")              # an agent still reflects
    assert rt.dispatch(retro, 4) == [] and retro.status == READY


@scenario()
async def test_after_a_restart_only_controller_work_resumes(f: Flow):
    rt = f.ce._runner
    prov, agent = Node(id="p", kind=PROVISION, title="p", status=RUNNING), node(f, BUILD)
    work = rt.resume_work(prov)
    assert work is not None and work.__qualname__ == "BuildImages._provision"   # its result comes back
    work.close()
    assert rt.resume_work(agent) is None


@scenario()
async def test_an_agent_step_dispatches_only_when_ready(f: Flow):
    n = node(f, BUILD, status=RUNNING)
    assert f.ce._runner.dispatch(n, 4) == []                 # its agent's run is live


# ---------------------------------------------------------------------------- what results mean
def ver(*rcs: int, setup: int | None = None) -> Verification:
    results = ([CommandResult(command="mise run setup", exit_code=setup, output="boom",
                              setup=True)] if setup is not None else [])
    results += [CommandResult(command=f"cmd{i}", exit_code=rc) for i, rc in enumerate(rcs)]
    return Verification(sha=CAND, ok=all(r.exit_code == 0 for r in results), results=results)


def conclude(f: Flow, n: Node, res: Verification) -> None:
    step_for(n).phase(Verify).verified(f.ce._runner, n, res, "att")


@scenario()
async def test_a_failing_integration_escalates_instead_of_reworking(f: Flow):
    n = node(f, INTEGRATION, status=VERIFYING)
    conclude(f, n, ver(1, 0, 2))
    assert n.status == ESCALATED and n.escalation.kind == "integration_failed"
    assert "2 oracle command(s) failed" in n.escalation.reason and n.cycle == 1


@scenario()
async def test_a_task_that_fails_verification_is_reworked(f: Flow):
    n = node(f, BUILD, status=VERIFYING)
    conclude(f, n, ver(1))
    assert (n.status, n.cycle) == (REWORK, 2)


@scenario()
async def test_a_baseline_and_a_preflight_pass_with_expected_failures_and_note_passes(f: Flow):
    f.ce.change.oracle = ORACLE_DOC
    for kind in (BASELINE, PREFLIGHT):
        n = node(f, kind, status=VERIFYING)
        conclude(f, n, ver(1, 0))
        assert n.status == ACCEPTED, kind
    timeline = "\n".join(f.ce._timeline)
    assert "acceptance command `cmd1` already passes before any build" in timeline


@scenario()
async def test_a_preflight_setup_failure_redoes_its_rounds_oracle(f: Flow):
    """The policy's result: the round's oracle again, with the output, within its budget; a
    human decides after. (The core's redo itself: test_workflow_tree, test_preflight.)"""
    from legion_orch.workflow.engine.phases import Escalate, OracleReworks
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
    conclude(f, n, ver(setup=1))
    assert n.status == ESCALATED and n.escalation.kind == "setup_failed"


# ---------------------------------------------------------------------------- waits
@scenario()
async def test_the_retrospective_waits_for_the_last_reflections(f: Flow):
    retro = Node(id="retro-x", kind=NodeKind.RETROSPECTIVE, title="x")
    wait = STEP_TYPES[NodeKind.RETROSPECTIVE].dispatch_when[0]
    assert wait.holds(f.ce._scheduler, retro)
    f.ce._sandboxes.reflector.pending.add("sb")
    assert not wait.holds(f.ce._scheduler, retro)
