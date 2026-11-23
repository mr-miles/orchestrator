"""The ticket runner (legion_orch/workflow/tickets/runner.py) end to end: the real skeleton from
assets/workflow.yaml, real branches and merges in a bare hub, the real can-merge and publish
executors, the tickets persisted in the state tables – agents, humans and services scripted."""
from __future__ import annotations

import asyncio
import itertools
import sqlite3
import threading
from collections import defaultdict
from pathlib import Path

import pytest
from ticket_harness import Script, commit, files_at

from legion_orch.git.branches import Branches
from legion_orch.git.remote import GitRemote
from legion_orch.store.journal import Journal
from legion_orch.store.state_tables import StateTables
from legion_orch.workflow.tickets import Add, Replan, Severity, Status, Ticket, Tickets, Waiting
from legion_orch.workflow.tickets.executors import (
    Backoff,
    CanMergeExecutor,
    Done,
    Executors,
    GaveUp,
    Judged,
    PublishExecutor,
)
from legion_orch.workflow.tickets.runner import Runner
from legion_orch.workflow.tickets.spec import WorkflowSpec, check_policy
from legion_orch.workflow.tickets.store import TicketRecords

pytestmark = pytest.mark.anyio
SPEC = WorkflowSpec.load(Path(__file__).resolve().parent.parent / "assets/workflow.yaml")
NOW = Backoff(tries=2, first=0, cap=0)


def build(tid: str, *criteria: str, **kw) -> tuple[Ticket, list[Ticket]]:
    """A build ticket and its criteria (as the planner's tickets.json gives them)."""
    x = Ticket(id=tid, type=kw.pop("type", "agent:implementer"), goal=f"build {tid}",
               criteria=[f"{c}:{tid}" for c in criteria], **kw)
    types = {"verify": "controller:verify", "review": "agent:reviewer",
             "can-merge": "controller:can-merge"}
    return x, [Ticket(id=f"{c}:{tid}", parent=tid, role="criterion", type=types[c])
               for c in criteria]


def planned(*builds: tuple[Ticket, list[Ticket]]) -> Replan:
    """The planner's tickets: the builds in parallel, then the integration, under delivery."""
    adds = [Add(x, "delivery", 0, new_group=i == 0, tree=crit) for i, (x, crit) in enumerate(builds)]
    adds.append(Add(Ticket(id="integration", type="agent:integration"), "delivery", 1))
    return Replan(add=adds)


class Hooks:
    def __init__(self, plan: Replan, tier: str | None = "R2"):
        self.plan, self._tier = plan, tier
        self.escalations: list[tuple[str, Waiting]] = []
        self.endings: list[Status] = []
        self._escalated: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def escalation(self, tid: str, timeout: float = 10) -> None:
        """Until ``tid`` has escalated."""
        await asyncio.wait_for(self._escalated[tid].wait(), timeout)

    def tier(self):
        return self._tier

    async def replan(self, signoff):
        return self.plan.__class__(add=[Add(a.ticket.model_copy(deep=True), a.into, a.group,
                                            a.new_group, a.criterion,
                                            [d.model_copy(deep=True) for d in a.tree])
                                        for a in self.plan.add])

    async def escalated(self, ticket, reason):
        self.escalations.append((ticket.id, reason))
        self._escalated[ticket.id].set()

    async def ended(self, status):
        self.endings.append(status)


class Change:
    """One change's runner over a fresh hub and state database."""

    def __init__(self, tmp: Path, plan: Replan, tier: str | None = "R2"):
        self.tmp = tmp
        self.hub = GitRemote(tmp / "git", "app", None, "main")
        self.hooks = Hooks(plan, tier)
        self.conn = sqlite3.connect(tmp / "state.db", check_same_thread=False)
        self.records = TicketRecords(StateTables(self.conn, threading.RLock()),
                                     Journal(tmp / "journal", sync=False), "c1")

    async def runner(self, tickets: list[Ticket] | None = None) -> Runner:
        await self.hub.ensure()
        self.script = Script(self.hub)
        branches = Branches(self.hub)

        async def pr_text(work):
            return "Add the builds\n\nWhat the change does."
        executors = Executors({
            "agent": self.script, "human:signoff": self.script, "controller:verify": self.script,
            "controller:provision": self.script,
            "controller:can-merge": CanMergeExecutor(branches, NOW),
            "controller:publish": PublishExecutor(remote=None, branches=branches,
                                                  base_branch="main", message=pr_text,
                                                  backoff=NOW)})
        self.base = await self.hub.resolve_base()
        return Runner("c1", Tickets(tickets or SPEC.skeleton_at(None)), executors=executors,
                      branches=branches, base=self.base, spec=SPEC, hooks=self.hooks,
                      save=self.records.save)


async def test_a_change_runs_from_its_skeleton_to_its_publication(tmp_path):
    ch = Change(tmp_path, planned(build("T1", "verify", "review", "can-merge"),
                                  build("T2", "verify", "review", "can-merge")))
    r = await ch.runner()
    await r.start()
    await r.settle()
    assert ch.hooks.endings == [Status.PASSED] and ch.hooks.escalations == []
    ran = ch.script.ran()
    # the skeleton's order, the tier's tickets (R2: the oracle, not the baseline), the plan's
    order = ["intake", "H-1", "design", "critique", "H-E1", "provision", "oracle", "preflight",
             "H-2", "integration", "describe", "H-3"]
    assert [t for t in ran if t in order] == order and "baseline" not in r.t.t
    assert ran.index("H-2") < min(ran.index("T1"), ran.index("T2"))
    assert max(ran.index("review:T1"), ran.index("review:T2")) < ran.index("integration")
    # merged up the tree into the integration branch, the root's, then published into main
    assert r.branch_name("delivery") == "legion/c1/integration"
    assert {"T1.txt", "T2.txt", "oracle.txt"} <= set(files_at(ch.hub, "main"))
    assert all(x.status == Status.PASSED for x in r.t.t.values())
    assert r.t.t["design"].limit == 3                      # the tier's limits, set at H-1
    # persisted: the tables hold every ticket as it ended
    assert {x.id: x.status for x in ch.records.load()} == {x.id: x.status for x in r.t.t.values()}


async def test_a_plan_that_breaks_the_tier_policy_fails_its_signoff(tmp_path):
    ch = Change(tmp_path, planned(build("T1", "verify", "can-merge")))     # R2: no review
    r = await ch.runner()
    attempts = []

    def planner(work):
        attempts.append(work.findings)
        if len(attempts) == 2:                             # it reworks: the plan, fixed
            ch.hooks.plan = planned(build("T1", "verify", "review", "can-merge"))
        return Done()
    ch.script.outcomes["design"] = planner
    await r.start()
    await r.settle()
    assert "T1 (agent:implementer) must have a agent:reviewer criterion" in attempts[1][0]
    assert ch.hooks.endings == [Status.PASSED]
    assert r.t.t["design"].attempt == 2


async def test_a_conflict_at_merge_is_a_rework_onto_the_new_head(tmp_path):
    # T1 and T2 change the same file: whichever merges second reworks, rebased
    ch = Change(tmp_path, planned(build("T1", "verify", "can-merge"),
                                  build("T2", "verify", "can-merge")), tier="R0")
    r = await ch.runner()

    def same_file(work):
        base = work.base if work.attempt == 1 else r.t.t["delivery"].sha
        return Done(commit(ch.hub, base, {"shared.txt": f"{work.ticket.id} {work.attempt}\n"},
                           f"{work.ticket.id}-{work.attempt}"))
    ch.script.outcomes["T1"] = ch.script.outcomes["T2"] = same_file
    await r.start()
    await r.settle()
    reworked = [x for x in ("T1", "T2") if r.t.t[x].attempt == 2]
    assert len(reworked) == 1 and ch.hooks.endings == [Status.PASSED]
    assert "G-A2" in r.t.t[reworked[0]].findings[0]


async def test_a_restart_resumes_whatever_was_under_way_at_the_same_attempt(tmp_path):
    for stuck in ("provision", "T1"):                       # a controller's work, an agent's
        (tmp_path / stuck).mkdir()
        ch = Change(tmp_path / stuck, planned(build("T1", "verify", "can-merge")), tier="R0")
        r = await ch.runner()
        gate = ch.script.gates[stuck] = asyncio.Event()
        await r.start()
        await ch.script.reached(stuck)
        await r.stop()                                     # the controller goes down

        r2 = await ch.runner(ch.records.load())            # and comes back
        await r2.recover()
        await r2.settle()
        assert ch.script.calls[0] == (stuck, 1)            # the same round again: no attempt
        assert ch.hooks.endings == [Status.PASSED] and ch.hooks.escalations == []
        assert not gate.is_set() and r2.t.t[stuck].attempt == 1


async def test_an_operators_pause_drains_the_work_and_a_resume_goes_on(tmp_path):
    ch = Change(tmp_path, planned(build("T1", "verify", "can-merge"),
                                  build("T2", "verify", "can-merge")), tier="R0")
    r = await ch.runner()
    gates = {t: ch.script.gates.setdefault(t, asyncio.Event()) for t in ("T1", "T2")}
    await r.start()
    await ch.script.reached("T1")
    await ch.script.reached("T2")
    await r.pause()
    for g in gates.values():
        g.set()
    await r.settle()                                       # both concluded; nothing started
    assert r.t.active() == [] and "verify:T1" not in ch.script.ran()
    assert {r.t.t[t].status for t in ("T1", "T2")} == {Status.CHECKING}
    await r.resume()
    await r.settle()
    assert ch.hooks.endings == [Status.PASSED]


async def test_failures_escalate_and_abandon_stops_running_work(tmp_path):
    ch = Change(tmp_path, planned(build("T1", "verify", "can-merge"),
                                  build("T2", "verify", "can-merge")), tier="R0")
    r = await ch.runner()
    rounds = itertools.count(1)                            # a new failure each round
    ch.script.outcomes["verify:T1"] = lambda w: Judged(False, (f"BLOCKER: {next(rounds)}",))
    ch.script.outcomes["T1"] = lambda w: GaveUp(("stuck",)) if w.attempt == 3 else Done(
        commit(ch.hub, w.base, {"T1.txt": str(w.attempt)}, f"T1-{w.attempt}"))
    gate = ch.script.gates["T2"] = asyncio.Event()
    await r.start()
    await ch.hooks.escalation("T1")
    assert r.t.t["T1"].waiting == Waiting.ATTEMPTS and ("T1", Waiting.ATTEMPTS) in \
        ch.hooks.escalations
    assert r.t.t["T2"].status == Status.WORKING            # a sibling failing stops nothing (R9)
    await r.abandon()
    await r.settle()
    assert ch.script.stopped == ["T2"] and ch.hooks.endings == [Status.CANCELLED]
    assert not gate.is_set() and r.t.t["T2"].status == Status.CANCELLED


def test_the_policy_and_the_skeleton_at_each_tier():
    for tier in SPEC.tiers:
        t = Tickets(SPEC.skeleton_at(None))
        t.t["H-1"].status = Status.WORKING
        t.pause()                                          # as its start does
        t.verdict("H-1", True, apply=SPEC.tier_additions(tier, t.t))
        assert ("acceptance" in t.t) == (tier in ("R2", "R3"))
        assert ("baseline" in t.t) == (tier == "R3")
        v = check_policy(SPEC, tier, t.t)
        assert f"tier {tier}: delivery's last group must be its agent:integration" in v
    t = Tickets(SPEC.skeleton_at("R2"))
    t.pause()
    t.replan(planned(build("T1", "verify", "can-merge", labels=["security"])))
    v = check_policy(SPEC, "R2", t.t)
    assert "tier R2: T1 (agent:implementer) must have a agent:reviewer criterion" in v
    assert "tier R2: T1 is labelled security: it must have a agent:security criterion" in v
    assert all(not x.waivable for x in t.t.values() if x.type == "human:signoff")
    assert t.t["H-1"].severity == Severity.REWORK and t.t["H-1"].parent == "intake"   # reworks
    assert t.t["H-2"].parent == "oracle" and t.t["H-3"].severity == Severity.ESCALATE


def test_a_workflow_file_that_cannot_work_is_refused(tmp_path):
    import shutil

    import yaml

    from legion_orch.workflow.assets import TEMPLATE_VARS, AssetError, Assets
    from legion_orch.workflow.tickets.spec import SpecError

    def with_(change) -> Path:
        d = tmp_path / str(len(list(tmp_path.iterdir())))
        shutil.copytree(Path(__file__).resolve().parent.parent / "assets", d)
        data = yaml.safe_load((d / "workflow.yaml").read_text())
        change(data)
        (d / "workflow.yaml").write_text(yaml.safe_dump(data))
        return d

    def loops(d):
        d["tiers"]["R0"]["extends"] = "R3"
    def unknown_role(d):
        d["skeleton"]["subtasks"][0]["subtasks"][0]["type"] = "agent:nobody"
    def unknown_ticket(d):
        d["tiers"]["R0"]["requires"]["nowhere"] = ["x"]
    def rework_on_grouping(d):
        d["skeleton"]["subtasks"][3]["criteria"][0]["severity"] = "rework"   # evaluate's H-3
    for broken, says in ((loops, "extends"), (unknown_role, "nobody"),
                         (unknown_ticket, "nowhere"), (rework_on_grouping, "R4")):
        with pytest.raises((AssetError, SpecError), match=says):
            Assets(with_(broken))
    a = Assets(Path(__file__).resolve().parent.parent / "assets")
    rendered = a.render_role("planner", dict.fromkeys(TEMPLATE_VARS, "x"))
    assert "legion-workflow" in rendered.skills
    assert "### Tier R2" in rendered.skills["legion-workflow"]


async def test_a_signoff_that_applies_a_plan_waits_for_the_others_to_drain(tmp_path):
    tree = [Ticket(id="c", role="root", subtasks=[["plan", "side"], ["delivery"]]),
            Ticket(id="plan", parent="c", subtasks=[["design"]]),
            Ticket(id="design", parent="plan", type="agent:planner", criteria=["H-E1"]),
            Ticket(id="H-E1", parent="design", role="criterion", type="human:signoff",
                   applies="tickets/design/tickets.json"),
            Ticket(id="side", parent="c", type="agent:researcher"),
            Ticket(id="delivery", parent="c")]
    ch = Change(tmp_path, Replan(add=[Add(Ticket(id="T1", type="agent:implementer"),
                                          "delivery", 0)]), tier=None)
    r = await ch.runner(tree)
    side = ch.script.gates["side"] = asyncio.Event()
    signed = asyncio.Event()

    def signoff(work):
        signed.set()
        return Judged(True)
    ch.script.outcomes["H-E1"] = signoff
    await r.start()
    await asyncio.wait_for(signed.wait(), 10)
    assert r.t.paused and "T1" not in r.t.t                # a static backdrop: side still works
    side.set()
    await r.settle()
    assert r.t.t["T1"].status == Status.PASSED and ch.hooks.endings == [Status.PASSED]
    assert r.t.t["design"].attempt == 1                    # held, not refused (no rework)
    assert ch.script.ran().index("side") < ch.script.ran().index("T1")
