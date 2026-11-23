"""The ticket rules (legion_orch/workflow/tickets): scenarios for each rule, and a property test
driving random trees through random events with the invariants checked after every step."""
from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from legion_orch.workflow.tickets import (
    Add,
    Ended,
    Escalate,
    Merge,
    Remedy,
    Replan,
    Severity,
    StartWork,
    Status,
    StopWork,
    Ticket,
    TicketError,
    Tickets,
    Waiting,
    validate,
)


def T(id, parent=None, **kw) -> Ticket:
    role = "root" if parent is None else kw.pop("role", "subtask")
    return Ticket(id=id, parent=parent, role=role, **kw)


def crit(id, parent, severity=Severity.REWORK, **kw) -> Ticket:
    return T(id, parent, role="criterion", type=kw.pop("type", "agent:reviewer"),
             severity=severity, **kw)


def started(effects) -> list[str]:
    return [e.ticket for e in effects if isinstance(e, StartWork)]


def build() -> Tickets:
    """A change: delivery's builds T1 ∥ T2, then T3; each build reviewed; the change signed off."""
    return Tickets([
        T("change", subtasks=[["delivery"]], criteria=["H-3"]),
        crit("H-3", "change", Severity.ESCALATE, type="human:signoff"),
        T("delivery", "change", subtasks=[["T1", "T2"], ["T3"]]),
        T("T1", "delivery", type="agent:implementer", criteria=["rev:T1"]),
        crit("rev:T1", "T1"),
        T("T2", "delivery", type="agent:implementer"),
        T("T3", "delivery", type="agent:implementer"),
    ])


# ------------------------------------------------------------------------------- order
def test_groups_run_in_order_and_a_groups_tickets_in_parallel():
    t = build()
    assert started(t.start()) == ["T1", "T2"]
    assert t.work_done("T2", "b" * 40) == [Merge("T2", "b" * 40, "delivery")]
    assert started(t.work_done("T1", "a" * 40)) == ["rev:T1"]     # T1's criterion; T3 waits
    eff = t.verdict("rev:T1", True)
    assert Merge("T1", "a" * 40, "delivery") in eff and started(eff) == ["T3"]
    eff = t.work_done("T3")
    assert started(eff) == ["H-3"] and t.t["delivery"].status == Status.PASSED
    assert t.verdict("H-3", True) == [Ended(Status.PASSED)]


def test_criteria_run_in_order_and_stop_at_the_first_failure():
    t = Tickets([T("c", subtasks=[["w"]]),
                 T("w", "c", type="agent:implementer", criteria=["k1", "k2"]),
                 crit("k1", "w", type="controller:verify"), crit("k2", "w")])
    t.start()
    assert started(t.work_done("w")) == ["k1"]
    eff = t.verdict("k1", False, ["`pytest` exited 1"])
    assert eff == [StartWork("w", 2, ("`pytest` exited 1",))]       # rework, k2 never ran
    assert t.t["k2"].status == Status.PENDING


# ------------------------------------------------------------------------------- failure
def test_a_rework_reruns_every_criterion_and_the_limit_makes_it_wait():
    t = Tickets([T("c", subtasks=[["w"]]),
                 T("w", "c", type="agent:implementer", criteria=["k1", "k2"], limit=2),
                 crit("k1", "w"), crit("k2", "w")])
    t.start()
    t.work_done("w")
    t.verdict("k1", True)
    assert started(t.verdict("k2", False, ["a"])) == ["w"]           # attempt 2
    assert t.t["k1"].status == Status.PENDING                          # rerun from the first
    assert started(t.work_done("w")) == ["k1"]
    t.verdict("k1", True)
    assert t.verdict("k2", False, ["b"]) == [Escalate("w", Waiting.ATTEMPTS)]
    assert t.t["w"].status == Status.WAITING and t.t["w"].attempt == 2   # not failed yet


def test_the_same_findings_twice_escalate_early():
    t = Tickets([T("c", subtasks=[["w"]]),
                 T("w", "c", type="agent:implementer", criteria=["k"], limit=5), crit("k", "w")])
    t.start()
    t.work_done("w")
    t.verdict("k", False, ["blocker: x"])
    t.work_done("w")
    assert t.verdict("k", False, ["blocker: x"]) == [Escalate("w", Waiting.REPEATED)]


def test_a_waiver_passes_a_rejected_criterion_and_the_check_goes_on():
    t = Tickets([T("c", subtasks=[["w"]]),
                 T("w", "c", type="agent:implementer", criteria=["k1", "k2"], limit=1),
                 crit("k1", "w"), crit("k2", "w", waivable=False)])
    t.start()
    t.work_done("w")
    t.verdict("k1", False, ["a"])                                       # limit 1: waits
    assert started(t.decide("k1", Remedy.WAIVE)) == ["k2"]
    assert t.t["k1"].waived
    t.verdict("k2", False, ["b"])
    with pytest.raises(TicketError, match="cannot be waived"):
        t.decide("k2", Remedy.WAIVE)


def test_a_grouping_tickets_criteria_escalate():
    bad = [T("c", subtasks=[["w"]], criteria=["k"]), T("w", "c", type="agent:x"), crit("k", "c")]
    assert any("R4" in e for e in validate({x.id: x for x in bad}))
    t = build()
    t.start()
    for x in ("T1", "T2"):
        t.work_done(x)
    t.verdict("rev:T1", True)
    t.work_done("T3")
    assert t.verdict("H-3", False, ["no"]) == [Escalate("change", Waiting.ESCALATED)]


def test_system_errors_and_budgets_wait_without_using_an_attempt():
    t = build()
    t.start()
    assert t.system_error("T1") == [Escalate("T1", Waiting.SYSTEM)]
    assert t.decide("T1", Remedy.RETRY) == [StartWork("T1", 1)]        # same attempt
    assert t.budget_exceeded("T2") == [Escalate("T2", Waiting.BUDGET)]
    with pytest.raises(TicketError, match="not a remedy"):
        t.decide("T2", Remedy.WAIVE)
    t.decide("T2", Remedy.RETRY)
    assert (t.t["T1"].attempt, t.t["T2"].attempt) == (1, 1)


def test_an_empty_grouping_ticket_waits_at_once():
    t = Tickets([T("c", subtasks=[["delivery"]]), T("delivery", "c")])
    assert t.start() == [Escalate("delivery", Waiting.EMPTY)]


# ------------------------------------------------------------------------------- replans
def test_a_failed_integration_is_replaced_by_fixes_and_a_new_integration():
    t = Tickets([T("c", subtasks=[["delivery"]]),
                 T("delivery", "c", subtasks=[["T1"], ["integration"]]),
                 T("T1", "delivery", type="agent:implementer"),
                 T("integration", "delivery", type="agent:integration", criteria=["ok"]),
                 crit("ok", "integration", Severity.ESCALATE, type="controller:verify")])
    t.start()
    t.work_done("T1", "a" * 40)
    t.work_done("integration")
    assert t.verdict("ok", False, ["152 failed"]) == [Escalate("integration", Waiting.ESCALATED)]
    t.decide("integration", Remedy.REPLAN)
    assert t.t["integration"].status == Status.FAILED                  # immutable history
    assert t.t["delivery"].subtasks == [["T1"]]
    with pytest.raises(TicketError, match="after delivery's completed groups"):
        t.replan(Replan(add=[Add(T("T0", type="agent:implementer"), "delivery", 0)]))
    eff = t.replan(Replan(add=[
        Add(T("T4", type="agent:implementer"), "delivery", 1),
        Add(T("integration.2", type="agent:integration"), "delivery", 2)]))
    assert started(eff) == ["T4"]
    assert started(t.work_done("T4", "c" * 40)) == ["integration.2"]
    assert t.t["integration"].status == Status.FAILED


def test_a_replan_waits_for_inactive_tickets_and_never_touches_closed_or_started_ones():
    t = build()
    t.start()
    with pytest.raises(TicketError, match="every ticket to be inactive"):
        t.replan(Replan())                                  # T1 and T2 are working
    t.work_done("T2")
    t.system_error("T1")                                    # T1 waits: nothing works now
    with pytest.raises(TicketError, match="nothing is added under it"):
        t.replan(Replan(add=[Add(T("x", type="agent:y"), "T2", 0)]))   # T2 passed: closed
    with pytest.raises(TicketError, match="only tickets not yet started are cancelled"):
        t.replan(Replan(cancel=["T1"]))
    assert t.replan(Replan(update={"T3": {"goal": "smaller"}})) == []
    assert t.t["T3"].goal == "smaller"


def test_a_replan_that_cancels_the_root_ends_the_change_and_abandon_stops_work():
    t = build()
    assert t.replan(Replan(cancel=["change"])) == [Ended(Status.CANCELLED)]
    t = build()
    t.start()
    eff = t.abandon()
    assert set(eff) == {StopWork("T1"), StopWork("T2"), Ended(Status.CANCELLED)}


def test_labels_are_inherited_and_add_only():
    t = Tickets([T("c", subtasks=[["d"]], labels=["security"]),
                 T("d", "c", subtasks=[["w"]]), T("w", "d", type="agent:x", labels=["ui"])])
    assert t.labels("w") == {"security", "ui"}


# ------------------------------------------------------------------------------- properties
@st.composite
def trees(draw):
    """A random tree: grouping tickets with groups of children, leaves with criteria."""
    tickets: list[Ticket] = []
    n = [0]

    def node(parent: str | None, depth: int) -> str:
        n[0] += 1
        tid = f"t{n[0]}"
        grouping = depth < 2 and draw(st.booleans())
        x = T(tid, parent, type="grouping" if grouping else "agent:w",
              limit=draw(st.integers(1, 3)))
        tickets.append(x)
        if grouping:
            x.subtasks = [[node(tid, depth + 1) for _ in range(draw(st.integers(1, 2)))]
                          for _ in range(draw(st.integers(1, 2)))]
        for _ in range(draw(st.integers(0, 2))):
            n[0] += 1
            cid = f"t{n[0]}"
            tickets.append(crit(cid, tid, Severity.ESCALATE if grouping
                                else draw(st.sampled_from(list(Severity)))))
            x.criteria.append(cid)
        return tid
    node(None, 0)
    return tickets


def check(t: Tickets) -> None:
    for x in t.t.values():
        assert (x.status == Status.WAITING) == (x.waiting is not None), x
        assert x.attempt <= x.limit, x
        live = [g for g in x.subtasks]
        if x.status in (Status.SUBTASKS, Status.CHECKING, Status.PASSED):
            begun = False
            for g in live:                                  # groups in order
                states = [t.t[m].status for m in g]
                assert not begun or all(s == Status.PENDING for s in states), x.id
                if not all(s in (Status.PASSED, Status.CANCELLED) for s in states):
                    begun = True
        seen_open = False
        for cid in x.criteria:                              # criteria in order
            s = t.t[cid].status
            assert not seen_open or s == Status.PENDING, (x.id, cid)
            if s != Status.PASSED:
                seen_open = True
        if x.status == Status.PASSED:
            assert all(t.t[c].status == Status.PASSED for c in x.criteria)
        for cid in [m for g in x.subtasks for m in g] + x.criteria:
            assert t.t[cid].status != Status.FAILED            # failed: out of the lists


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(trees(), st.data())
def test_any_tree_through_any_events_keeps_the_invariants(tree, data):
    t = Tickets(tree)
    t.start()
    check(t)
    for _ in range(60):
        working = [x for x in t.t.values() if x.status == Status.WORKING]
        waiting = [x for x in t.t.values() if x.status == Status.WAITING]
        if not working and not waiting:
            break
        if working:
            x = data.draw(st.sampled_from(working))
            if x.role == "criterion":
                t.verdict(x.id, data.draw(st.booleans()), [data.draw(st.sampled_from("ab"))])
            else:
                kind = data.draw(st.sampled_from(["done", "done", "failed", "system"]))
                if kind == "done":
                    t.work_done(x.id, data.draw(st.sampled_from([None, "a" * 40])))
                elif kind == "failed":
                    t.work_failed(x.id, ["f"])
                else:
                    t.system_error(x.id)
        else:
            x = data.draw(st.sampled_from(waiting))
            if x.waiting in (Waiting.SYSTEM, Waiting.BUDGET):
                t.decide(x.id, Remedy.RETRY)
            else:
                rejected = [c for c in x.criteria if t.t[c].status == Status.REJECTED
                            and t.t[c].waivable]
                if rejected:
                    t.decide(rejected[0], Remedy.WAIVE)
                else:
                    break
        check(t)
