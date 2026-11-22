"""Ship readiness and change-level governance against a real change: gates that fail (and why),
overrides, waivers, human attestations, protected-path approvals and surface drift."""
from __future__ import annotations

import json

import pytest
from engine_harness import REPO, TASKS, Flow, scenario

from legion_orch.workflow import attention, governance
from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.model import ACCEPTED, ChangeStatus, GateStatus
from legion_orch.workflow.ship import evaluate

SAT, UNSAT, UNEVAL = GateStatus.SATISFIED, GateStatus.UNSATISFIED, GateStatus.UNEVALUABLE
REASON = "reviewed by hand against the production incident log"


def not_ready(f: Flow) -> bool:
    """H-3 waits: its preconditions (the ship gates) are not all met; the change is still on."""
    return f.ce.change.status == ChangeStatus.ACTIVE and f.status("H-3") == "AWAITING_HUMAN"


async def ready_r1(f: Flow) -> None:
    await f.to_build()
    await f.implement()
    await f.review()
    assert f.ce.change.status == ChangeStatus.READY


# ====================================================================== gates
@scenario()
async def test_gates_are_unevaluable_until_the_work_exists(f: Flow):
    await f.to_build()
    r = await evaluate(f.ce)
    assert r.verdict == "NOT_READY"
    g = r.gates
    assert g["task_contract"].status == SAT
    # builds exist but none is accepted yet: verification reports unsatisfied, the rest unevaluable
    assert g["deterministic_verification"].status == UNSAT
    for gate in ("evidence_bundle_or_log", "scoped_implementer_run",
                 "lightweight_independent_review"):
        assert g[gate].status == UNEVAL, (gate, g[gate])


@scenario()
async def test_a_changed_task_graph_makes_the_contract_stale(f: Flow):
    await ready_r1(f)
    f.ce.write_record("plan/taskgraph.json", {"tasks": TASKS + [TASKS[0] | {"id": "T9"}]})
    g = (await evaluate(f.ce)).gates["task_contract"]
    assert g.status == UNSAT and "stale" in g.detail


@scenario()
async def test_a_review_doc_that_no_longer_matches_fails_the_review_gate(f: Flow):
    await ready_r1(f)
    node = f.ce.nodes["build:T1"]
    key = next(iter(node.reviews))
    node.reviews[key] = node.reviews[key].model_copy(update={"sha": "0" * 40})
    r = await evaluate(f.ce)
    assert r.gates["lightweight_independent_review"].status == UNSAT
    assert r.verdict == "NOT_READY"


@scenario()
async def test_an_overridden_build_is_human_judgement(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    await f.implement()
    await f.review(verdict="FAIL")
    await f.human("build:T1", "override_accept", reason=REASON)
    await f.describe()
    g = f.ce.change.ship.gates["lightweight_independent_review"]
    assert g.status == SAT and g.human_judgement and "override" in g.detail
    assert "lightweight_independent_review" in f.ce.change.ship.human_judgement_gates


# ====================================================================== overrides
@scenario()
async def test_added_gate_must_be_satisfied(f: Flow):
    await f.to_build()
    await f.change_action("override", kind="add_gate", gate="security_or_e2e_evaluator", reason=REASON)
    await f.implement()
    await f.review()
    g = f.ce.change.ship.gates["security_or_e2e_evaluator"]
    assert g.added and g.status == UNEVAL and not_ready(f)
    src = f.ce.latest_att("tasks/T1/evidence/cycle-1.md")
    await f.change_action("attest", kind="security-evaluation", verdict="fail", sources=[src])
    await f.human("H-3", "recheck")
    assert f.ce.change.ship.gates["security_or_e2e_evaluator"].status == UNSAT
    res = await f.change_action("attest", approver="ana", kind="security-evaluation", verdict="pass",
                                sources=[src])
    assert any("superseded" in w for w in res.warnings)
    await f.human("H-3", "recheck")
    assert f.ce.change.status == ChangeStatus.READY


@scenario()
async def test_waived_gate_holds_only_while_its_evidence_resolves(f: Flow):
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")                    # review gate cannot be met ...
    await f.implement()
    await f.review(verdict="FAIL")
    evidence = f.ce.write_record("control/notes/manual-review.md", "reviewed by hand").attestation
    await f.change_action("override", kind="waive_gate", gate="lightweight_independent_review",
                          reason=REASON, evidence=evidence)  # ... so a human waives it
    await f.human("build:T1", "override_accept", reason=REASON)
    await f.describe()
    ship = f.ce.change.ship
    assert ship.verdict == "READY" and "lightweight_independent_review" in ship.waived_gates
    assert ship.gates["lightweight_independent_review"].detail.startswith("waived by miles")


@scenario()
async def test_override_validation(f: Flow):
    for payload, msg in [({"kind": "lower_tier", "gate": "task_contract"}, "kind must be"),
                         ({"kind": "add_gate", "gate": "vibes"}, "unknown gate"),
                         ({"kind": "add_gate", "gate": "task_contract", "reason": "short"}, "reason"),
                         ({"kind": "waive_gate", "gate": "deterministic_verification", "reason": REASON},
                          "cannot be waived"),
                         ({"kind": "waive_gate", "gate": "task_contract", "reason": REASON,
                           "evidence": "file:control/nope.md@1"}, "replacement evidence")]:
        payload.setdefault("reason", REASON)
        with pytest.raises(WorkflowError, match=msg):
            await f.change_action("override", **payload)
    assert f.ce.change.overrides == []


# ====================================================================== attestations
@scenario()
async def test_attestation_validation(f: Flow):
    await f.intake()                                    # build tasks exist, none started
    await f.human("H-1", "accept")
    await f.plan()
    await f.critique()
    src = f.ce.latest_att("plan/spec.md")
    for payload, msg in [({"kind": "vibes", "verdict": "pass"}, "unknown attestation kind"),
                         ({"kind": "security-evaluation", "verdict": "maybe"}, "verdict must be"),
                         ({"kind": "release-observation", "verdict": "pass"}, "cannot be attested as pass"),
                         ({"kind": "security-evaluation", "verdict": "not_applicable"}, "waiver_reason"),
                         ({"kind": "security-evaluation", "verdict": "pass", "waiver_reason": REASON},
                          "only accepted with verdict not_applicable"),
                         ({"kind": "security-evaluation", "verdict": "pass"}, "at least one source"),
                         ({"kind": "security-evaluation", "verdict": "pass",
                           "sources": ["file:plan/missing.md@3"]}, "source"),
                         ({"kind": "security-evaluation", "verdict": "pass", "sources": [src],
                           "covers": ["T9"]}, "covers unknown task")]:
        with pytest.raises(WorkflowError, match=msg):
            await f.change_action("attest", **payload)
    res = await f.change_action("attest", kind="independent-baseline", verdict="pass", sources=[src],
                                covers=["T1"])
    assert res.attestation and res.warnings == []


@scenario()
async def test_attestation_warnings(f: Flow):
    tasks = [TASKS[0], TASKS[0] | {"id": "T2", "files_modified": ["src/bye.txt"]}]
    await f.to_build(tasks=tasks)
    src = f.ce.latest_att("plan/spec.md")
    res = await f.change_action("attest", kind="independent-baseline", verdict="pass", sources=[src],
                                covers=["T1"])
    warnings = " ".join(res.warnings)
    assert "partial_coverage" in warnings and "after_execution" in warnings


# ====================================================================== protected paths (R2)
PROTECTED = "tests/acceptance/test_greet.sh"


@scenario()
async def test_protected_acceptance_tests_need_a_human_approval(f: Flow):
    await f.to_h2(tier="R2")              # G-P7: a task may not plan to modify protected paths
    assert f.ce.nodes["oracle"].status == ACCEPTED and f.ce.change.oracle_sha
    await f.human("H-2", "approve")
    out = await f.implement(files={"src/greet.txt": "hello\n", PROTECTED: "true\n"})
    assert any(f"{PROTECTED} is a protected path" in v for v in out.violations)


@scenario()
async def test_approved_protected_path_change_ships_as_human_judgement(f: Flow):
    await f.to_h2(tier="R2")
    with pytest.raises(WorkflowError, match="must be protected acceptance paths"):
        await f.change_action("approve_protected_paths", task="T1", paths=["src/greet.txt"], reason=REASON)
    with pytest.raises(WorkflowError, match="reason"):
        await f.change_action("approve_protected_paths", task="T1", paths=[PROTECTED], reason="x")
    res = await f.change_action("approve_protected_paths", task="T1", paths=[PROTECTED], reason=REASON)
    assert res.attestation and "after now" in res.note
    await f.human("H-2", "approve")
    out = await f.implement(files={"src/greet.txt": "hello\n",
                                   PROTECTED: "grep -q hello src/greet.txt && echo ok\n"})
    assert out.accepted, out
    await f.review()
    await f.integration()                               # G-INT2 accepts the approved change
    assert f.status("integration") == ACCEPTED
    await f.human("H-3", "accept")
    g = f.ce.change.ship.gates["protected_oracle"]
    assert g.status == SAT and g.human_judgement and PROTECTED in g.detail
    assert f.ce.change.status == ChangeStatus.READY


# ====================================================================== surfaces (R2)
@scenario()
async def test_surface_drift_blocks_ship_until_reaffirmed(f: Flow):
    await f.to_build(tier="R2", tasks=[TASKS[0] | {"files_modified": ["src/greet.txt", "README.md"]}])
    assert "files" in f.ce.change.surface_pins                     # pinned at oracle approval
    await f.implement(files={"src/greet.txt": "hello\n", "README.md": "demo v2\n"})
    await f.review()
    await f.integration()
    await f.human("H-3", "accept")
    g = f.ce.change.ship.gates["integration_or_real_interface_checks"]
    assert g.status == UNSAT and "G-SF1" in g.detail and not_ready(f)
    with pytest.raises(WorkflowError, match="unknown surface"):
        await f.change_action("approve_surface", surface="nope", reason=REASON)
    res = await f.change_action("approve_surface", surface="files", reason=REASON)
    assert res.surfaces == ["files"]
    await f.human("H-3", "recheck")
    assert f.ce.change.status == ChangeStatus.READY, f.ce.change.ship.gates


@scenario()
async def test_integration_fail_verdict_and_failing_oracle_commands(f: Flow):
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    out = await f.integration(verdict="FAIL")
    assert not out.accepted and any("G-RV3" in v or "G-INT1" in v for v in out.violations), out
    f.runner.commands["sh tests/acceptance/test_greet.sh"] = (1, "FAIL\n")
    await f.integration(verdict="PASS")                 # the agent says PASS; the controller re-runs
    node = f.ce.nodes["integration"]
    assert node.status == "ESCALATED" and node.escalation.kind == "integration_failed"
    assert f"git:{REPO}@" not in node.escalation.reason


@scenario()
async def test_oracle_command_not_found_escalates_and_retry_reverifies(f: Flow):
    """G-V2 at integration: retry re-runs the oracle commands, no new integration run."""
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    f.runner.commands["sh tests/acceptance/test_greet.sh"] = (127, "sh: not found\n")
    await f.integration(verdict="PASS")
    node = f.ce.nodes["integration"]
    assert node.status == "ESCALATED" and node.escalation.kind == "verify_unrunnable"
    runs = len(f.runs("integration"))
    del f.runner.commands["sh tests/acceptance/test_greet.sh"]
    await f.human("integration", "retry")
    assert node.status == "ACCEPTED" and len(f.runs("integration")) == runs


@scenario()
async def test_higher_tier_gates_added_to_an_r1_change(f: Flow):
    """R2/R3 gates added by override evaluate (unevaluable / unsatisfied / satisfied) on R1."""
    await f.to_build()
    for gate in ("independent_baseline", "architecture_or_security_review", "release_observation_plan",
                 "rollback_or_forward_fix_evidence", "protected_oracle", "protected_acceptance_tests",
                 "integration_or_real_interface_checks", "approved_delta_spec", "approved_spec_and_oracle",
                 "explicit_human_approval"):
        await f.change_action("override", kind="add_gate", gate=gate, reason=REASON)
    await f.implement()
    await f.review()
    g = f.ce.change.ship.gates
    assert not_ready(f)
    for gate in ("independent_baseline", "release_observation_plan", "rollback_or_forward_fix_evidence",
                 "protected_oracle", "protected_acceptance_tests", "integration_or_real_interface_checks",
                 "explicit_human_approval"):
        assert g[gate].status == UNEVAL, (gate, g[gate])
    assert g["architecture_or_security_review"].status == UNSAT
    assert g["approved_delta_spec"].status == SAT and g["approved_spec_and_oracle"].status == SAT

    src = f.ce.latest_att("tasks/T1/evidence/cycle-1.md")
    attest = lambda kind, verdict, **kw: f.change_action("attest", kind=kind, verdict=verdict,
                                                         sources=[src], **kw)
    await attest("independent-baseline", "pass")             # too late: build already started
    await attest("release-observation", "not_applicable", waiver_reason=REASON)
    with pytest.raises(WorkflowError, match="covers must list at least one task"):
        await attest("rollback-evidence", "pass", covers=[])  # not "everything"
    await attest("architecture-review", "fail")
    await f.human("H-3", "recheck")
    g = f.ce.change.ship.gates
    assert g["independent_baseline"].status == UNSAT and "after execution" in g["independent_baseline"].detail
    assert g["release_observation_plan"].status == SAT and "waived" in g["release_observation_plan"].detail
    assert "release_observation_plan" in f.ce.change.ship.waived_gates
    assert g["rollback_or_forward_fix_evidence"].status == UNEVAL
    arch = g["architecture_or_security_review"]
    assert arch.status == UNSAT and "attested fail" in arch.detail
    await attest("rollback-evidence", "pass")
    await attest("architecture-review", "pass")
    await f.human("H-3", "recheck")
    g = f.ce.change.ship.gates
    rollback = g["rollback_or_forward_fix_evidence"]
    assert rollback.status == SAT and rollback.human_judgement
    assert g["architecture_or_security_review"].status == SAT


@scenario()
async def test_an_answer_to_an_integration_failure_reaches_the_next_run(f: Flow):
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    f.runner.commands["sh tests/acceptance/test_greet.sh"] = (1, "FAIL\n")
    await f.integration(verdict="PASS")
    assert f.ce.nodes["integration"].escalation.kind == "integration_failed"
    del f.runner.commands["sh tests/acceptance/test_greet.sh"]
    await f.human("integration", "answer", answer="the greeting test needs LANG=C")
    prompt = f.runner.payloads[f.run("integration").sandbox]["prompt.md"]
    assert "the greeting test needs LANG=C" in prompt


@scenario()
async def test_a_resumed_integration_deletes_the_failed_runs_sandbox(f: Flow):
    f.cfg.retrospective.reflect = True
    await f.to_build(tier="R2")
    await f.implement()
    await f.review()
    f.runner.commands["sh tests/acceptance/test_greet.sh"] = (1, "FAIL\n")
    first = f.run("integration")
    with open(f.ce.run_dir(first.id) / "agent.log", "a") as fh:
        fh.write(json.dumps({"type": "system", "subtype": "init", "session_id": "s-int"}) + "\n")
    await f.integration(verdict="PASS")
    assert f.ce.nodes["integration"].escalation.kind == "integration_failed"
    del f.runner.commands["sh tests/acceptance/test_greet.sh"]
    await f.human("integration", "answer", answer="fixed the environment")
    assert first.sandbox in f.runner.deleted and first.status == "SUPERSEDED"
    assert first.sandbox in [sb for sb, _ in f.runner.reflected]
    assert f.run("integration").sandbox != first.sandbox


@pytest.mark.parametrize("retrospective", [False, True])
def test_an_abandoned_change_leaves_no_sandbox(tmp_path, retrospective):
    """Abandoning deletes every sandbox of the change's work; only the retrospective it starts
    (unless asked not to) has one."""
    @scenario()
    async def go(f: Flow):
        await f.to_build(tier="R2")
        await f.implement()
        await f.review()
        f.runner.commands["sh tests/acceptance/test_greet.sh"] = (1, "FAIL\n")
        await f.integration(verdict="PASS")
        await f.human("integration", "abandon", retrospective=retrospective)
        await f.settle()
        left = {r.sandbox: r.role for r in f.ce.runs.values() if r.sandbox and not r.sandbox_deleted}
        assert set(left.values()) == ({"retrospective"} if retrospective else set()), left
    go(tmp_path)


# ====================================================================== baseline (R3)
GREET = "sh tests/acceptance/test_greet.sh"


async def _baseline(f: Flow, reported: int) -> None:
    r = f.run("baseline")
    doc = await f.write(r, "baseline/report.md", "results before any build")
    out = await f.report(r, [f"git:{REPO}@{r.judged_sha}", doc], {"results": [
        {"command": c, "exit_code": reported} for c in f.ce.baseline_commands()]})
    assert out.accepted, out
    await f.settle()


def _oracle_warnings(f: Flow) -> list[str]:
    from legion_orch.workflow import governance
    return next(s for s in governance.h2_status(f.ce) if s.subject == "oracle").warnings


@scenario()
async def test_the_baseline_flags_an_acceptance_test_that_already_passes(f: Flow):
    await f.to_h2(tier="R3")
    await _baseline(f, reported=0)                       # the controller's run passes too
    assert f.status("baseline") == ACCEPTED
    assert f.ce.nodes["baseline"].verification["baseline"].results
    w = _oracle_warnings(f)
    assert len(w) == 1 and "already passes" in w[0] and GREET in w[0], w


@scenario()
async def test_the_baseline_flags_results_that_differ_from_the_controllers(f: Flow):
    f.runner.commands[GREET] = (1, "FAIL\n")             # fails before the build (preflight too)
    await f.to_h2(tier="R3")
    await _baseline(f, reported=0)                       # but the agent says it passed
    assert f.status("baseline") == ACCEPTED              # failures are expected: no rework
    w = _oracle_warnings(f)
    assert len(w) == 1 and "reported" in w[0] and "exit 1" in w[0], w


@scenario()
async def test_a_failing_acceptance_test_at_the_baseline_raises_nothing(f: Flow):
    f.runner.commands[GREET] = (1, "FAIL\n")             # fails before the build (preflight too)
    await f.to_h2(tier="R3")
    await _baseline(f, reported=1)
    assert f.status("baseline") == ACCEPTED and _oracle_warnings(f) == []


@scenario()
async def test_the_integration_verifier_is_given_the_baseline(f: Flow):
    from legion_orch.workflow.roles import handler_for
    f.runner.commands[GREET] = (1, "FAIL\n")             # fails before the build (preflight too)
    await f.to_h2(tier="R3")
    await _baseline(f, reported=1)
    refs = handler_for("integration").extra_context(f.ce, f.ce.nodes["integration"])
    ver = f.ce.nodes["baseline"].verification["baseline"].attestation
    assert f.ce.latest_att("baseline/report.md") in refs and ver in refs, refs


@scenario()
async def test_h3_attests_the_gates_only_a_human_can_satisfy_with_the_acceptance(f: Flow):
    await f.to_build(tier="R2")
    for gate in ("security_or_e2e_evaluator", "rollback_or_forward_fix_evidence"):
        await f.change_action("override", kind="add_gate", gate=gate, reason=REASON)
    await f.implement()
    await f.review()
    await f.integration()
    assert f.status("H-3") == "AWAITING_HUMAN"
    rows = {r.gate: r for r in governance.h3_attestations(f.ce)}
    sec, rb = rows["security_or_e2e_evaluator"], rows["rollback_or_forward_fix_evidence"]
    assert sec.status == rb.status == "pending" and sec.kinds == ["security-evaluation", "e2e-evaluation"]
    labels = [s.label for s in sec.suggested]
    assert labels == ["integration report", "controller's oracle run at the head"]
    assert rb.suggested == []                                   # no release stage at R2
    report = f.ce.latest_att("integration/report.md")
    assert rb.waivable and sec.waivable and rb.waiver_sources == sec.waiver_sources == [report]
    with pytest.raises(WorkflowError, match="not needed"):
        await f.human("H-3", "accept", attestations=[{"kind": "independent-baseline", "sources": []}])
    with pytest.raises(WorkflowError, match="G-AT1"):
        await f.human("H-3", "accept", attestations=[{"kind": "e2e-evaluation", "sources": []}])
    assert f.status("H-3") == "AWAITING_HUMAN"
    res = await f.human("H-3", "accept", attestations=[
        {"kind": "e2e-evaluation", "sources": [s.att for s in sec.suggested]}])
    assert [w.split()[0] for w in res.warnings] == ["rollback_or_forward_fix_evidence"]
    g = f.ce.change.ship.gates
    assert g["security_or_e2e_evaluator"].status == SAT and g["security_or_e2e_evaluator"].human_judgement
    assert g["rollback_or_forward_fix_evidence"].status == UNEVAL
    rows = {r.gate: r for r in governance.h3_attestations(f.ce)}
    assert rows["security_or_e2e_evaluator"].status == "attested"
    assert rows["security_or_e2e_evaluator"].kind == "e2e-evaluation"
    # not ready: H-3 waits on its preconditions, and needs you until a recheck finds them met
    assert not_ready(f)
    [item] = [i for i in attention.items(f.ce) if i.blocking]
    assert item.kind == "not_ready" and item.node == "H-3"
    assert item.detail == "rollback_or_forward_fix_evidence" and "1 gate not" in item.title
    assert not [i for i in attention.items(f.ce) if i.kind == "change_ended"]
    await f.change_action("attest", kind="rollback-evidence", verdict="pass",
                          sources=[f.ce.latest_att("integration/report.md")])
    await f.human("H-3", "recheck")
    assert f.status("H-3") == ACCEPTED and f.ce.change.status == ChangeStatus.READY
    assert [i.kind for i in attention.items(f.ce)] == ["change_ended"]
    assert any("resolved" in e["message"] and "Not ready" in e["message"]
               for e in f.ce.timeline_events() if e["kind"] == "attention")


def test_recovery_strategy_is_read_from_the_rollback_plan():
    from legion_orch.workflow.guards import recovery_strategy, release_docs
    assert recovery_strategy("Strategy: rollback\nRationale: no data changes\n") == \
        ("rollback", "no data changes")
    assert recovery_strategy("# Plan\n\n**Strategy:** Forward fix\n**Rationale:** drops a column")[0] \
        == "forward-fix"
    assert recovery_strategy("revert to abc") == (None, "")
    plan = "## Health criteria\nok\n## Rollback criteria\nbad\n"
    assert release_docs(plan, "Strategy: rollback\nRevert to abc123", "abc123") == []
    [v] = release_docs(plan, "Revert to abc123", "abc123")
    assert v.startswith("G-REL1") and "Strategy: rollback" in v


@scenario()
async def test_h3_shows_the_recommended_recovery_and_e2e_can_be_not_applicable(f: Flow):
    await f.to_build(tier="R2")
    for gate in ("security_or_e2e_evaluator", "rollback_or_forward_fix_evidence"):
        await f.change_action("override", kind="add_gate", gate=gate, reason=REASON)
    await f.implement()
    await f.review()
    await f.integration()
    f.ce.write_record("release/rollback.md", "Strategy: forward-fix\nRationale: drops a column\n"
                                             f"Revert to {f.ce.change.base_sha}\n")
    rows = {r.gate: r for r in governance.h3_attestations(f.ce)}
    rb, sec = rows["rollback_or_forward_fix_evidence"], rows["security_or_e2e_evaluator"]
    assert (rb.recommended, rb.rationale) == ("forward-fix-evidence", "drops a column")
    assert sec.waivable and not rb.waivable
    report = f.ce.latest_att("integration/report.md")
    with pytest.raises(WorkflowError, match="only be attested pass"):
        await f.human("H-3", "accept", attestations=[
            {"kind": "forward-fix-evidence", "verdict": "not_applicable", "sources": [report],
             "waiver_reason": REASON}])
    with pytest.raises(WorkflowError, match="waiver_reason"):
        await f.human("H-3", "accept", attestations=[
            {"kind": "e2e-evaluation", "verdict": "not_applicable", "sources": [report]}])
    res = await f.human("H-3", "accept", attestations=[
        {"kind": "e2e-evaluation", "verdict": "not_applicable", "sources": [report],
         "waiver_reason": REASON},
        {"kind": "forward-fix-evidence", "sources": [f.ce.latest_att("release/rollback.md")]}])
    assert res.warnings == []
    g = f.ce.change.ship.gates
    sec = g["security_or_e2e_evaluator"]
    assert sec.status == SAT and "not_applicable" in sec.detail
    assert g["rollback_or_forward_fix_evidence"].status == SAT
    assert "security_or_e2e_evaluator" in f.ce.change.ship.waived_gates
    assert f.ce.change.status == ChangeStatus.READY
