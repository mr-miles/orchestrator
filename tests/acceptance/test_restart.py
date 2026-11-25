"""Acceptance (out of process): the restart remedy for an escalated ticket (AC-1 … AC-14).

Nothing here imports legion_orch: each scenario runs ``tests/acceptance/support/restart_driver.py``
as a subprocess, which drives the ticket rules, the runner and the engine and prints one JSON
result; the tests assert on that result, on files, and on nested test runs. See
tickets/oracle/tests.md for the criterion mapping."""
import functools
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests" / "acceptance" / "support" / "restart_driver.py"
BASE = "0d1afa053a75fb8e3a2ddd6741f501933314c4ea"
BUILD_REASONS = ("attempts", "repeated", "escalated", "system", "budget", "blocked")


@functools.cache
def drive(scenario: str) -> dict:
    p = subprocess.run([sys.executable, str(DRIVER)], input=json.dumps({"scenario": scenario}),
                       capture_output=True, text=True, timeout=400, cwd=ROOT)
    lines = [x for x in p.stdout.splitlines() if x.strip()]
    try:
        res = json.loads(lines[-1])
    except (ValueError, IndexError):
        pytest.fail(f"driver {scenario} crashed ({p.returncode}): {p.stderr[-1500:]}")
    if "crash" in res:
        pytest.fail(f"driver {scenario}: {res['crash']}\n{res.get('trace', '')}")
    return res


def sh(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600, **kw)


def effects_start(decision: dict) -> list[dict]:
    return [e for e in decision["effects"] if e["kind"] == "StartWork"]


def assert_refused(r: dict):
    assert r["ok"] is False and r.get("error")
    assert r["unchanged"] is True


# ================================================================================== AC-1
def test_AC_1_restart_is_a_remedy_and_decide_starts_attempt_1():
    assert "restart" in drive("model")["remedy_values"]
    r = drive("rules")
    assert r["restart_value"] == "restart"
    d = r["attempts"]["decide"]
    assert d["ok"] is True
    starts = effects_start(d)
    assert [(e["ticket"], e["attempt"]) for e in starts] == [("B", 1)]


# ================================================================================== AC-2
def test_AC_2_a_grouping_ticket_refuses_restart_in_the_rules():
    g = drive("rules")["grouping"]
    assert_refused(g["G"])            # escalated: an escalate criterion failed
    assert_refused(g["E"])            # empty


def test_AC_2_a_grouping_ticket_refuses_restart_through_the_human_action_path():
    e = drive("engine")["grouping"]
    assert set(e) == {"escalated", "empty"}
    for why, r in e.items():
        assert r["error"], why
        assert r["after"]["ticket"]["status"] == "waiting", why
        assert r["unchanged"] is True, why                 # no branch, sandbox or state touched
        assert r["after"]["deleted"] == r["before"]["deleted"], why
        assert "cleanup" not in r["new_kinds"], why


# ================================================================================== AC-3
@pytest.mark.parametrize("reason", BUILD_REASONS)
def test_AC_3_a_waiting_build_accepts_restart_for_each_reason(reason):
    r = drive("rules")["reasons"][reason]
    assert r["ok"] is True, r
    assert (r["status"], r["attempt"]) == ("working", 1)


def test_AC_3_a_ticket_that_is_not_waiting_refuses_restart():
    for r in drive("rules")["not_waiting"].values():
        assert_refused(r)
    assert {v["status"] for v in drive("rules")["not_waiting"].values()}.isdisjoint({"waiting"})


# ================================================================================== AC-4
def test_AC_4_the_resolver_is_offered_restart_for_a_build_but_not_a_grouping_ticket():
    r = drive("resolver")
    for reason in BUILD_REASONS:
        assert "restart" in r[f"build:{reason}"], reason
    for key, offered in r.items():
        if key.startswith("grouping:"):
            assert "restart" not in offered, key


# ================================================================================== AC-5
def test_AC_5_the_branch_and_sandbox_go_and_evidence_and_other_branches_stay():
    e = drive("engine")
    before, after = e["before"], e["after"]
    ticket_branch = next(r["branch"] for r in before["runs"].values() if r["branch"])
    old_heads = set(e["old_heads"].values())
    assert old_heads and f"refs/heads/{ticket_branch}" in before["heads"]
    # the old head is no ref's any more; the new branch (if any) does not contain the old commits
    assert not old_heads & set(after["heads"].values())
    assert not any(e["ancestry"].values())
    # each sandbox of the old runs was live, is deleted and recorded so
    old_ids = [i for i, r in before["runs"].items() if r["sandbox_ref"] and not r["sandbox_deleted"]]
    assert old_ids
    for i in old_ids:
        assert after["runs"][i]["sandbox_deleted"] is True, i
        assert before["runs"][i]["sandbox"] in after["deleted"], i
    # earlier runs' evidence refs and the other tickets' branches are still there
    assert before["evidence"] and all(after["evidence"].get(k) == v
                                      for k, v in before["evidence"].items())
    others = {k: v for k, v in before["heads"].items() if k != f"refs/heads/{ticket_branch}"}
    assert all(after["heads"].get(k) == v for k, v in others.items())


# ================================================================================== AC-6
def test_AC_6_attempts_are_given_again_with_the_limit_unchanged():
    a = drive("rules")["attempts"]
    assert a["before"]["B"]["attempt"] == a["before"]["B"]["limit"] == 2
    assert a["after"]["B"]["attempt"] == 1 and a["after"]["B"]["limit"] == 2
    assert a["after"]["B"]["status"] == "working"
    assert a["then"]["mid"] == ["working", 2]                    # not waiting at attempt 1's failure
    assert a["then"]["end"] == ["waiting", "attempts", 2] and a["then"]["limit"] == 2


# ================================================================================== AC-7
def test_AC_7_the_ticket_starts_afresh_in_the_rules():
    a = drive("rules")["attempts"]
    d, b = a["decide"], a["after"]["B"]
    assert [(e["ticket"], e["attempt"], e["findings"]) for e in effects_start(d)] == [("B", 1, [])]
    assert (b["status"], b["attempt"], b["findings"]) == ("working", 1, [])
    assert b["waiting"] is None and b["sha"] is None and b["sandbox"] is None
    assert b["runs"] == [] and b["outputs"] == {} and b["merging"] is False and b["held"] is None
    assert a["before"]["B"]["sha"] and a["before"]["B"]["runs"] and a["before"]["B"]["outputs"]
    c = drive("rules")["criteria"]
    assert c["before"]["k1"]["waived"] is True and c["before"]["k2"]["status"] == "rejected"
    for k in ("k1", "k2"):
        x = c["after"][k]
        assert (x["status"], x["findings"], x["waived"]) == ("pending", [], False), k
    assert c["running_after_work"] == ["k1"]                     # the criteria run after new work


def test_AC_7_the_restarted_engine_ticket_is_working_afresh_and_its_criteria_run_again():
    e = drive("engine")
    t = e["after"]["ticket"]
    assert (t["status"], t["attempt"], t["findings"], t["waiting"]) == ("working", 1, [], None)
    assert t["runs"] == [e["new_run"]["id"]] and e["new_run"]["attempt"] == 1
    assert t["merging"] is False and t["held"] is None
    for c, x in e["after"]["criteria"].items():
        assert (x["status"], x["findings"], x["waived"]) == ("pending", [], False), c
    assert e["after_work"]["verify"] == "passed" and e["after_work"]["review"] == "working"
    assert e["final"]["T1"] == "passed"


# ================================================================================== AC-8
def test_AC_8_a_restart_by_a_non_owner_is_refused_with_no_change_and_no_cleanup():
    s = drive("engine")["stranger"]
    assert s["error"]
    assert s["unchanged"] is True
    assert s["after"]["ticket"]["status"] == "waiting"
    assert s["after"]["deleted"] == s["before"]["deleted"]
    assert not {"decision", "cleanup"} & set(s["new_kinds"])


def test_AC_8_a_decision_owner_restart_is_journalled_and_keeps_the_run_records():
    e = drive("engine")
    ev = e["after"]["events"]
    decisions = [x for x in ev if x["kind"] == "decision" and "restart" in x["message"]]
    assert len(decisions) == 1 and "ana" in decisions[0]["message"] and decisions[0]["ticket"] == "T1"
    assert [x for x in ev if x["kind"] == "cleanup"]
    for rid, rec in e["before"]["runs"].items():                  # earlier run records remain
        assert rid in e["after"]["runs"]
        assert e["after"]["runs"][rid]["status"] == rec["status"]
        assert e["after"]["runs"][rid]["role"] == rec["role"]


# ================================================================================== AC-10
def test_AC_10_the_resolution_skill_and_the_c4_doc_mention_restart():
    skill = (ROOT / "assets" / "skills" / "resolution-protocol.md").read_text().lower()
    assert "restart" in skill
    c4 = (ROOT / "docs" / "c4.md").read_text().lower()
    assert re.search(r"discard", c4)


# ================================================================================== AC-11
def test_AC_11_restart_while_a_replan_is_chosen_is_refused_with_no_change():
    for r in drive("rules")["replan"].values():
        assert_refused(r)


def test_AC_11_restart_of_a_ticket_that_is_not_waiting_is_refused_through_the_action_path():
    r = drive("engine")["not_waiting"]
    assert r["error"]
    assert r["unchanged"] is True
    assert not {"decision", "cleanup"} & set(r["new_kinds"])


# ================================================================================== AC-12
@pytest.mark.parametrize("scenario", ["fail_sandbox", "fail_branch"])
def test_AC_12_a_failed_removal_keeps_the_ticket_waiting_and_a_second_restart_succeeds(scenario):
    r = drive(scenario)
    assert r["live_sandboxes"]
    refused = r["refused"]
    assert refused["error"]
    b, a = refused["before"]["ticket"], refused["after"]["ticket"]
    assert a["status"] == "waiting"
    assert (a["attempt"], a["findings"], a["waiting"]) == (b["attempt"], b["findings"], b["waiting"])
    assert r["after"]["status"] == "working" and r["after"]["attempt"] == 1
    assert r["after"]["findings"] == []


def test_AC_12_restarting_when_branch_and_sandbox_are_already_gone_succeeds():
    r = drive("gone")
    assert r["live_sandboxes"]
    gone = r["gone_before"]
    assert gone["ticket"]["status"] == "waiting"
    assert not any(h.endswith("/t/T1") for h in gone["heads"])
    assert all(x["sandbox_deleted"] for x in gone["runs"].values() if x["sandbox_ref"])
    assert r["after"]["status"] == "working" and r["after"]["attempt"] == 1


# ================================================================================== AC-13
def test_AC_13_a_waiting_ticket_whose_merge_is_owed_refuses_restart():
    assert_refused(drive("rules")["merge_owed"])


# ================================================================================== AC-14
def base_text(rel: str) -> str:
    p = sh(["git", "show", f"{BASE}:{rel}"])
    return p.stdout if p.returncode == 0 else ""


TOKEN = {"tests/test_tickets.py": r"Remedy\.RESTART|restart",
         "tests/test_ticket_runner.py": r"Remedy\.RESTART|restart",
         "tests/test_resolver.py": r"restart"}


def test_AC_14_tests_restart_py_exists_and_passes():
    assert (ROOT / "tests" / "test_restart.py").is_file()
    p = sh([sys.executable, "-m", "pytest", "-q", "-n", "0", "tests/test_restart.py"])
    assert p.returncode == 0, p.stdout[-2500:] + p.stderr[-1000:]


@pytest.mark.parametrize("rel", ["tests/test_tickets.py", "tests/test_ticket_runner.py",
                                 "tests/test_resolver.py"])
def test_AC_14_each_python_suite_has_a_restart_test_that_passes(rel):
    text = (ROOT / rel).read_text()
    assert len(re.findall(TOKEN[rel], text)) > len(re.findall(TOKEN[rel], base_text(rel))), rel
    assert re.search(r"def test_\w*restart\w*", text), f"{rel}: no test named for restart"
    p = sh([sys.executable, "-m", "pytest", "-q", "-n", "0", "-k", "restart", rel])
    assert p.returncode == 0, p.stdout[-2500:] + p.stderr[-1000:]
