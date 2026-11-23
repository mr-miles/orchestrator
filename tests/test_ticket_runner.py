"""The ticket runner (legion_orch/workflow/tickets/runner.py) end to end: the real skeleton from
assets/workflow.yaml, real branches and merges in a bare hub, the real can-merge and publish
executors, the tickets persisted in the state tables – agents, humans and services scripted."""
from __future__ import annotations

import asyncio
import itertools
import sqlite3
import subprocess
import threading
from collections import defaultdict
from pathlib import Path

import pytest
from ticket_harness import Script, commit, files_at

from legion_orch.git.branches import Branches
from legion_orch.git.remote import GitRemote
from legion_orch.store.journal import Journal
from legion_orch.store.state_tables import StateTables
from legion_orch.workflow.assets import Assets
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
ASSETS = Assets(Path(__file__).resolve().parent.parent / "assets")
NOW = Backoff(tries=2, first=0, cap=0)


WORK = {"soul": "backend-engineer", "requirements": ["REQ-1"], "files_modified": ["src/x.py"],
        "instructions": "do it", "acceptance": ["it works"], "verification": ["pytest -q"]}


def build(tid: str, *criteria: str, **kw) -> tuple[Ticket, list[Ticket]]:
    """A build ticket and its criteria (as the planner's tickets.json gives them)."""
    x = Ticket(id=tid, type=kw.pop("type", "agent:implementer"), goal=f"build {tid}",
               criteria=[f"{c}:{tid}" for c in criteria], work=kw.pop("work", WORK), **kw)
    types = {"verify": "controller:verify", "review": "agent:reviewer",
             "can-merge": "controller:can-merge"}
    return x, [Ticket(id=f"{c}:{tid}", parent=tid, role="criterion", type=types[c])
               for c in criteria]


def planned(*builds: tuple[Ticket, list[Ticket]]) -> Replan:
    """The planner's tickets: the builds in parallel under delivery (whose criteria – the
    oracle's commands, the integration – are the skeleton's)."""
    return Replan(add=[Add(x, "delivery", 0, new_group=i == 0, tree=crit)
                       for i, (x, crit) in enumerate(builds)])



def node(d: dict, tid: str) -> dict:
    """A ticket of a workflow file's skeleton, by id (wherever it sits)."""
    stack = [d["skeleton"]]
    while stack:
        x = stack.pop()
        if x.get("id") == tid:
            return x
        for g in x.get("subtasks", []):
            stack += g if isinstance(g, list) else [g]
        stack += x.get("criteria", [])
    raise KeyError(tid)

class Hooks:
    def __init__(self, plan: Replan, tier: str | None = "R2"):
        self.plan, self._tier = plan, tier
        self.escalations: list[tuple[str, Waiting]] = []
        self.endings: list[Status] = []
        self._escalated: defaultdict[str, asyncio.Event] = defaultdict(asyncio.Event)

    async def escalation(self, tid: str, timeout: float = 10) -> None:
        """Until ``tid`` has escalated."""
        await asyncio.wait_for(self._escalated[tid].wait(), timeout)

    def tier(self, pointers):
        self.pointers = dict(pointers)
        return self._tier if "risk" in pointers else None   # from the approved risk

    async def replan(self, signoff, ref):
        self.replanned = ref
        return self.plan.__class__(add=[Add(a.ticket.model_copy(deep=True), a.into, a.group,
                                            a.new_group, a.criterion,
                                            [d.model_copy(deep=True) for d in a.tree])
                                        for a in self.plan.add])

    def problems(self, ticket):
        return ASSETS.work_problems(ticket)

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
        self.script = Script(self.hub, ASSETS.role_outputs())
        branches = Branches(self.hub)

        async def pr_text(work):
            return "Add the builds\n\nWhat the change does."
        executors = Executors({
            "agent": self.script, "human:signoff": self.script, "controller:verify": self.script,
            "controller:preflight": self.script, "controller:sync": self.script,
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
             "H-2", "verify:delivery", "integration", "describe", "H-3"]
    assert [t for t in ran if t in order] == order and "baseline" not in r.t.t
    assert ran.index("H-2") < min(ran.index("T1"), ran.index("T2"))
    assert max(ran.index("review:T1"), ran.index("review:T2")) < ran.index("integration")
    # merged up the tree – the builds into delivery's branch, judged there, then into the
    # integration branch, the root's – then published into main
    assert r.branch_name("change") == "legion/c1/integration"
    made = set(subprocess.run(["git", "for-each-ref", "--format=%(refname:lstrip=5)",
                               "refs/heads/legion/c1/t/"], cwd=ch.hub.repo, check=True,
                              capture_output=True, text=True).stdout.split())
    assert not made & {"requirements", "plan", "acceptance", "release"}   # no criteria
    assert {"T1", "T2", "oracle", "delivery", "evaluate"} <= made   # agents', and those judged
    assert {"T1.txt", "T2.txt", "oracle.txt"} <= set(files_at(ch.hub, "main"))
    assert all(x.status == Status.PASSED for x in r.t.t.values())
    assert r.t.t["design"].limit == 3                      # the tier's limits, set at H-1
    # each sign-off moved the change's pointers to exactly what it approved
    ptr = r.t.root.pointers
    assert ptr["requirements"] == "file:tickets/intake/requirements@1"     # H-1
    assert ptr["spec"] == "file:tickets/design/spec@1"                     # H-E1
    assert ptr["tests"] == "file:tickets/oracle/tests@1"                   # H-2
    assert ptr["pr"] == "file:tickets/describe/pr@1"                       # H-3, from its subtree
    assert ptr["head"].startswith("git:") and "baseline" not in r.t.t      # R2
    assert "risk" in ch.hooks.pointers                                    # the tier came from it
    assert ch.hooks.replanned == "file:tickets/design/tickets@1"           # the plan H-E1 signed
    # consumers were given the approved versions
    assert ch.script.works["provision"].inputs == {"environment": ptr["environment"]}
    assert ch.script.works["oracle"].inputs["spec"] == ptr["spec"]
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
        return Done(outputs=ch.script.outputs(work))
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
        base = work.base if work.attempt == 1 else r.t.t["delivery"].sha   # its merge target
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
        assert ("H-2b" in t.t and t.t["H-2b"].parent == "baseline") == (tier == "R3")
        v = check_policy(SPEC, tier, t.t)
        assert not any("agent:integration" in x for x in v)    # the skeleton's: delivery's
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
        node(d, "intake")["type"] = "agent:nobody"
    def both_ways(d):
        d["tiers"]["R0"]["order"].append({"each": "agent:x", "after": "agent:y",
                                          "before": "agent:z"})
    def rework_on_grouping(d):
        node(d, "H-3")["severity"] = "rework"   # evaluate's H-3
    for broken, says in ((loops, "extends"), (unknown_role, "nobody"),
                         (both_ways, "exactly one"), (rework_on_grouping, "R4")):
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
                   signs=["tickets"], applies="tickets"),
            Ticket(id="side", parent="c", type="agent:researcher"),
            Ticket(id="delivery", parent="c")]
    ch = Change(tmp_path, Replan(add=[Add(Ticket(id="T1", type="agent:implementer", work=WORK),
                                          "delivery", 0)]), tier=None)
    r = await ch.runner(tree)
    side = ch.script.gates["side"] = asyncio.Event()
    signed = asyncio.Event()

    def signoff(work):
        signed.set()
        return Judged(True, signed=work.subjects)
    ch.script.outcomes["H-E1"] = signoff
    await r.start()
    await asyncio.wait_for(signed.wait(), 10)
    assert r.t.paused and "T1" not in r.t.t                # a static backdrop: side still works
    side.set()
    await r.settle()
    assert r.t.t["T1"].status == Status.PASSED and ch.hooks.endings == [Status.PASSED]
    assert r.t.t["design"].attempt == 1                    # held, not refused (no rework)
    assert ch.script.ran().index("side") < ch.script.ran().index("T1")


def test_the_order_rules_judge_where_tickets_run_whatever_they_are_called():
    from legion_orch.workflow.tickets.rules import precedes

    def prepared(plan: Replan) -> Replan:                  # as the runner does with a plan
        for add in plan.add:
            for x in [add.ticket, *add.tree]:
                SPEC.prepare(x, "R2")
        return plan
    t = Tickets(SPEC.skeleton_at("R2"))
    t.pause()
    t.replan(prepared(planned(build("T1", "verify", "review", "can-merge"))))
    # the plan's tree: provision (plan, group 2) is finished before T1 (delivery) starts
    assert precedes(t.t, "provision", "T1") and not precedes(t.t, "T1", "provision")
    assert precedes(t.t, "T1", "integration") and precedes(t.t, "T1", "H-3")
    assert precedes(t.t, "design", "H-E1") is False                # its own criterion: inside it
    assert precedes(t.t, "verify:T1", "review:T1")                 # criteria in order
    assert not precedes(t.t, "plan", "provision")                  # an ancestor
    assert check_policy(SPEC, "R2", t.t) == []
    # a replacement provision, named anything, still counts – a build planned too early does not
    bad = Tickets(SPEC.skeleton_at("R2"))
    bad.pause()
    early, crit = build("T0", "verify", "review", "can-merge")
    bad.replan(prepared(Replan(add=[Add(early, "plan", 1, tree=crit),   # before provision
                                    *planned(build("T1", "verify", "review", "can-merge")).add])))
    assert check_policy(SPEC, "R2", bad.t) == [
        "tier R2: T0 (agent:implementer) must come after a controller:provision",
        "tier R2: T0 (agent:implementer) must come after a agent:oracle"]
    renamed = Tickets(SPEC.skeleton_at("R2"))
    renamed.pause()
    renamed.replan(prepared(Replan(
        cancel=["provision"],
        add=[Add(Ticket(id="rebuild-env", type="controller:provision"), "plan", 1),
             *planned(build("T1", "verify", "review", "can-merge")).add])))
    assert renamed.t["plan"].subtasks[1] == ["rebuild-env"]
    assert check_policy(SPEC, "R2", renamed.t) == []


def test_signoff_subjects_resolve_against_the_work_they_check():
    from legion_orch.workflow.tickets.outputs import resolve
    t = {x.id: x for x in [
        Ticket(id="c", role="root", subtasks=[["ev"]]),
        Ticket(id="ev", parent="c", subtasks=[["a", "b"]], criteria=["H"]),
        Ticket(id="a", parent="ev", type="agent:describer", outputs={"pr": "file:tickets/a/pr@2"}),
        Ticket(id="b", parent="ev", type="agent:describer"),
        Ticket(id="H", parent="ev", role="criterion", type="human:signoff", signs=["pr", "head"])]}
    assert resolve(t, "H", "f" * 40) == ({"pr": "file:tickets/a/pr@2", "head": "git:" + "f" * 40}, [])
    t["b"].outputs = {"pr": "file:tickets/b/pr@1"}                  # two producers: which?
    assert "several tickets produce it (a, b)" in resolve(t, "H", "f" * 40)[1][0]
    t["a"].outputs = t["b"].outputs = {}
    assert "ev has no ticket that produces it" in resolve(t, "H", None)[1][0]
    assert "no commit to judge" in resolve(t, "H", None)[1][1]


def test_check_refuses_signoffs_nobody_can_satisfy(tmp_path):
    import shutil

    import yaml

    from legion_orch.workflow.assets import AssetError
    root = Path(__file__).resolve().parent.parent / "assets"
    for n, (change, says) in enumerate([
            (lambda d: node(d, "H-3")["signs"].append("nothing"),
             "evaluate has no ticket that produces it"),
            (lambda d: node(d, "H-3").update(applies="pr2"),
             "which it does not sign"),
            (lambda d: node(d, "describe").update(inputs=["secret"]),
             "input 'secret' is no sign-off's output")]):
        d = tmp_path / str(n)
        shutil.copytree(root, d)
        data = yaml.safe_load((d / "workflow.yaml").read_text())
        change(data)
        (d / "workflow.yaml").write_text(yaml.safe_dump(data))
        with pytest.raises(AssetError, match=says):
            Assets(d)


async def test_a_plan_whose_work_does_not_fit_its_role_fails_its_signoff(tmp_path):
    bad = dict(WORK, files_modified=[])                    # the schema wants at least one
    ch = Change(tmp_path, planned(build("T1", "verify", "review", "can-merge", work=bad)))
    r = await ch.runner()
    seen = []

    def planner(work):
        seen.append(work.findings)
        if len(seen) == 2:
            ch.hooks.plan = planned(build("T1", "verify", "review", "can-merge"))
        return Done(outputs=ch.script.outputs(work))
    ch.script.outcomes["design"] = planner
    await r.start()
    await r.settle()
    assert any("T1:" in f and "files_modified" in f for f in seen[1])
    assert ch.hooks.endings == [Status.PASSED] and r.t.t["T1"].work == WORK
