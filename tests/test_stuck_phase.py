"""A phase's work that raises does not strand its step.

Before: ``ActiveStep.run`` caught only ``Withdrawn``; any other exception from a phase's
``perform``/``result`` went to ``Controller.spawn``, which logged it as an "internal error"
event – and nothing else. The node stayed in its phase's status (VERIFYING) with nothing
running and no action offered until the controller restarted. Now the step escalates
(``internal_error``) and a retry resumes in the phase that failed."""
from __future__ import annotations

from engine_harness import Flow, scenario

from legion_orch.sandbox.names import VERIFIER_PREFIX
from legion_orch.workflow.model import ESCALATED, REVIEWING, FailureSource, Resume


@scenario()
async def test_a_verification_that_raises_escalates_and_a_retry_verifies_again(f: Flow):
    await f.to_build()
    real_write = f.ce.write_record
    fail = [True]

    def write_record(rel, content):              # one ordinary failure in Verify.result
        if fail[0] and rel.endswith("verification.json"):
            raise OSError("disk full")
        return real_write(rel, content)
    f.ce.write_record = write_record

    await f.implement()                          # T1 reports; its verification raises
    node = f.ce.nodes["build:T1"]
    verifiers = [s for s in f.runner.created if s.startswith(VERIFIER_PREFIX)]
    assert len(verifiers) == 1 and set(verifiers) <= set(f.runner.deleted)   # nothing left
    assert node.status == ESCALATED, node.status
    esc = node.escalation
    assert esc.kind == "internal_error" and "Verify failed: OSError: disk full" in esc.reason
    assert esc.resume == Resume.VERIFYING and esc.source == FailureSource.CONTROLLER
    assert {"retry", "abandon"} <= set(f.ce._flow.actions("build:T1"))

    fail[0] = False                              # fixed: a retry verifies again
    implementers = len(f.runs("build:T1", "implementer"))
    await f.human("build:T1", "retry")
    assert len([s for s in f.runner.created if s.startswith(VERIFIER_PREFIX)]) == 2
    assert len(f.runs("build:T1", "implementer")) == implementers      # not built again
    assert f.status("build:T1") == REVIEWING and node.current_verification().ok


@scenario()
async def test_a_merge_that_raises_escalates_and_a_retry_merges(f: Flow):
    await f.to_build()
    await f.implement()
    real_merge = f.git.merge
    fail = [True]

    async def merge(*a, **kw):
        if fail[0]:
            raise RuntimeError("git merge: fatal: index.lock exists")
        return await real_merge(*a, **kw)
    f.git.merge = merge
    await f.review(verdict="PASS", describe=False)
    node = f.ce.nodes["build:T1"]
    assert node.status == ESCALATED and node.escalation.kind == "internal_error"
    assert node.escalation.resume == Resume.MERGING and "index.lock" in node.escalation.reason
    fail[0] = False
    await f.human("build:T1", "retry")
    assert f.status("build:T1") == "ACCEPTED"


@scenario()
async def test_a_phase_that_concludes_nothing_escalates_instead_of_waiting_forever(f: Flow):
    """A live phase whose result is None (e.g. a judge's turn ended with neither a review nor
    an escalation) has nothing left to take its work up: it is the controller's failure."""
    from legion_orch.workflow.engine.steps.phases import Panel
    await f.to_build()
    await f.implement()
    real = Panel._decide
    calls = [0]

    def decide(self, active):
        calls[0] += 1
        return None if calls[0] == 1 else real(self, active)
    Panel._decide = decide
    try:
        await f.review(verdict="PASS", describe=False)
        node = f.ce.nodes["build:T1"]
        assert node.status == ESCALATED and node.escalation.kind == "internal_error"
        assert "Panel concluded nothing" in node.escalation.reason
        assert node.escalation.resume == Resume.REVIEWING
        await f.human("build:T1", "retry")      # the reviews stand: the panel decides
        assert f.status("build:T1") == "ACCEPTED"
    finally:
        Panel._decide = real
