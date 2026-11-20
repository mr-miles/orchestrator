"""The orchestrator phases (engine/phases.py) on their own: what each one concludes – ok, rework or
escalate – from scripted runner, builder and judge results, and how a result becomes a node's
state."""
from __future__ import annotations

from engine_harness import Flow, scenario

from legion_orch.sandbox import ImageRecipe, Tool
from legion_orch.workflow.engine.steps.phases import (
    ActivePhase,
    Escalate,
    Merge,
    Ok,
    Panel,
    PhaseContext,
    Rework,
    Verify,
    verification,
)
from legion_orch.workflow.model import (
    BUILD,
    ESCALATED,
    R_REPORTED,
    R_SUPERSEDED,
    REWORK,
    VERIFYING,
    CommandResult,
    Confidence,
    Feedback,
    Finding,
    Node,
    Readiness,
    Resume,
    ReviewResult,
    Run,
    Severity,
    Verdict,
    Verification,
)

SHA = "a" * 40
CAND = "c" * 40


def ctx(f: Flow) -> PhaseContext:
    return f.ce._phases


def task(f: Flow, **kw) -> Node:
    node = Node(**({"id": "T9", "kind": BUILD, "title": "a task", "candidate_sha": CAND,
                    "status": VERIFYING} | kw))                 # in its step
    f.ce.nodes[node.id] = node
    return node


def ver(*results: CommandResult, infra: str | None = None) -> Verification:
    return Verification(sha=CAND, ok=all(r.exit_code == 0 for r in results), results=list(results),
                        infra_error=infra)


def cmd(command: str, rc: int = 0, *, setup: bool = False) -> CommandResult:
    return CommandResult(command=command, exit_code=rc, output=f"out of {command}", setup=setup)


# ---------------------------------------------------------------------------- Verify.judge
def test_passing_verification_is_ok():
    res = ver(cmd("setup", setup=True), cmd("pytest"))
    assert Verify(("pytest",)).interpret(res, 1, "att") == Ok(res)


def test_failing_commands_are_rework_with_one_blocker_each():
    res = ver(cmd("setup", setup=True), cmd("pytest", 1), cmd("ruff", 2), cmd("mypy"))
    result = Verify(("pytest", "ruff", "mypy")).interpret(res, 3, "file:v@1")
    assert isinstance(result, Rework) and result.source == "verification"
    assert [fb.text.split(":")[0] for fb in result.feedback] == ["BLOCKER", "BLOCKER"]
    assert "`pytest` exited 1" in result.feedback[0].text
    assert all(fb.cycle == 3 and fb.attestations == ["file:v@1"] for fb in result.feedback)


def test_an_infrastructure_error_escalates_to_verify_again():
    result = Verify(("pytest",)).interpret(ver(infra="gateway down"), 1, None)
    assert result == Escalate("verify_infra", "gateway down", Resume.VERIFYING)


def test_a_missing_command_escalates_without_spending_a_cycle():
    result = Verify(("bun test",)).interpret(ver(cmd("bun test", 127)), 1, None)
    assert isinstance(result, Escalate) and result.kind == "verify_unrunnable"
    assert "`bun test`" in result.question and "out of bun test" in result.log_tail
    # a setup that is not found is the setup failing, not the image lacking a command
    result = Verify((), expect_failures=True).interpret(ver(cmd("setup", 127, setup=True)), 1, None)
    assert result.kind == "setup_failed"


def test_expected_failures_are_ok_but_a_failed_setup_is_not():
    phase = Verify(("pytest",), expect_failures=True)
    res = ver(cmd("setup", setup=True), cmd("pytest", 1))
    assert phase.interpret(res, 1, None) == Ok(res)
    result = phase.interpret(ver(cmd("mise run setup", 1, setup=True)), 1, None)
    assert isinstance(result, Escalate) and result.kind == "setup_failed"
    assert result.question.startswith("G-V3: the repository's setup `mise run setup` exited 1")
    assert result.resume == Resume.VERIFYING


def test_a_failed_setup_in_a_build_fails_like_any_command():
    result = Verify(("pytest",)).interpret(ver(cmd("mise run setup", 1, setup=True)), 1, "att")
    assert isinstance(result, Rework) and "`mise run setup` exited 1" in result.feedback[0].text


# ---------------------------------------------------------------------------- Verify.verification
@scenario()
async def test_nothing_to_verify_is_an_infrastructure_error(f: Flow):
    before = list(f.runner.created)
    res = await verification(ctx(f), "T9", SHA, (), dispatch=f.ce.svc.dispatcher)
    assert res.infra_error == "no commands to verify" and f.runner.created == before


@scenario()
async def test_commands_run_in_order_after_the_setup_and_not_after_it_fails(f: Flow):
    from legion_orch.workflow.model import SetupPin
    f.ce.change.setup_pin = SetupPin(blob="b" * 40, source="base")
    res = await verification(ctx(f), "T9", SHA, ("first", "second"), dispatch=f.ce.svc.dispatcher)
    assert [r.command for r in res.results][1:] == ["first", "second"] and res.ok
    assert res.results[0].setup
    f.runner.commands["b" * 40] = (1, "no setup task")
    res = await verification(ctx(f), "T9", SHA, ("first", "second"), dispatch=f.ce.svc.dispatcher)
    assert len(res.results) == 1 and not res.ok
    assert len(f.runner.deleted) == 2                        # every verifier is removed


@scenario()
async def test_a_taken_verifier_name_is_replaced(f: Flow):
    before = list(f.runner.created)
    f.runner.clash = 1
    res = await verification(ctx(f), "T9", SHA, ("pytest",), dispatch=f.ce.svc.dispatcher)
    assert res.ok and f.runner.created == [*before, res.sandbox]


@scenario()
async def test_a_verifier_that_cannot_start_is_reported_not_raised(f: Flow):
    f.runner.fail_create = "verifier"
    res = await verification(ctx(f), "T9", SHA, ("pytest",), dispatch=f.ce.svc.dispatcher)
    assert not res.ok and "quota" in res.infra_error


# ---------------------------------------------------------------------------- Panel
def finding(title: str) -> Finding:
    return Finding(severity=Severity.BLOCKER, confidence=Confidence.HIGH, title=title,
                   fix="do it", attestations=["file:src/a.py@1"])


def judged(node: Node, role: str, verdict: Verdict, *, sha: str = CAND, findings=()) -> None:
    node.reviews[node.review_key(node.cycle, role)] = ReviewResult(
        verdict=verdict, sha=sha, run=f"r-{role}", findings=list(findings), doc=f"file:{role}@1")


def reviewing(f: Flow, node: Node, *roles: str) -> ActivePhase:
    """A panel of ``roles`` as it runs for ``node``."""
    return f.ce._runner.of(node).at(Panel(roles))


@scenario()
async def test_the_panel_waits_for_every_judge(f: Flow):
    node = task(f)
    panel = reviewing(f, node, "reviewer", "security")
    judged(node, "reviewer", Verdict.PASS)
    assert panel.missing() == ["security"] and panel.decide() is None


@scenario()
async def test_a_unanimous_pass_at_the_candidate_is_ok(f: Flow):
    node = task(f)
    judged(node, "reviewer", Verdict.PASS)
    judged(node, "security", Verdict.PASS)
    assert reviewing(f, node, "reviewer", "security").decide() == Ok()
    judged(node, "security", Verdict.PASS, sha=SHA)          # judged an older candidate
    assert isinstance(reviewing(f, node, "reviewer", "security").decide(), Rework)


@scenario()
async def test_high_findings_are_rework_and_repeated_ones_escalate(f: Flow):
    node = task(f)
    panel = reviewing(f, node, "reviewer")
    judged(node, "reviewer", Verdict.FAIL, findings=[finding("Unsafe eval")])
    first = panel.decide()
    assert isinstance(first, Rework) and first.source == "review"
    assert "Unsafe eval" in first.feedback[0].text
    assert first.feedback[-1].attestations == ["file:reviewer@1"]
    node.cycle = 2
    judged(node, "reviewer", Verdict.FAIL, findings=[finding("unsafe eval ")])
    second = panel.decide()
    assert isinstance(second, Escalate) and second.kind == "stale_loop"
    assert second.resume == Resume.REWORK_CYCLE and second.feedback


# ---------------------------------------------------------------------------- applying results
@scenario()
async def test_rework_supersedes_the_report_and_starts_the_next_cycle(f: Flow):
    run = Run(id="r9001", node_id="T9", role="implementer", soul="s", cycle=1, status=R_REPORTED)
    f.ce.runs[run.id] = run
    node = task(f, current_run=run.id)
    fb = [Feedback(cycle=1, source="merge", text="conflict")]
    f.ce._runner.of(node).apply(Rework(fb, "merge", base=SHA))
    assert (node.status, node.cycle, node.candidate_sha, node.base_sha) == (REWORK, 2, None, SHA)
    assert run.status == R_SUPERSEDED and node.feedback == fb


@scenario(review_max_cycles=1)
async def test_rework_past_the_budget_escalates(f: Flow):
    node = task(f)
    f.ce._runner.of(node).apply(Rework([], "review", base=SHA))
    assert node.status == ESCALATED and node.escalation.kind == "cycle_budget"
    assert node.cycle == 1 and node.base_sha is None


@scenario()
async def test_an_escalation_keeps_its_feedback_for_the_next_cycle(f: Flow):
    node = task(f)
    fb = [Feedback(cycle=1, source="review", text="same again")]
    f.ce._runner.of(node).apply(Escalate("stale_loop", "why", Resume.REWORK_CYCLE, log_tail="tail",
                                feedback=fb))
    assert node.status == ESCALATED and node.feedback == fb
    assert (node.escalation.kind, node.escalation.resume) == ("stale_loop", Resume.REWORK_CYCLE)


# ---------------------------------------------------------------------------- Merge
def merging(f: Flow, node: Node) -> ActivePhase:
    return f.ce._runner.of(node).at(Merge())


@scenario()
async def test_merge_needs_verification_and_every_pass_at_the_candidate(f: Flow):
    node = task(f)
    judged(node, "reviewer", Verdict.PASS, sha=SHA)
    f.ce.change.panel = ["reviewer", "security"]           # the build's judges
    result = await merging(f, node).merge()
    assert isinstance(result, Escalate) and result.kind == "accept_guard"
    assert result.question.split("; ") == [
        "G-A1: no passing deterministic verification at candidate",
        "G-A1: no reviewer PASS at candidate", "G-A1: no security PASS at candidate"]
    assert result.resume == Resume.REWORK


@scenario()
async def test_an_override_waives_the_panel_but_not_verification(f: Flow):
    from legion_orch.workflow.model import Override
    node = task(f, override=Override(approver="miles", reason="trusted", sha=CAND))
    f.ce.change.panel = ["reviewer"]
    result = await merging(f, node).merge()
    assert result.question == "G-A1: no passing deterministic verification at candidate"


@scenario()
async def test_merge_stops_when_the_integration_head_moved(f: Flow):
    node = task(f)
    node.verification["1"] = Verification(sha=CAND, ok=True)
    f.ce.change.integration_head = SHA                     # not where the branch is
    f.ce.change.panel = []
    result = await merging(f, node).merge()
    assert isinstance(result, Escalate) and result.kind == "head_moved"
    assert result.resume == Resume.MERGING


# ---------------------------------------------------------------------------- BuildImages
def recipe(f: Flow, tool: str) -> ImageRecipe:
    return ImageRecipe.make("base:1", [Tool(tool, tool, {tool: "1"})], mise_version="2025.1.0", user="agent",
                            registry=None, change=f.ce.cid)


@scenario()
async def test_images_are_built_and_logged(f: Flow):
    r, log = recipe(f, "uv"), []
    result = await f.ce._provisioner.build_images({r.tag: r}, log)
    assert result == Ok({r.tag: r.tag}) and f.builder.built == [r.tag]
    assert log[0].startswith(f"## image {r.tag}") and log[1] == f"built {r.tag}"


@scenario()
async def test_a_failed_build_escalates_with_the_log(f: Flow):
    r = recipe(f, "uv")
    f.builder.fail = "apt-get failed"
    result = await f.ce._provisioner.build_images({r.tag: r}, [])
    assert isinstance(result, Escalate) and result.kind == "provision_failed"
    assert result.question == "G-PV1: apt-get failed" and result.resume == Resume.READY
    assert f"## image {r.tag}" in result.log_tail


# ---------------------------------------------------------------------------- gates, Produce
@scenario()
async def test_the_ship_gates_are_recorded_and_only_their_approval_gates_wait_for_it(f: Flow):
    from legion_orch.workflow.engine.steps.phases import ShipGates
    from legion_orch.workflow.model import GateResult
    gates = {g: GateResult(status="unsatisfied", detail="x")
             for g in ("explicit_human_approval", "protected_oracle")}
    res = Readiness(verdict="NOT_READY", tier="R2", integration_head=SHA,
                    integration_branch="legion/x", gates=gates)
    node = task(f, id="H-3", kind="human")
    assert ShipGates().unmet(f.ce, node, res) == ("protected_oracle",)   # not yet approved
    assert f.ce.change.ship is res and res.attestation
    f.ce.change.approvals["H-3"] = f.ce.change.approvals.get("H-1")
    assert ShipGates().unmet(f.ce, node, res) == ("explicit_human_approval", "protected_oracle")


@scenario()
async def test_an_agent_run_is_work_its_step_may_still_withdraw(f: Flow):
    node = task(f, status="READY")
    before = len(f.ce.runs)
    work, prepared = ctx(f).agents.work(node, "reviewer", CAND, wanted=lambda: False,
                                        started=lambda run: None)
    outcome = await work.execute()
    assert outcome.skipped and prepared.run is None and len(f.ce.runs) == before


@scenario()
async def test_a_guard_that_stops_an_agent_run_is_its_result(f: Flow):
    from legion_orch.workflow.assets import AssetError
    node = task(f, id="R9", kind="release", status="READY")

    def bad(role, variables):
        raise AssetError("bad")
    f.ce.assets.render_role = bad
    work, prepared = ctx(f).agents.work(node, "release", None, wanted=lambda: True,
                                        started=lambda run: None)
    assert (await work.execute()).skipped
    assert prepared.run is None and prepared.refused.kind == "assets"


@scenario()
async def test_an_agents_work_is_its_checkout_then_its_turn(f: Flow):
    node = task(f, id="R9", kind="release", status="READY")
    started = []
    work, prepared = ctx(f).agents.work(node, "release", None, wanted=lambda: True,
                                        started=started.append)
    import asyncio
    running = asyncio.ensure_future(work.execute())
    for _ in range(100):
        await asyncio.sleep(0.01)
        if prepared.run and prepared.run.id in f.ctl.svc.procs:
            break
    run = prepared.run
    assert started == [run]
    assert [c.kind for c in work.commands] == ["prepare", "prepare", "agent"]
    f.ctl.svc.procs[run.id].exit(0)
    outcome = await running
    assert [s.result.exit_code for s in outcome.steps] == [0, 0, 0]
    assert run.status == "FAILED"                 # it ended without a report: a failure
    assert (await ctx(f).agents.conclude(run)).kind == "run_failed"
