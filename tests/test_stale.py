"""Revised outputs and their follow-ups (legion_orch/workflow/tickets/stale.py): what a replan
revises is signed off again, and a sign-off that moves an approved output to a new version
renews the finished work of a `redo` type that consumed it – nothing else."""
from __future__ import annotations

from engine_harness import TASKS, Flow, plan_doc, scenario
from test_tickets import T, crit

from legion_orch.workflow.tickets import Add, Replan, Status, Ticket, Tickets
from legion_orch.workflow.tickets.stale import followups, next_id, resign

OUTPUTS = {"agent:planner": ["spec", "environment", "tickets"]}


def change() -> Tickets:
    t = Tickets([
        T("c", subtasks=[["plan"], ["delivery"]]),
        T("plan", "c", subtasks=[["design"], ["provision"]]),
        T("design", "plan", type="agent:planner", criteria=["H-E1"]),
        crit("H-E1", "design", type="human:signoff", signs=["spec", "environment", "tickets"],
             applies="tickets",
             waivable=False),
        T("provision", "plan", type="controller:provision", inputs=["environment"]),
        T("delivery", "c", subtasks=[["T1"]]),
        T("T1", "delivery", type="agent:implementer", inputs=["spec"]),
    ])
    for x in t.t.values():
        x.status = Status.PASSED
    t.t["delivery"].status = t.t["c"].status = Status.SUBTASKS
    t.root.pointers = {"spec": "file:s@1", "environment": "file:e@1", "tickets": "file:t@1"}
    return t


def test_ids_of_new_instances():
    assert next_id("provision", ["provision"]) == "provision.2"
    assert next_id("provision.2", ["provision", "provision.2"]) == "provision.3"


def test_a_replans_revision_of_approved_outputs_is_signed_off_again():
    t = change()
    plan = Replan(add=[Add(T("redesign", type="agent:planner"), "delivery", 1)])
    assert resign(t.t, plan, OUTPUTS) == ["H-E1.2"]
    [s] = plan.add[0].tree
    assert (s.parent, s.type, s.signs, s.applies) == ("redesign", "human:signoff",
                                                     ["spec", "environment", "tickets"], "tickets")
    assert plan.add[0].ticket.criteria == ["H-E1.2"]
    again = Replan(add=[Add(T("redesign", type="agent:planner", criteria=["mine"]), "delivery", 1,
                            tree=[crit("mine", "redesign", type="human:signoff",
                                       signs=["environment"])])])
    assert resign(t.t, again, OUTPUTS) == []                 # it has its own
    env = Replan(add=[Add(T("env", type="agent:envplanner"), "delivery", 1)])
    [e] = resign(t.t, env, {"agent:envplanner": ["environment"]})
    assert (env.add[0].tree[0].signs, env.add[0].tree[0].applies) == (["environment"], None)
    assert e == "H-E1.2"                                     # it does not produce the tickets
    build = Replan(add=[Add(T("T2", type="agent:implementer"), "delivery", 1)])
    assert resign(t.t, build, OUTPUTS) == []                 # it produces nothing approved


def test_a_new_version_renews_what_consumed_it_and_of_a_redo_type_only():
    t = change()
    t.t["delivery"].subtasks.append(["redesign"])
    t.t["redesign"] = T("redesign", "delivery", type="agent:planner", criteria=["H-E1.2"])
    t.t["H-E1.2"] = crit("H-E1.2", "redesign", type="human:signoff", signs=["environment"])
    redo = {"controller:provision"}
    same = followups(t.t, "H-E1.2", {"environment": "file:e@1"}, redo)
    assert same.add == []                                    # an unchanged version: nothing
    spec = followups(t.t, "H-E1.2", {"spec": "file:s@2"}, redo)
    assert spec.add == []                                    # the build stands
    f = followups(t.t, "H-E1.2", {"environment": "file:e@2"}, redo)
    [a] = f.add
    assert (a.ticket.id, a.ticket.type, a.into, a.group, a.new_group) == (
        "provision.2", "controller:provision", "delivery", 2, True)
    assert a.ticket.inputs == ["environment"] and a.ticket.status == Status.PENDING
    t.t["provision.2"] = Ticket(id="provision.2", parent="delivery", type="controller:provision",
                                inputs=["environment"])
    assert followups(t.t, "H-E1.2", {"environment": "file:e@3"}, redo).add == []   # one to run


@scenario()
async def test_a_revised_environment_is_signed_off_and_provisioned_again(f: Flow):
    await f.to_build()
    first = f.ce.tickets.root.pointers["environment"]
    # a replan (a decision owner's, at the change level) adds a revised design after T1
    await f.change_action("pause")
    await f.implement("T1")                               # it drains to its conclusion point
    await f.change_action("replan", plan={"add": [{"into": "delivery", "group": 9, "ticket": {
        "id": "redesign", "type": "agent:planner", "goal": "the environment, revised",
        "inputs": ["requirements", "risk"]}}]})
    await f.change_action("resume")
    if "review:T1" in f.ce.st.tickets:
        await f.review("T1")
    redesign = f.ticket("redesign")
    assert redesign.criteria == ["H-E1.2"] and f.ticket("H-E1.2").type == "human:signoff"
    build = plan_doc([{**TASKS[0], "id": "T2", "title": "greet again"}],
                     ("verify", "review", "can-merge"))["add"]
    res = await f.plan(tid="redesign", doc={"add": [{**build[0], "group": 99}]}, environment={
        "summary": "the default image, revised", "profiles": {"default": {"tools": []}},
        "roles": {}, "rationale": "a tool was missing"})
    assert res.accepted, res
    await f.signoff("H-E1.2")
    assert f.ce.tickets.root.pointers["environment"] != first
    p2 = f.ticket("provision.2")                          # renewed, right after the redesign
    assert p2.parent == "delivery" and p2.status in ("working", "passed"), f.status()
    await f.settle()
    assert f.status("provision.2") == "passed" and f.status("provision") == "passed"
    assert "T1.2" not in f.ce.st.tickets                  # the build stands
    assert f.ticket("T2").parent == "delivery"            # the revised plan applied
