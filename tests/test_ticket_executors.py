"""The ticket executors (legion_orch/workflow/tickets/executors): each outcome as its rules event,
the backoff for system errors, each executor's mapping of its service's result, and the branches
that follow the tree – checks, merges and conflicts in a real bare hub."""
from __future__ import annotations

import asyncio
import subprocess

import pytest
from ticket_harness import commit

from legion_orch.git.branches import Branches, ticket_branch
from legion_orch.git.remote import GitRemote
from legion_orch.workflow.model import CommandResult, FailureSource, Verdict, Verification
from legion_orch.workflow.tickets import Merge, Severity, StartWork, Status, Ticket, Tickets, Waiting
from legion_orch.workflow.tickets.executors import (
    AgentEnd,
    AgentExecutor,
    Attempts,
    Backoff,
    CanMergeExecutor,
    Decision,
    Done,
    Executors,
    Fault,
    GaveUp,
    Judged,
    Merger,
    OverBudget,
    PublishExecutor,
    SignoffExecutor,
    VerifyExecutor,
    Work,
    agent_outcome,
    apply,
    attempt,
    merge_outcome,
    verification_outcome,
)

pytestmark = pytest.mark.anyio
NOW = Backoff(tries=3, first=0, cap=0)          # retries without waiting


def T(id, parent=None, **kw) -> Ticket:
    role = "root" if parent is None else kw.pop("role", "subtask")
    return Ticket(id=id, parent=parent, role=role, **kw)


def tickets() -> Tickets:
    return Tickets([
        T("c", subtasks=[["T1", "T2"]]),
        T("T1", "c", type="agent:implementer", criteria=["rev"]),
        T("rev", "T1", role="criterion", type="agent:reviewer", severity=Severity.REWORK),
        T("T2", "c", type="agent:implementer"),
    ])


def work(ticket: Ticket, **kw) -> Work:
    return Work(ticket=ticket, attempt=1, **kw)


# ------------------------------------------------------------------------------- apply
def test_each_outcome_is_its_rules_event():
    t = tickets()
    t.start()
    assert apply(t, "T1", Done("a" * 40)) == [StartWork("rev", 1)]
    assert apply(t, "rev", Judged(False, ("HIGH: x",))) == [StartWork("T1", 2, ("HIGH: x",))]
    assert apply(t, "T1", GaveUp(("stuck",))) == [StartWork("T1", 3, ("stuck",))]
    apply(t, "T1", Fault("sandbox"))
    assert t.t["T1"].waiting == Waiting.SYSTEM and t.t["T1"].attempt == 3      # not an attempt
    apply(t, "T2", OverBudget("timeout"))
    assert t.t["T2"].waiting == Waiting.BUDGET and t.t["T2"].attempt == 1
    t2 = tickets()
    t2.start()
    apply(t2, "T1", Done("a" * 40))
    assert apply(t2, "rev", Done()) == [Merge("T1", "a" * 40, "c")]          # a criterion done: a pass
    apply(t2, "T2", Judged(True))                                            # not a criterion: a bug
    assert t2.t["T2"].waiting == Waiting.SYSTEM


def test_executors_by_type_then_family():
    agent, verify = object(), object()
    ex = Executors({"agent": agent, "controller:verify": verify})
    assert ex("agent:reviewer") is agent and ex("controller:verify") is verify
    with pytest.raises(KeyError):
        ex("controller:publish")
    with pytest.raises(KeyError):
        ex("grouping")


# ------------------------------------------------------------------------------- backoff
async def test_transient_faults_are_retried_with_backoff_then_returned():
    waits: list[float] = []

    async def sleep(d):
        waits.append(d)
    outcomes = iter([Fault("a"), RuntimeError("boom"), Done("s")])

    async def work_():
        out = next(outcomes)
        if isinstance(out, Exception):
            raise out
        return out
    seen = Attempts()
    assert await attempt(work_, Backoff(tries=4, first=1, factor=3, cap=5), sleep=sleep,
                         seen=seen) == Done("s")
    assert waits == [1, 3] and seen.faults == ["a", "RuntimeError: boom"]

    async def always():
        return Fault("down")
    waits.clear()
    assert await attempt(always, Backoff(tries=3, first=1), sleep=sleep) == Fault("down")
    assert waits == [1, 2]                                  # three tries, then the fault

    async def fixable():
        return Fault("G-V2", transient=False)
    waits.clear()
    assert (await attempt(fixable, sleep=sleep)).detail == "G-V2" and waits == []


# ------------------------------------------------------------------------------- agents
def test_an_agent_runs_end_as_its_kind_of_outcome():
    sha = "a" * 40
    assert agent_outcome(AgentEnd(True, sha), criterion=False) == Done(sha)
    # a ticket's own work that judges (integration) and fails: not done – an attempt
    assert agent_outcome(AgentEnd(True, sha, verdict=Verdict.FAIL, findings=("[BLOCKER] x",)),
                         criterion=False) == GaveUp(("[BLOCKER] x",))
    assert agent_outcome(AgentEnd(True, sha, verdict=Verdict.PASS), criterion=False) == Done(sha)
    assert agent_outcome(AgentEnd(True, verdict=Verdict.PASS), criterion=True) == Judged(True)
    assert agent_outcome(AgentEnd(True, verdict=Verdict.REWORK, findings=("HIGH: x",)),
                         criterion=True) == Judged(False, ("HIGH: x",))
    assert agent_outcome(AgentEnd(False, failure=FailureSource.AGENT, detail="gave up"),
                         criterion=False) == GaveUp(("gave up",))
    assert isinstance(agent_outcome(AgentEnd(False, failure=FailureSource.SYSTEM), False), Fault)
    assert isinstance(agent_outcome(AgentEnd(False, failure=FailureSource.CONTROLLER), False), Fault)
    assert isinstance(agent_outcome(AgentEnd(False, failure=FailureSource.TIMEOUT), False),
                      OverBudget)
    assert agent_outcome(AgentEnd(False), criterion=False) == GaveUp()      # a BLOCKED report


async def test_an_agent_executor_retries_a_system_failure_but_not_a_timeout():
    class Port:
        def __init__(self, ends):
            self.ends, self.calls = iter(ends), []

        async def run(self, role, w):
            self.calls.append(role)
            return next(self.ends)

        async def stop(self, ticket):
            pass
    port = Port([AgentEnd(False, failure=FailureSource.SYSTEM), AgentEnd(True, "b" * 40)])
    x = T("T1", "c", type="agent:implementer")
    assert await AgentExecutor(port, NOW).run(work(x)) == Done("b" * 40)
    assert port.calls == ["implementer", "implementer"]
    port = Port([AgentEnd(False, failure=FailureSource.TIMEOUT)])
    assert isinstance(await AgentExecutor(port, NOW).run(work(x)), OverBudget)
    port = Port([AgentEnd(True)])
    await AgentExecutor(port, NOW, role="resolver").run(work(x))
    assert port.calls == ["resolver"]


# ------------------------------------------------------------------------------- verification
def test_a_verification_means_pass_fail_or_a_fault():
    sha = "a" * 40
    assert verification_outcome(Verification(sha=sha, ok=True, results=[
        CommandResult(command="t", exit_code=0)])) == Judged(True)
    failed = verification_outcome(Verification(sha=sha, results=[
        CommandResult(command="mise run setup", exit_code=1, output="no", setup=True),
        CommandResult(command="pytest", exit_code=2, output="x" * 2000)]))
    assert isinstance(failed, Judged) and not failed.passed
    assert failed.findings[0].startswith("BLOCKER: the setup `mise run setup` exited 1")
    assert len(failed.findings[1]) < 900                                # the output's tail
    infra = verification_outcome(Verification(sha=sha, infra_error="gateway"))
    assert isinstance(infra, Fault) and infra.transient
    unrunnable = verification_outcome(Verification(sha=sha, results=[
        CommandResult(command="cargo test", exit_code=127)]))
    assert isinstance(unrunnable, Fault) and not unrunnable.transient
    assert "G-V2" in unrunnable.detail


async def test_verification_judges_the_owners_commit():
    class Port:
        seen = None

        def commands(self, w):
            return ("pytest",)

        async def verify(self, w, sha, commands):
            Port.seen = (sha, commands)
            return Verification(sha=sha, ok=True, results=[CommandResult(command="pytest",
                                                                         exit_code=0)])

        async def stop(self, ticket):
            pass
    c = T("ok", "T1", role="criterion", type="controller:verify")
    assert await VerifyExecutor(Port(), NOW).run(work(c, judged="a" * 40)) == Judged(True)
    assert Port.seen == ("a" * 40, ("pytest",))
    assert isinstance(await VerifyExecutor(Port(), NOW).run(work(c)), Fault)  # nothing to judge


# ------------------------------------------------------------------------------- sign-off
class Owner:
    """A decision owner: their decisions in turn, and what they were asked."""

    def __init__(self, *decisions):
        self.decisions, self.asked = iter(decisions), []

    async def decide(self, w, subjects):
        self.asked.append(dict(subjects))
        return next(self.decisions)

    async def stop(self, ticket):
        pass


async def test_a_signoff_approves_exactly_the_subjects_it_was_shown():
    h = T("H-E1", "design", role="criterion", type="human:signoff", signs=["spec", "head"])
    shown = {"spec": "file:tickets/design/spec.md@3", "head": "git:" + "a" * 40}
    owner = Owner(Decision(True))
    assert await SignoffExecutor(owner, NOW).run(work(h, subjects=shown)) == \
        Judged(True, signed=shown)                         # what the pointers move to
    assert owner.asked == [shown]
    rejected = await SignoffExecutor(Owner(Decision(False, "too broad")), NOW).run(
        work(h, subjects=shown))
    assert rejected == Judged(False, ("too broad",)) and rejected.signed is None
    unresolved = await SignoffExecutor(Owner(), NOW).run(work(h))     # nobody produced it
    assert isinstance(unresolved, Fault) and not unresolved.transient


# ------------------------------------------------------------------------------- branches
def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
async def hub(tmp_path):
    hub = GitRemote(tmp_path / "git", "app", None, "main")
    await hub.ensure()
    return hub


def test_ticket_branches_are_valid_refs():
    for tid in ("rev:T1", "plan.2", "a..b", "x y", "T~1^2", "build:T1.lock"):
        name = ticket_branch("c1", tid)
        subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"], check=True)
    assert ticket_branch("c1", "T1") == "legion/c1/t/T1"


async def test_merges_follow_the_tree_and_a_conflict_is_a_rework(hub):
    br = Branches(hub)
    base = await hub.resolve_base()
    parent = ticket_branch("c1", "delivery")
    assert await br.create(parent, base) == base
    assert await br.create(parent, "0" * 39 + "1") == base        # it exists: kept
    t1 = commit(hub, base, {"a.txt": "one\n"}, "t1")
    t2 = commit(hub, base, {"a.txt": "two\n"}, "t2")       # the same file, otherwise
    t3 = commit(hub, base, {"b.txt": "three\n"}, "t3")
    assert (await br.can_merge(t1, parent)).clean and (await br.can_merge(t2, parent)).clean

    merged = await br.merge(t1, parent, "legion c1: T1")
    head = await br.head(parent)
    assert merged.clean and merged.head == head
    assert _git("rev-list", "--parents", "-n1", head, cwd=hub.repo).split()[1:] == [base, t1]
    again = await br.merge(t1, parent, "again")                     # merged already: nothing
    assert again.merged and await br.head(parent) == head

    conflict = await br.can_merge(t2, parent)                       # T1 merged first
    assert not conflict.clean and conflict.conflicts == ("a.txt",)
    assert "rebase onto " + head in conflict.finding(parent)
    assert not (await br.merge(t2, parent, "T2")).clean and await br.head(parent) == head
    both = await asyncio.gather(br.merge(t3, parent, "T3"), br.merge(t3, parent, "T3 again"))
    assert sorted(m.merged for m in both) == [False, True]          # serialised: one merge


async def test_the_merge_on_pass_through_the_rules(hub):
    br, merger = Branches(hub), Merger(Branches(hub), NOW)
    base = await hub.resolve_base()
    into = ticket_branch("c1", "c")
    await br.create(into, base)
    t = tickets()
    t.start()
    t1 = commit(hub, base, {"a.txt": "one\n"}, "t1")
    t2 = commit(hub, base, {"a.txt": "two\n"}, "t2")
    t.work_done("T2", t2)                                           # T2: no criteria, merges
    assert merge_outcome(t, "T2", into, await merger.merge(t2, into, "T2")) == []
    assert t.t["T2"].status == Status.PASSED and t.t["c"].sha == await br.head(into)
    t.work_done("T1", t1)
    assert t.verdict("rev", True) == [Merge("T1", t1, "c")]
    eff = merge_outcome(t, "T1", into, await merger.merge(t1, into, "T1"))
    assert [type(e) for e in eff] == [StartWork] and "G-A2" in eff[0].findings[0]
    assert t.t["T1"].attempt == 2

    can = CanMergeExecutor(br, NOW)
    rev = t.t["rev"]
    assert (await can.run(work(rev, judged=t1, into=into))).passed is False
    t1b = commit(hub, t.t["c"].sha, {"a.txt": "one and two\n"}, "t1b")   # rebased
    assert await can.run(work(rev, judged=t1b, into=into)) == Judged(True)


async def test_publishing_without_a_remote_merges_into_the_base_branch(hub):
    br = Branches(hub)
    base = await hub.resolve_base()
    result = commit(hub, base, {"a.txt": "done\n"}, "result")

    async def text(w):
        return "Add a.txt\n\nThe change's description."
    pub = PublishExecutor(remote=None, branches=br, base_branch="main", message=text, backoff=NOW)
    x = T("publish", "release", type="controller:publish")
    assert await pub.run(work(x, base=result)) == Done()
    head = await br.head("main")
    assert _git("log", "-1", "--format=%s", head, cwd=hub.repo) == "Add a.txt"
    assert _git("rev-list", "--parents", "-n1", head, cwd=hub.repo).split()[1:] == [base, result]

    class Remote:
        pushed = []

        async def publish(self, w, head):
            Remote.pushed.append(head)
            return "https://github.com/o/r/pull/1"
    pub = PublishExecutor(remote=Remote(), branches=br, base_branch="main", message=text,
                          backoff=NOW)
    assert await pub.run(work(x, base=result)) == Done() and Remote.pushed == [result]
    assert isinstance(await pub.run(work(x)), Fault)                    # nothing to publish


async def test_a_file_is_committed_on_a_branch_head_in_the_bare_hub(hub):
    br = Branches(hub)
    base = await hub.resolve_base()
    await br.create("legion/c1/integration", base)
    moved = commit(hub, base, {"a.txt": "merged\n"}, "merged")          # merged meanwhile
    await hub.git("update-ref", "refs/heads/legion/c1/integration", moved, base)
    head = await br.commit_file("legion/c1/integration", "mise.toml", "[tools]\n", "pin")
    assert subprocess.run(["git", "ls-tree", "-r", "--name-only", head], cwd=hub.repo,
                          capture_output=True, text=True).stdout.split() == ["a.txt", "mise.toml"]
    assert await br.commit_file("legion/c1/integration", "mise.toml", "[tools]\n", "again") == head
