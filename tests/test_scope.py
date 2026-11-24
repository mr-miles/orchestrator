"""A build beyond its files (G-B3, WORKFLOW.md §5.8): it may change a file outside its
``files_modified`` – or in ``files_forbidden``, or a protected path other than the oracle's
tests – with a reason in its report; its reviewer judges the reasons (a protected path
strictly). At R0 the reviewer judges only that, and passes at once when there is nothing."""
from __future__ import annotations

from engine_harness import Flow, scenario, signals

R0 = signals(scope_breadth=0, novelty_uncertainty=0, verification_quality=0)
WHY = "the greeting needs the shared formatter's new option"


def files(f: Flow, task: str = "T1", **extra: str) -> dict[str, str]:
    work = f.ticket(task).work
    return {p: f"hello from {task}\n" for p in work["files_modified"]} | extra


@scenario()
async def test_a_change_beyond_the_files_needs_a_reason_which_the_reviewer_judges(f: Flow):
    await f.to_build()                                     # R1: a full review
    out = await f.implement(files=files(f, **{"src/format.txt": "option\n"}))
    assert not out.accepted
    assert any("src/format.txt is outside your files_modified: revert it, or list it in "
               "data.beyond_scope" in v for v in out.violations), out.violations
    out = await f.implement(files=files(f, **{"src/format.txt": "option 2\n"}),
                            data={"beyond_scope": [{"path": "src/format.txt", "reason": WHY}]})
    assert out.accepted, out.violations
    review = f.run("review:T1", "reviewer")
    prompt = review.template_vars
    assert f"`src/format.txt` (outside its files_modified): {WHY}" in prompt["beyond_scope"]
    assert prompt["review_scope"].startswith("the whole build")
    await f.review("T1", verdict="FAIL", title="the formatter change is another build's work")
    assert f.ticket("T1").attempt == 2                     # sent back: a normal rework


@scenario()
async def test_the_oracles_tests_stay_out_of_reach_whatever_the_reason(f: Flow):
    await f.to_build(signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2))
    test = "tests/acceptance/test_greet.sh"
    out = await f.implement(files=files(f, **{test: "true\n"}),
                            data={"beyond_scope": [{"path": test, "reason": "it is wrong"}]})
    assert not out.accepted
    assert any(f"{test} is a protected path: one of the oracle's tests" in v
               for v in out.violations), out.violations


@scenario()
async def test_a_protected_path_is_reviewed_strictly(f: Flow):
    f.cfg.protected_paths = ["config/"]
    await f.to_build()
    out = await f.implement(files=files(f, **{"config/app.ini": "x=1\n"}))
    assert any("strictly: a protected path" in v for v in out.violations), out.violations
    out = await f.implement(files=files(f, **{"config/app.ini": "x=2\n"}),
                            data={"beyond_scope": [{"path": "config/app.ini", "reason": WHY}]})
    assert out.accepted, out.violations
    assert "`config/app.ini` (protected)" in f.run("review:T1", "reviewer").template_vars[
        "beyond_scope"]


@scenario()
async def test_at_r0_the_review_judges_only_what_is_beyond_and_skips_when_nothing_is(f: Flow):
    await f.to_build(R0)
    assert f.ce.change.tier == "R0" and f.ticket("review:T1").scope_only
    await f.implement()                                    # inside its files
    await f.settle()
    assert f.status("review:T1") == "passed" and not f.runs("review:T1")   # nothing to judge
    assert "nothing beyond its scope" in f.ce.timeline()


@scenario()
async def test_at_r0_a_change_beyond_the_files_gets_a_scope_only_review(f: Flow):
    await f.to_build(R0)
    out = await f.implement(files=files(f, **{"src/format.txt": "option\n"}),
                            data={"beyond_scope": [{"path": "src/format.txt", "reason": WHY}]})
    assert out.accepted, out.violations
    review = f.run("review:T1", "reviewer")
    assert review.template_vars["review_scope"].startswith("only the changes above")
    await f.review("T1")
    await f.settle()
    assert f.status("T1") == "passed"
