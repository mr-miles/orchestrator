"""Out-of-process driver for the restart acceptance tests (protected support).

Reads ONE JSON scenario on stdin, prints ONE JSON result on stdout (its last line). The test
process never imports legion_orch: this driver does, in its own process, and reports what it
observed. Scenarios:

  model      the backend's REMEDIES table and Remedy values
  rules      the ticket rules (Tickets.decide) over small trees: acceptance and refusals, state
             after restart, attempts given again, criteria reset
  resolver   the actions the resolver is offered for a waiting build and a waiting grouping ticket
  engine     a change driven through the engine (real controller, fake sandboxes, real bare hub):
             an escalated build restarted by a non-owner and by a decision owner, then run to the
             end; a grouping ticket refusing restart through the human action path
  fail_sandbox, fail_branch, gone
             cleanup failures through the human action path, then success; restart of a ticket
             whose branch and sandboxes are already gone
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from engine_harness import Flow  # noqa: E402

from legion_orch.workflow.engine import WorkflowError  # noqa: E402
from legion_orch.workflow.engine.services.resolver import remedies  # noqa: E402
from legion_orch.workflow.tickets import (  # noqa: E402
    REMEDIES,
    Remedy,
    Severity,
    Status,
    Ticket,
    TicketError,
    Tickets,
    Waiting,
)

SHA = "a" * 40


# ------------------------------------------------------------------------------- rules
def T(id, parent=None, **kw) -> Ticket:
    role = "root" if parent is None else kw.pop("role", "subtask")
    return Ticket(id=id, parent=parent, role=role, **kw)


def crit(id, parent, severity=Severity.REWORK, **kw) -> Ticket:
    return T(id, parent, role="criterion", type=kw.pop("type", "agent:reviewer"),
             severity=severity, **kw)


def effect(e) -> dict:
    return {"kind": type(e).__name__, **{k: (list(v) if isinstance(v, tuple) else v)
                                         for k, v in vars(e).items() if not k.startswith("_")}}


def snap(t: Tickets, *ids) -> dict:
    return {i: json.loads(t.t[i].model_dump_json()) for i in ids}


def attempt(fn) -> dict:
    """What a call returned (its effects) or the TicketError it raised."""
    try:
        return {"ok": True, "effects": [effect(e) for e in fn()]}
    except TicketError as e:
        return {"ok": False, "error": str(e)}


def one_build(limit=3, criteria=("k1",), severity=Severity.REWORK) -> Tickets:
    t = Tickets([T("change", subtasks=[["B"]]),
                 T("B", "change", type="agent:implementer", limit=limit, criteria=list(criteria)),
                 *[crit(c, "B", severity, type="controller:verify") for c in criteria]])
    t.start()
    return t


def refusal(t: Tickets, tid: str, *watch: str) -> dict:
    before = snap(t, *watch)
    r = attempt(lambda: t.decide(tid, Remedy.RESTART))
    r["unchanged"] = before == snap(t, *watch)
    return r


def rules() -> dict:
    out: dict = {"restart_value": Remedy("restart").value}

    # --- attempts used up (limit 2): restarted, and the full limit again
    t = one_build(limit=2)
    t.work_done("B", SHA)
    t.verdict("k1", False, ["f1"])
    t.work_done("B", SHA, {"evidence": "file:x@1"})
    t.verdict("k1", False, ["f2"])
    assert t.t["B"].waiting == Waiting.ATTEMPTS
    t.t["B"].sandbox, t.t["B"].runs = "sbx", ["r1", "r2"]
    before = snap(t, "B", "k1")
    out["attempts"] = {"before": before, "decide": attempt(lambda: t.decide("B", Remedy.RESTART)),
                       "after": snap(t, "B", "k1")}
    t.work_done("B", SHA)
    t.verdict("k1", False, ["g1"])
    mid = (str(t.t["B"].status), t.t["B"].attempt)
    t.work_done("B", SHA)
    t.verdict("k1", False, ["g2"])
    out["attempts"]["then"] = {"mid": mid, "end": [str(t.t["B"].status), str(t.t["B"].waiting),
                                                   t.t["B"].attempt], "limit": t.t["B"].limit}

    # --- criteria reset: a waived criterion and a rejected one, both pending afterwards
    t = one_build(limit=2, criteria=("k1", "k2"))
    t.work_done("B", SHA)
    t.verdict("k1", False, ["a"])
    t.work_done("B", SHA)
    t.verdict("k1", False, ["a"])                       # the same again: repeated
    assert t.t["B"].waiting == Waiting.REPEATED
    t.decide("k1", Remedy.WAIVE)                        # k1 waived, k2 runs
    t.verdict("k2", False, ["b"])                       # attempts used up (limit 2)
    assert (t.t["B"].waiting, t.t["k1"].waived) == (Waiting.ATTEMPTS, True)
    out["criteria"] = {"before": snap(t, "B", "k1", "k2"),
                       "decide": attempt(lambda: t.decide("B", Remedy.RESTART)),
                       "after": snap(t, "B", "k1", "k2")}
    t.work_done("B", SHA)
    out["criteria"]["running_after_work"] = [
        i for i in ("k1", "k2") if t.t[i].status == Status.WORKING]

    # --- each waiting reason of a build accepts restart
    def waiting_build(reason: str) -> Tickets:
        if reason == "attempts":
            t = one_build(limit=1)
            t.work_done("B", SHA)
            t.verdict("k1", False, ["x"])
        elif reason == "repeated":
            t = one_build()
            t.work_done("B", SHA)
            t.verdict("k1", False, ["x"])
            t.work_done("B", SHA)
            t.verdict("k1", False, ["x"])
        elif reason == "escalated":
            t = one_build(severity=Severity.ESCALATE)
            t.work_done("B", SHA)
            t.verdict("k1", False, ["x"])
        else:
            t = one_build()
            {"system": t.system_error, "budget": t.budget_exceeded, "blocked": t.blocked}[reason]("B")
        assert (t.t["B"].status, t.t["B"].waiting) == (Status.WAITING, Waiting(reason)), reason
        return t

    out["reasons"] = {}
    for reason in ("attempts", "repeated", "escalated", "system", "budget", "blocked"):
        t = waiting_build(reason)
        r = attempt(lambda t=t: t.decide("B", Remedy.RESTART))
        r["status"], r["attempt"] = str(t.t["B"].status), t.t["B"].attempt
        out["reasons"][reason] = r

    # --- not waiting: refused, nothing changes
    out["not_waiting"] = {}
    t = one_build()
    for label, setup in (("working", lambda: None),
                         ("checking", lambda: t.work_done("B", SHA)),
                         ("passed", lambda: t.verdict("k1", True))):
        setup()
        out["not_waiting"][label] = refusal(t, "B", "change", "B", "k1") | {
            "status": str(t.t["B"].status)}

    # --- grouping tickets: escalated (a criterion failed) and empty
    t = Tickets([T("change", subtasks=[["G", "E"]]),
                 T("G", "change", subtasks=[["S"]], criteria=["sign:G"]),
                 T("S", "G", type="agent:implementer"),
                 crit("sign:G", "G", Severity.ESCALATE, type="human:signoff"),
                 T("E", "change")])
    t.start()
    t.work_done("S")
    t.verdict("sign:G", False, ["no"])
    assert (t.t["G"].waiting, t.t["E"].waiting) == (Waiting.ESCALATED, Waiting.EMPTY)
    out["grouping"] = {tid: refusal(t, tid, "change", "G", "E", "S", "sign:G") for tid in ("G", "E")}

    # --- a replan chosen: restart refused (the chosen ticket's, and another waiting build's)
    t = Tickets([T("change", subtasks=[["B1", "B2"]]),
                 T("B1", "change", type="agent:implementer"),
                 T("B2", "change", type="agent:implementer")])
    t.start()
    t.system_error("B1")
    t.system_error("B2")
    t.decide("B1", Remedy.REPLAN)
    out["replan"] = {tid: refusal(t, tid, "change", "B1", "B2") for tid in ("B1", "B2")}

    # --- a merge is owed: the ticket waits (a merge error) and refuses restart
    t = one_build()
    t.work_done("B", SHA)
    t.verdict("k1", True)
    assert t.t["B"].merging
    t.system_error("B")
    assert (t.t["B"].status, t.t["B"].waiting, t.t["B"].merging) == (
        Status.WAITING, Waiting.SYSTEM, True)
    out["merge_owed"] = refusal(t, "B", "change", "B", "k1")
    return out


def model() -> dict:
    return {"remedies": {str(k): sorted(str(r) for r in v) for k, v in REMEDIES.items()},
            "remedy_values": sorted(r.value for r in Remedy)}


def resolver() -> dict:
    def offered(waiting: Waiting, ticket: Ticket) -> list[str]:
        ticket.status, ticket.waiting = Status.WAITING, waiting
        tickets = {"change": T("change", subtasks=[[ticket.id]]), ticket.id: ticket}
        return remedies(SimpleNamespace(tickets=SimpleNamespace(t=tickets)), ticket)

    out = {}
    for w in Waiting:
        out[f"build:{w}"] = offered(w, T("B", "change", type="agent:implementer"))
        out[f"grouping:{w}"] = offered(w, T("G", "change", subtasks=[["S"]]))
    return out


# ------------------------------------------------------------------------------- engine
def git(f: Flow, *a) -> str:
    p = subprocess.run(["git", *a], cwd=f.git.repo, capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def refs(f: Flow, prefix: str) -> dict[str, str]:
    out = git(f, "for-each-ref", "--format=%(refname) %(objectname)", prefix)
    return dict(line.split(" ", 1) for line in out.splitlines())


def view(f: Flow, tid: str) -> dict:
    return json.loads(f.ticket(tid).model_dump_json())


def run_records(f: Flow, tid: str) -> dict:
    return {r.id: {"sandbox": r.sandbox, "sandbox_ref": r.sandbox_ref,
                   "sandbox_deleted": r.sandbox_deleted, "status": str(r.status),
                   "branch": r.branch, "role": r.role}
            for r in f.ce.runs.values() if r.ticket == tid}


def events(f: Flow, tid: str) -> list[dict]:
    return [e for e in f.ce.timeline_events() if e.get("ticket") == tid]


def world(f: Flow, tid: str) -> dict:
    return {"ticket": view(f, tid), "heads": refs(f, "refs/heads/"),
            "evidence": refs(f, "refs/legion/evidence/"), "deleted": list(f.runner.deleted),
            "runs": run_records(f, tid), "events": events(f, tid)}


def core(w: dict) -> dict:
    """What a refused action must leave alone (the journal aside): the ticket, the branches, the
    evidence refs, the sandboxes deleted, the run records."""
    return {k: w[k] for k in ("ticket", "heads", "evidence", "deleted", "runs")}


async def refuses(f: Flow, tid: str, action: str, approver: str = "miles") -> dict:
    """The human action path: what it raised, and what it changed."""
    before = world(f, tid)
    err = None
    try:
        await f.human(tid, action, approver)
    except WorkflowError as e:
        err = str(e)
    after = world(f, tid)
    return {"error": err, "unchanged": core(before) == core(after), "before": before,
            "after": after,
            "new_kinds": [e["kind"] for e in after["events"] if e not in before["events"]]}


async def escalate_build(f: Flow) -> None:
    """T1 implemented twice and rejected twice with the same finding: waiting (repeated)."""
    await f.to_build()
    await f.implement()
    await f.review(verdict="FAIL")
    await f.implement()
    await f.review(verdict="FAIL")
    t = f.ticket("T1")
    assert (t.status, t.waiting) == (Status.WAITING, Waiting.REPEATED), f.status()


async def engine(tmp: Path) -> dict:
    f = await Flow.create(tmp)
    out: dict = {}
    try:
        await escalate_build(f)
        out["before"] = world(f, "T1")
        out["before"]["criteria"] = {c: view(f, c) for c in f.ticket("T1").criteria}
        out["old_heads"] = {r.id: git(f, "rev-parse", f"refs/heads/{r.branch}")
                            for r in f.runs("T1") if r.branch}

        # a grouping ticket that waits: refused through the action path, nothing removed
        g = f.ce.tickets.t["builds"]
        saved = (g.status, g.waiting)
        out["grouping"] = {}
        for why in (Waiting.ESCALATED, Waiting.EMPTY):
            g.status, g.waiting = Status.WAITING, why
            out["grouping"][str(why)] = await refuses(f, "builds", "restart")
        g.status, g.waiting = saved

        # not a decision owner: refused, no change, no cleanup
        out["stranger"] = await refuses(f, "T1", "restart", "mallory")

        # a decision owner
        out["result"] = await f.human("T1", "restart", "ana")
        out["after"] = world(f, "T1")
        out["after"]["criteria"] = {c: view(f, c) for c in f.ticket("T1").criteria}
        run = f.run("T1", "implementer")
        out["new_run"] = {"id": run.id, "attempt": run.attempt, "branch": run.branch}

        # the restarted ticket works afresh and its criteria run again after the new work
        await f.implement()
        out["after_work"] = {"verify": str(f.status("verify:T1")),
                             "review": str(f.status("review:T1")), "T1": str(f.status("T1"))}
        await f.review()
        out["final"] = {"T1": str(f.status("T1")), "heads": refs(f, "refs/heads/"),
                        "new_head": git(f, "rev-parse", f"refs/heads/{run.branch}")}
        out["not_waiting"] = await refuses(f, "T1", "restart", "ana")     # passed by now
        out["ancestry"] = {}
        for rid, old in out["old_heads"].items():
            p = subprocess.run(["git", "merge-base", "--is-ancestor", old, out["final"]["new_head"]],
                               cwd=f.git.repo, capture_output=True)
            out["ancestry"][rid] = p.returncode == 0
    finally:
        await f.stop()
    return out


async def failures(tmp: Path, which: str) -> dict:
    """``fail_sandbox`` / ``fail_branch``: the removal fails, then works (a second restart);
    ``gone``: the branch and the sandboxes are removed already."""
    f = await Flow.create(tmp)
    out: dict = {}
    try:
        await escalate_build(f)
        out["before"] = world(f, "T1")
        out["live_sandboxes"] = [i for i, r in out["before"]["runs"].items()
                                 if r["sandbox_ref"] and not r["sandbox_deleted"]]
        if which == "fail_sandbox":
            f.runner.fail_delete = True
            out["refused"] = await refuses(f, "T1", "restart", "ana")
            f.runner.fail_delete = False
        elif which == "fail_branch":
            real = f.git.delete_ref

            async def no(ref):
                return False

            f.git.delete_ref = no
            out["refused"] = await refuses(f, "T1", "restart", "ana")
            f.git.delete_ref = real
        else:
            for r in f.runs("T1"):
                git(f, "update-ref", "-d", f"refs/heads/{r.branch}")
            for name in sorted({x.sandbox for x in f.runs("T1") if x.sandbox}):
                await f.ce._flow.lifecycle.sandboxes.delete(name)
            out["gone_before"] = world(f, "T1")
        await f.human("T1", "restart", "ana")
        out["after"] = {"status": str(f.status("T1")), "attempt": f.ticket("T1").attempt,
                        "findings": f.ticket("T1").findings}
    finally:
        await f.stop()
    return out


def main() -> None:
    scenario = json.load(sys.stdin)
    name = scenario["scenario"]
    try:
        if name == "model":
            res = model()
        elif name == "rules":
            res = rules()
        elif name == "resolver":
            res = resolver()
        elif name == "engine":
            with tempfile.TemporaryDirectory() as d:
                res = asyncio.run(engine(Path(d)))
        elif name in ("fail_sandbox", "fail_branch", "gone"):
            with tempfile.TemporaryDirectory() as d:
                res = asyncio.run(failures(Path(d), name))
        else:
            res = {"crash": f"unknown scenario {name}"}
    except BaseException as e:      # a crash is reported, never swallowed into a pass
        res = {"crash": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-3000:]}
    sys.stdout.write("\n" + json.dumps(res, default=str) + "\n")


if __name__ == "__main__":
    main()
