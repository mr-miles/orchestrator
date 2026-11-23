"""The engine on tickets, end to end: a real controller, store and hub; agents and decision
owners played through the engine's API (tests/engine_harness.py)."""
from __future__ import annotations

from engine_harness import Flow, scenario, signals


@scenario()
async def test_a_change_runs_from_intake_to_publication(f: Flow):
    await f.to_end()
    assert f.ce.change.status == "READY", f.status()
    assert all(t.status == "passed" for t in f.ce.st.tickets.values()
               if t.id != "scaffold" and t.parent != "scaffold"), f.status()
    assert f.status("scaffold") == "cancelled"          # the repository has its tooling
    assert f.ce.change.tier == "R1"
    ptr = f.ce.tickets.root.pointers
    assert {"requirements", "risk", "spec", "oracle", "environment", "tickets", "pr",
            "head"} <= set(ptr)
    import subprocess
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=f.git.repo,
                           capture_output=True, text=True, check=True).stdout.split()
    assert "src/greet.txt" in files                    # published: merged into the base branch
    assert f.runner.deleted                             # closed tickets' sandboxes went


def files_at(f: Flow, ref: str) -> list[str]:
    import subprocess
    return subprocess.run(["git", "ls-tree", "-r", "--name-only", ref], cwd=f.git.repo,
                          capture_output=True, text=True, check=True).stdout.split()


R2 = signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2)   # total 6


@scenario()
async def test_at_r2_the_oracle_is_signed_off_before_any_build(f: Flow):
    await f.intake(R2)
    await f.signoff("H-1")
    assert f.ce.change.tier == "R2" and "oracle" in f.ce.st.tickets   # linked by H-1's tier
    await f.plan()
    await f.critique()
    await f.signoff("H-E1")
    assert f.status("T1") == "pending"                 # the builds wait for acceptance
    await f.oracle()
    assert f.status("preflight") == "passed"           # the oracle's commands, verified
    await f.signoff("H-2")
    assert "tests/acceptance/test_greet.sh" in files_at(f, f"legion/{f.ce.cid}/integration")
    r = f.run("T1", "implementer")                      # the build starts at the oracle's merge
    assert r.base_sha == f.ce.tickets.t["change"].sha


@scenario()
async def test_a_failed_review_sends_the_build_back_with_its_findings(f: Flow):
    await f.to_build()
    await f.implement("T1")
    await f.review("T1", "FAIL", title="greet.txt lacks a newline")
    t = f.ticket("T1")
    assert t.attempt == 2 and any("greet.txt lacks a newline" in x for x in t.findings)
    r = f.run("T1", "implementer")
    assert any("greet.txt lacks a newline" in x for x in r.assignment["findings"])
    assert r.sandbox == f.runs("T1")[0].sandbox           # its sandbox and session kept
    await f.implement("T1")
    await f.review("T1")
    assert f.status("T1") == "passed"


@scenario()
async def test_a_failed_verification_sends_the_build_back(f: Flow):
    await f.to_build()
    f.runner.commands["grep -q hello src/greet.txt"] = (1, "no hello\n")
    await f.implement("T1")
    t = f.ticket("T1")
    assert t.attempt == 2 and any("BLOCKER" in x and "grep -q hello" in x for x in t.findings)
    assert "review:T1" not in [r.ticket for r in f.ce.runs.values()]   # judged code only


@scenario()
async def test_a_rejected_plan_reworks_the_planner(f: Flow):
    await f.intake()
    await f.signoff("H-1")
    await f.plan()
    await f.critique()
    await f.signoff("H-E1", approve=False, reason="split the greeting into two tasks")
    t = f.ticket("design")
    assert t.attempt == 2 and "split the greeting into two tasks" in t.findings
    assert f.ce.tickets.root.pointers.get("tickets") is None        # nothing was applied


@scenario()
async def test_a_blocked_report_waits_on_a_human_without_an_attempt(f: Flow):
    await f.to_build()
    r = f.run("T1", "implementer")
    await f.report(r, status="BLOCKED", blocked_reason="which greeting?",
                   missing_decision="the greeting's wording")
    t = f.ticket("T1")
    assert t.status == "waiting" and t.waiting == "blocked" and t.attempt == 1
    assert "which greeting?" in t.findings
    await f.human("T1", "retry")
    assert f.status("T1") == "working" and f.ticket("T1").attempt == 1
    await f.implement("T1")
    await f.review("T1")
    assert f.status("T1") == "passed"


@scenario()
async def test_a_restart_runs_the_build_again_at_the_same_attempt(f: Flow):
    await f.to_build()
    first = f.run("T1", "implementer")
    ctl, ce = await f.restart(recover=True)
    f.ctl, f.ce = ctl, ce
    await f.settle()
    again = f.run("T1", "implementer")
    assert again.id != first.id and again.attempt == 1 == f.ticket("T1").attempt
    assert again.sandbox == first.sandbox               # its kept sandbox: the session goes on
    await f.implement("T1")
    await f.review("T1")
    assert f.status("T1") == "passed"


@scenario()
async def test_an_operator_pause_drains_then_resume_goes_on(f: Flow):
    await f.to_build()
    await f.change_action("pause")
    await f.implement("T1")                             # concludes; its criteria wait
    assert f.status("T1") == "checking" and not f.runs("verify:T1")
    assert any(i.kind == "paused" for i in f.ce.attention_items)
    await f.change_action("resume")
    await f.review("T1")
    assert f.status("T1") == "passed"


@scenario()
async def test_abandoning_stops_the_work(f: Flow):
    await f.to_build()
    await f.change_action("abandon", reason="not needed")
    assert f.ce.change.status == "ABANDONED"
    assert f.status("T1") == "cancelled"
