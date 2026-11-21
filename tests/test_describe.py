"""The pull request description, before H-3: the describer writes it at the integration head
(G-PR1), the PR reviewer judges it faithful (G-PR2) within a budget (G-PR3), and H-3's ship gate
``pr_description_verified`` holds only for a PASS at the head."""
from __future__ import annotations

from engine_harness import Flow, scenario

from legion_orch.workflow import guards
from legion_orch.workflow.model import (
    ACCEPTED,
    ESCALATED,
    REVIEWING,
    RUNNING,
    ChangeStatus,
    GateStatus,
)


async def built(f: Flow) -> None:
    """An R1 change with its task built and reviewed: the describe step runs next."""
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    await f.implement()
    await f.review(verdict="PASS", describe=False)


def test_the_description_guard_wants_a_title_the_sections_and_every_requirement():
    assert guards.pr_description(Flow.PR, ["REQ-1"]) == []
    bad = guards.pr_description("Greet\n\n## Changes\n\nx\n", ["REQ-1", "REQ-2"])
    assert bad == ["G-PR1: the first line is the PR's title: '# <title>'",
                   "G-PR1: section '## Motivation' is missing",
                   "G-PR1: section '## Verification' is missing",
                   "G-PR1: requirement(s) ['REQ-1', 'REQ-2'] are not cited"]
    assert "at most 120" in guards.pr_description("# " + "x" * 121, [])[0]
    assert guards.pr_title(Flow.PR) == "Greet the user"
    assert guards.pr_body(Flow.PR).startswith("## Motivation") and "# Greet" not in guards.pr_body(
        Flow.PR)


@scenario()
async def test_a_described_and_reviewed_change_is_ready(f: Flow):
    await built(f)
    assert f.status("describe") == RUNNING and f.ce.change.status == ChangeStatus.ACTIVE
    describer = f.run("describe", "describer")
    assert describer.judged_sha == f.ce.change.integration_head            # at the head
    assert describer.template_vars["change_base_sha"] == f.ce.change.base_sha
    await f.describe(review=None)
    assert f.status("describe") == REVIEWING
    reviewer = f.run("describe", "pr-reviewer")
    assert reviewer.judged_sha == f.ce.change.integration_head
    assert f.ce.latest_att("delivery/pr.md") in reviewer.context_refs
    await f.pr_review("PASS")
    assert f.status("describe") == ACCEPTED and f.ce.change.status == ChangeStatus.READY
    gate = f.ce.change.ship.gates["pr_description_verified"]
    assert gate.status == GateStatus.SATISFIED and "delivery/pr.md" in gate.detail


@scenario()
async def test_a_description_missing_a_section_is_rejected(f: Flow):
    await built(f)
    out = await f.describe("# Greet\n\n## Changes\n\nREQ-1\n", review=None)
    assert not out.accepted
    assert any("'## Motivation' is missing" in v for v in out.violations)
    assert f.status("describe") == RUNNING                    # the describer reports again


@scenario(critique_max_cycles=2)
async def test_rework_goes_back_to_the_describer_then_the_budget_escalates(f: Flow):
    await built(f)
    await f.describe(review="REWORK")
    node = f.ce.nodes["describe"]
    assert f.status("describe") == RUNNING and node.cycle == 2
    again = f.run("describe", "describer")
    assert "the motivation is not REQ-1" in again.template_vars["feedback"]
    gate = f.ce.change.ship.gates["pr_description_verified"] if f.ce.change.ship else None
    assert gate is None or gate.status != GateStatus.SATISFIED
    await f.describe(review="REWORK")                         # the second cycle: the budget
    assert f.status("describe") == ESCALATED
    esc = f.ce.nodes["describe"].escalation
    assert esc.kind == "critique_budget" and esc.reason.startswith("G-PR3: 2 pr-reviewer cycles")


@scenario()
async def test_r3_has_no_release_step_and_the_release_gates_are_only_addable(f: Flow):
    from legion_orch.workflow import risk
    from legion_orch.workflow.engine.runtime.workflow_tree import build_workflow
    from legion_orch.workflow.flow import walk
    wf = build_workflow(3, environment=False, blank_repo=False)
    ids = [n.id for s in wf.stages for n in walk(s)]
    assert "release" not in ids and ids[-2:] == ["describe", "H-3"]
    assert not set(risk.RELEASE_GATES) & {g for gs in risk.GATES_BY_TIER.values() for g in gs}
    assert all("pr_description_verified" in gs for gs in risk.GATES_BY_TIER.values())
    from legion_orch.workflow.governance.overrides import ALL_GATES
    assert set(risk.RELEASE_GATES) <= ALL_GATES                       # still addable
