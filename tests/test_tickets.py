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
    assert t.t["T2"].status == Status.MERGING and t.merged("T2", "c" * 40) == []
    assert started(t.work_done("T1", "a" * 40)) == ["rev:T1"]     # T1's criterion; T3 waits
    assert t.verdict("rev:T1", True) == [Merge("T1", "a" * 40, "delivery")]
    assert started(t.merged("T1", "d" * 40)) == ["T3"]
    assert t.t["delivery"].sha == "d" * 40                          # its branch head
    eff = t.work_done("T3")                                         # no commit: nothing to merge
    assert eff == [Merge("delivery", "d" * 40, "change")]           # delivery's branch, into the root's
    assert started(t.merged("delivery", "e" * 40)) == ["H-3"]
    assert t.t["delivery"].status == Status.PASSED and t.t["change"].sha == "e" * 40
    assert t.verdict("H-3", True) == [Ended(Status.PASSED)]


def test_a_merge_conflict_is_a_rework_and_a_merge_error_is_retried_without_an_attempt():
    t = build()
    t.start()
    t.work_done("T2", "b" * 40)
    t.system_error("T2")                                            # the merge's backoff ran out
    assert t.t["T2"].waiting == Waiting.SYSTEM and t.t["T2"].attempt == 1
    assert t.decide("T2", Remedy.RETRY) == [Merge("T2", "b" * 40, "delivery")]
    eff = t.merge_conflict("T2", ["G-A2: rebase onto c"])
    assert eff == [StartWork("T2", 2, ("G-A2: rebase onto c",))]
    assert not t.t["T2"].merging
    with pytest.raises(TicketError):
        t.merged("T2", "c" * 40)                                    # working again, not merging


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
    t.merged("T1", "b" * 40)
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
    t.work_done("T4", "c" * 40)
    assert started(t.merged("T4", "d" * 40)) == ["integration.2"]
    assert t.t["integration"].status == Status.FAILED


def test_a_replan_waits_for_inactive_tickets_and_never_touches_closed_or_started_ones():
    t = build()
    t.start()
    with pytest.raises(TicketError, match="while the workflow is paused"):
        t.replan(Replan())
    t.pause()
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
    t.pause()
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
        assert (x.status == Status.PAUSED) == (x.held is not None), x
        assert t.paused or x.status != Status.PAUSED, x           # nothing rests once it runs
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
        if x.status in (Status.PASSED, Status.MERGING):
            assert all(t.t[c].status == Status.PASSED for c in x.criteria)
        assert not x.merging or x.status in (Status.MERGING, Status.WAITING), x
        if x.status == Status.MERGING:
            assert x.sha and x.role == "subtask", x
        for cid in [m for g in x.subtasks for m in g] + x.criteria:
            assert t.t[cid].status != Status.FAILED            # failed: out of the lists


@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(trees(), st.data())
def test_any_tree_through_any_events_keeps_the_invariants(tree, data):
    t = Tickets(tree)
    t.start()
    check(t)
    for _ in range(80):
        working = [x for x in t.t.values() if x.status in (Status.WORKING, Status.MERGING)]
        waiting = [x for x in t.t.values() if x.status == Status.WAITING]
        if data.draw(st.integers(0, 9)) == 0:                  # an operator pauses or resumes
            t.resume() if t.paused else t.pause()
            check(t)
            continue
        if not working and not waiting:
            if t.paused:
                t.resume()
                check(t)
                continue
            break
        before, eff = t.paused, []
        if working:
            x = data.draw(st.sampled_from(working))
            if x.status == Status.MERGING:
                kind = data.draw(st.sampled_from(["merged", "merged", "conflict", "system"]))
                if kind == "merged":
                    eff = t.merged(x.id, "m" * 40)
                elif kind == "conflict":
                    eff = t.merge_conflict(x.id, ["c"])
                else:
                    eff = t.system_error(x.id)
            elif x.role == "criterion":
                eff = t.verdict(x.id, data.draw(st.booleans()), [data.draw(st.sampled_from("ab"))])
            else:
                kind = data.draw(st.sampled_from(["done", "done", "failed", "system"]))
                if kind == "done":
                    eff = t.work_done(x.id, data.draw(st.sampled_from([None, "a" * 40])))
                elif kind == "failed":
                    eff = t.work_failed(x.id, ["f"])
                else:
                    eff = t.system_error(x.id)
        else:
            x = data.draw(st.sampled_from(waiting))
            if x.waiting in (Waiting.SYSTEM, Waiting.BUDGET):
                eff = t.decide(x.id, Remedy.RETRY)
            else:
                rejected = [c for c in x.criteria if t.t[c].status == Status.REJECTED
                            and t.t[c].waivable]
                if rejected:
                    eff = t.decide(rejected[0], Remedy.WAIVE)
                else:
                    break
        if before and t.paused:                                 # paused: nothing starts
            assert not [e for e in eff if isinstance(e, StartWork)], eff
        check(t)


# ------------------------------------------------------------------------------- sign-offs apply
def planning() -> Tickets:
    return Tickets([
        T("c", subtasks=[["plan"], ["delivery"]]),
        T("plan", "c", subtasks=[["design"], ["provision"]]),
        T("design", "plan", type="agent:planner", criteria=["H-E1"]),
        crit("H-E1", "design", type="human:signoff", applies="plan/tickets.json"),
        T("provision", "plan", type="controller:provision"),
        T("delivery", "c"),
    ])


def test_a_signoff_applies_its_replan_before_anything_starts():
    t = planning()
    t.start()
    assert started(t.work_done("design")) == ["H-E1"]
    plan = Replan(add=[Add(T("acceptance", type="agent:oracle"), "plan", 2),
                       Add(T("T1", type="agent:implementer"), "delivery", 0)])
    with pytest.raises(TicketError, match="inactive"):     # another ticket is working
        t.t["provision"].status = Status.WORKING
        t.verdict("H-E1", True, apply=plan)
    t.t["provision"].status = Status.PENDING
    eff = t.verdict("H-E1", True, apply=plan)
    assert started(eff) == ["provision"]                  # design passed, then plan went on
    assert t.t["plan"].subtasks == [["design"], ["provision"], ["acceptance"]]
    assert t.t["delivery"].subtasks == [["T1"]]


def test_a_refused_replan_changes_nothing():
    t = planning()
    t.start()
    t.work_done("design")
    before = {k: v.model_dump() for k, v in t.t.items()}
    bad = Replan(add=[Add(T("T1", type="agent:implementer"), "delivery", 0),
                      Add(T("T1", type="agent:implementer"), "delivery", 1)])   # a duplicate
    with pytest.raises(TicketError, match="duplicate"):
        t.verdict("H-E1", True, apply=bad)
    assert {k: v.model_dump() for k, v in t.t.items()} == before   # H-E1 still working
    quiet = Tickets([T("c", subtasks=[["delivery"]]), T("delivery", "c")])
    quiet.start()                                         # delivery is empty: it waits
    quiet.pause()
    with pytest.raises(TicketError, match="nowhere"):
        quiet.replan(Replan(add=[Add(T("T1", type="agent:implementer"), "delivery", 0),
                                 Add(T("x"), "nowhere", 0)]))
    assert quiet.t["delivery"].subtasks == [] and "T1" not in quiet.t


# ------------------------------------------------------------------------------- pause
def test_a_pause_stops_everything_at_its_next_conclusion_but_merges_conclude():
    t = build()
    assert started(t.start()) == ["T1", "T2"]
    t.pause()
    assert t.work_done("T2", "b" * 40) == [Merge("T2", "b" * 40, "delivery")]   # concludes
    assert t.merged("T2", "c" * 40) == []
    assert t.work_done("T1", "a" * 40) == []               # its criterion does not start
    assert t.t["T1"].status == Status.CHECKING and t.active() == []
    assert started(t.resume()) == ["rev:T1"]               # it goes on from where it rests
    t.pause()
    assert t.verdict("rev:T1", False, ["HIGH: x"]) == []   # the rework rests
    assert t.t["T1"].status == Status.PAUSED and t.t["T1"].held == "start"
    assert t.resume("someone else") == [] and t.paused     # only its own pause is lifted
    assert t.resume() == [StartWork("T1", 2, ("HIGH: x",))]


def test_choosing_a_replan_pauses_and_applying_it_resumes():
    t = Tickets([T("c", subtasks=[["d"]]),
                 T("d", "c", subtasks=[["T1", "T2"], ["T3"]]),
                 T("T1", "d", type="agent:implementer", criteria=["ok"]),
                 crit("ok", "T1", Severity.ESCALATE, type="controller:verify"),
                 T("T2", "d", type="agent:implementer"),
                 T("T3", "d", type="agent:implementer")])
    t.start()
    t.work_done("T1", "a" * 40)
    t.verdict("ok", False, ["fails"])                      # T1 waits; T2 still works
    t.decide("T1", Remedy.REPLAN)
    assert t.paused and t.t["T1"].status == Status.FAILED
    with pytest.raises(TicketError, match="inactive"):
        t.replan(Replan(add=[Add(T("T1b", type="agent:implementer"), "d", 1)]))
    assert t.work_done("T2") == []                         # drained: T3 does not start
    eff = t.replan(Replan(add=[Add(T("T1b", type="agent:implementer"), "d", 1)]))
    assert not t.paused and started(eff) == ["T1b"]        # the backdrop was static until now
