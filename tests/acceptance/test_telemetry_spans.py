"""Acceptance (black-box, out of process): the controller's OpenTelemetry spans (AC-1 .. AC-9),
on tickets (docs/telemetry.md): a span per ticket under its parent's, agent runs under theirs.

The tests never import legion_orch. ``support/otel_driver.py`` is run as a subprocess; it starts
``legion-orch serve`` (local runner, scripted fake agent), drives it through the operator HTTP API
and records what the controller sends to a collector it runs (OTLP/HTTP protobuf). See
oracle/tests.md for the criterion mapping.
"""
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
DRIVER = HERE / "support" / "otel_driver.py"
DEADLINE_S = 420
COLLECTOR = "${COLLECTOR}"
PROJECT = "otel-acceptance"

GOAL = "SECRET-GOAL-TEXT say hello and goodbye"
TOKEN = "SECRET-OPERATOR-TOKEN"
# text the scripted agents (tests/fake_agent.py) write into documents, summaries and findings
NEEDLES = [GOAL, TOKEN, "SECRET-GOAL-TEXT", "Write greet/bye files and docs", "spec too thin",
           "missing newline handling", "files are the public API", "greet.txt says hello",
           "pretend secrets", "requirements captured", "not good enough"]

ATTR_KEYS = {
    "legion.change.id", "legion.workspace", "legion.ticket.id", "legion.ticket.type",
    "legion.attempt", "legion.status", "legion.waiting", "legion.outcome", "legion.run.id",
    "legion.run.role", "legion.resumed"}
SIGNOFFS = ["H-1", "H-E1", "H-2", "H-3"]
TELEMETRY_ON = {"enabled": True, "endpoint": COLLECTOR}


def run_driver(scenario: dict) -> dict:
    scenario = {"deadline_s": DEADLINE_S, "project": PROJECT, **scenario}
    try:
        p = subprocess.run([sys.executable, str(DRIVER)], input=json.dumps(scenario),
                           capture_output=True, text=True, timeout=scenario["deadline_s"] + 60)
    except subprocess.TimeoutExpired:
        return {"error": "driver timeout", "spans": [], "requests": []}
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"error": f"driver crashed ({p.returncode}): {p.stderr[-1500:]}", "spans": [],
                "requests": []}


SCENARIOS = {
    "full": {"scenario": "full", "telemetry": TELEMETRY_ON, "goal": GOAL, "operator_token": TOKEN,
             "needles": NEEDLES},
    "rejection": {"scenario": "rejection", "telemetry": TELEMETRY_ON},
    "abandon": {"scenario": "abandon", "telemetry": TELEMETRY_ON},
    "restart": {"scenario": "restart", "telemetry": TELEMETRY_ON},
    # AC-1 / AC-2: the wire, on and off
    "off_disabled": {"scenario": "wire", "stop_at": "h1", "telemetry": {"enabled": False},
                     "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR}},
    "off_unconfigured": {"scenario": "wire", "stop_at": "h1", "deadline_s": 240},
    "on_auto": {"scenario": "wire", "deadline_s": 240, "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": COLLECTOR}},
    "on_forced": {"scenario": "wire", "deadline_s": 240, "telemetry": TELEMETRY_ON},
    "on_slash": {"scenario": "wire", "deadline_s": 240,
                 "telemetry": {"enabled": True, "endpoint": COLLECTOR + "/", "service_name": "custom"}},
    # AC-8: a collector that refuses, hangs, answers 500
    "refuse": {"scenario": "full", "telemetry": TELEMETRY_ON, "collector": "refuse"},
    "hang": {"scenario": "full", "telemetry": TELEMETRY_ON, "collector": "hang"},
    "error": {"scenario": "full", "telemetry": TELEMETRY_ON, "collector": "error"},
}


@pytest.fixture(scope="module")
def runs():
    """Every scenario once, concurrently (each driver has its own ports and state dir)."""
    with ThreadPoolExecutor(max_workers=len(SCENARIOS)) as pool:
        futures = {name: pool.submit(run_driver, sc) for name, sc in SCENARIOS.items()}
        return {name: f.result() for name, f in futures.items()}


def result(runs, name) -> dict:
    r = runs[name]
    assert "error" not in r, f"scenario {name}: {r.get('error')}\n{r.get('stderr_tail', '')[-1500:]}"
    assert r.get("decode_error") is None, f"scenario {name}: collector could not decode: {r['decode_error']}"
    return r


# ---------------------------------------------------------------------------- span helpers
def named(r, name):
    return sorted((s for s in r["spans"] if s["name"] == name), key=lambda s: s["start"])


def attr(s, key, default=None):
    return s["attributes"].get(key, default)


def ticket_spans(r):
    return [s for s in r["spans"] if attr(s, "legion.ticket.type")]


def of_ticket(r, tid):
    got = [s for s in ticket_spans(r) if attr(s, "legion.ticket.id") == tid]
    assert got, f"no span of ticket {tid}"
    return sorted(got, key=lambda s: s["start"])


def index(r):
    return {s["span_id"]: s for s in r["spans"]}


def parent_of(r, s):
    return index(r).get(s["parent_span_id"])


def trace_id_for(cid: str) -> str:
    return hashlib.sha256(("legion-change:" + cid).encode()).digest()[:16].hex()


def the_root(r):
    roots = [s for s in r["spans"] if s["name"] == f"change {r['cid']}"]
    assert roots, f"no root span 'change {r['cid']}'"
    return roots


# ================================================================================== AC-1
def test_AC_1_off_when_disabled_even_with_the_endpoint_env(runs):
    r = result(runs, "off_disabled")
    assert r["requests"] == [] and r["spans"] == []
    assert r["exit_code"] == 0, r["stderr_tail"]


def test_AC_1_off_without_any_configuration(runs):
    r = result(runs, "off_unconfigured")
    assert r["requests"] == [] and r["spans"] == []
    assert r["exit_code"] == 0, r["stderr_tail"]


@pytest.mark.parametrize("name", ["on_auto", "on_forced"])
def test_AC_1_on_with_auto_endpoint_env_or_enabled_true(runs, name):
    r = result(runs, name)
    assert r["requests"], "the collector received nothing"
    assert named(r, "agent intake"), [s["name"] for s in r["spans"]]
    roots = the_root(r)
    assert len(roots) == 1 and attr(roots[0], "legion.outcome") == "interrupted"


# ================================================================================== AC-2
@pytest.mark.parametrize("name,service", [("on_forced", "legion-orchestrator"), ("on_slash", "custom")])
def test_AC_2_otlp_http_protobuf_with_resource_attributes(runs, name, service):
    r = result(runs, name)
    assert r["requests"]
    for q in r["requests"]:
        assert q["path"] == "/v1/traces", q
        assert q["content_type"].split(";")[0].strip() == "application/x-protobuf", q
        assert q["bytes"] > 0
    assert r["resources"]
    for res in r["resources"]:
        assert res.get("service.name") == service
        assert res.get("legion.project") == PROJECT
    run = named(r, "agent intake")
    assert run and attr(run[0], "legion.outcome") in ("reported", "accepted")
    root = the_root(r)[0]
    assert attr(root, "legion.outcome") == "interrupted"
    assert run[0]["end"] <= root["end"]
    assert len({s["trace_id"] for s in r["spans"]}) == 1


def test_AC_2_a_trailing_slash_on_the_endpoint_still_posts_to_v1_traces(runs):
    r = result(runs, "on_slash")
    assert r["requests"] and {q["path"] for q in r["requests"]} == {"/v1/traces"}


# ================================================================================== AC-3
def test_AC_3_one_deterministic_trace_with_one_root_ending_completed(runs):
    r = result(runs, "full")
    cid = r["cid"]
    assert r["final"]["status"] == "READY"
    assert {s["trace_id"] for s in r["spans"]} == {trace_id_for(cid)}
    parentless = [s for s in r["spans"] if not s["parent_span_id"]]
    assert [s["name"] for s in parentless] == [f"change {cid}"]
    assert attr(parentless[0], "legion.outcome") == "completed"
    assert attr(parentless[0], "legion.change.id") == cid
    ids = index(r)
    assert all(s["parent_span_id"] in ids for s in r["spans"] if s["parent_span_id"])


def test_AC_3_ticket_spans_nest_as_the_tree(runs):
    r = result(runs, "full")
    root = the_root(r)[0]
    [tree_root] = of_ticket(r, "change")
    assert parent_of(r, tree_root) is root
    for s in ticket_spans(r):
        if s is tree_root:
            continue
        parent = parent_of(r, s)
        assert parent is not None and attr(parent, "legion.ticket.type"), s["name"]
        assert s["name"] == f"{attr(s, 'legion.ticket.type')} {attr(s, 'legion.ticket.id')}"
    # a build under the delivery, its criteria under it
    [t1] = of_ticket(r, "T1")
    assert attr(parent_of(r, t1), "legion.ticket.id") == "delivery"
    [review] = of_ticket(r, "review:T1")
    assert parent_of(r, review) is t1


def test_AC_3_child_intervals_lie_inside_their_parents(runs):
    r = result(runs, "full")
    slack = 1_000_000          # 1 ms
    ids = index(r)
    checked = 0
    for s in r["spans"]:
        p = ids.get(s["parent_span_id"])
        if p is None:
            continue
        checked += 1
        assert s["start"] >= p["start"] - slack and s["end"] <= p["end"] + slack, (s["name"], p["name"])
        assert s["end"] >= s["start"]
    assert checked > 20


# ================================================================================== AC-4
def test_AC_4_every_ticket_that_ran_has_a_span_that_ends_passed(runs):
    r = result(runs, "full")
    tickets = r["final"]["tickets"]
    assert set(SIGNOFFS) | {"intake", "design", "T1", "T2", "T3", "integration"} <= {
        t for t, st in tickets.items() if st == "passed"}, tickets
    for tid, status in tickets.items():
        if status != "passed":
            continue
        spans = of_ticket(r, tid)       # a criterion that passed, then ran again: two
        assert all(attr(s, "legion.outcome") == "passed" for s in spans), (tid, spans)
    for tid in SIGNOFFS:
        assert attr(of_ticket(r, tid)[0], "legion.ticket.type") == "human:signoff"
    for tid in ("T1", "T2", "T3"):
        assert attr(of_ticket(r, tid)[0], "legion.ticket.type") == "agent:implementer"


def test_AC_4_abandoning_ends_what_is_open_and_the_root_with_an_error(runs):
    r = result(runs, "abandon")
    assert r["final"]["status"] == "ABANDONED"
    assert all(s["end"] > 0 and s["end"] >= s["start"] for s in r["spans"]), "an unended span"
    root = the_root(r)[0]
    assert attr(root, "legion.outcome") == "abandoned" and root["status_code"] == "ERROR"
    for tid in ("intake", "design", "T1", "T2", "T3"):
        assert attr(of_ticket(r, tid)[-1], "legion.outcome") == "passed", tid
    [h3] = of_ticket(r, "H-3")
    assert attr(h3, "legion.outcome") in ("abandoned", "cancelled")


# ================================================================================== AC-5
@pytest.mark.parametrize("tid", ["design", "T1", "T2"])
def test_AC_5_a_rework_is_an_event_of_the_ticket_with_its_attempt(runs, tid):
    r = result(runs, "full")
    [s] = of_ticket(r, tid)
    attempts = [e["attributes"].get("legion.attempt") for e in s["events"]]
    assert 2 in attempts, (tid, [(e["name"], e["attributes"]) for e in s["events"]])
    assert attr(s, "legion.attempt") == r["final"]["attempts"][tid] == 2
    names = [e["name"] for e in s["events"]]
    assert names[0] == "working" and "checking" in names


def test_AC_5_agent_spans_carry_run_id_and_role_under_their_ticket(runs):
    r = result(runs, "full")
    agents = [s for s in r["spans"] if s["name"].startswith("agent ")]
    assert agents
    roles = {attr(s, "legion.run.role") for s in agents}
    assert {"intake", "planner", "implementer", "critic", "reviewer"} <= roles, roles
    for s in agents:
        assert attr(s, "legion.run.id") and s["name"] == "agent " + attr(s, "legion.run.role")
        parent = parent_of(r, s)
        assert parent is not None and attr(parent, "legion.ticket.id") == attr(s, "legion.ticket.id")
    assert len([s for s in agents if attr(s, "legion.ticket.id") == "T1"]) == 2   # its rework


# ================================================================================== AC-6
@pytest.mark.parametrize("tid", SIGNOFFS)
def test_AC_6_a_sign_off_is_a_span_from_its_wait_to_its_decision(runs, tid):
    r = result(runs, "full")
    [s] = of_ticket(r, tid)
    assert [e["name"] for e in s["events"]][:1] == ["working"]
    assert attr(s, "legion.outcome") == "passed"
    assert attr(parent_of(r, s), "legion.ticket.type")          # under what it signs


def test_AC_6_a_rejected_sign_off_reworks_its_owner_in_the_same_span(runs):
    r = result(runs, "rejection")
    assert r["final"]["status"] == "READY"
    [design] = of_ticket(r, "design")
    assert attr(design, "legion.outcome") == "passed"
    assert max(e["attributes"].get("legion.attempt", 0) for e in design["events"]) >= 2
    [h_e1] = of_ticket(r, "H-E1")
    assert attr(h_e1, "legion.outcome") == "passed"
    assert "rejected" in [e["name"] for e in h_e1["events"]] or \
        len([s for s in r["spans"] if s["name"] == "agent planner"]) >= 2


# ================================================================================== AC-7
def test_AC_7_restart_continues_the_trace_and_leaves_no_open_span(runs):
    r = result(runs, "restart")
    cid = r["cid"]
    assert r["final"]["status"] == "READY"
    assert {s["trace_id"] for s in r["spans"]} == {trace_id_for(cid)}
    first = [s for s in r["spans"] if s["process"] == 1]
    second = [s for s in r["spans"] if s["process"] == 2]
    assert first and second
    assert all(s["end"] > 0 for s in r["spans"]), "an open span arrived"
    interrupted = [s for s in first if attr(s, "legion.outcome") == "interrupted"]
    names = {s["name"] for s in interrupted}
    assert f"change {cid}" in names and "human:signoff H-1" in names, sorted(names)
    root2 = [s for s in second if s["name"] == f"change {cid}"]
    assert root2 and attr(root2[0], "legion.resumed") is True
    assert attr(root2[0], "legion.outcome") == "completed"
    h1 = [s for s in second if s["name"] == "human:signoff H-1"]
    assert h1 and attr(h1[0], "legion.resumed") is True and attr(h1[-1], "legion.outcome") == "passed"
    later = {s["name"] for s in second}
    assert "agent:planner design" in later and "human:signoff H-3" in later


# ================================================================================== AC-8
@pytest.mark.parametrize("name", ["refuse", "hang", "error"])
def test_AC_8_a_bad_collector_changes_nothing_in_the_workflow(runs, name):
    healthy = result(runs, "full")
    assert healthy["spans"], "the healthy run exported nothing: telemetry is not in use"
    r = result(runs, name)
    if name != "refuse":
        assert r["requests"], "the exporter never tried the collector"
    assert r["final"]["status"] == "READY"
    assert r["final"]["tickets"] == healthy["final"]["tickets"]
    assert r["final"]["attempts"] == healthy["final"]["attempts"]
    assert r["stop_s"] is not None and r["stop_s"] <= 60, r["stop_s"]


# ================================================================================== AC-9
def test_AC_9_no_goal_token_or_document_text_in_the_collector_bytes(runs):
    r = result(runs, "full")
    assert len(r["spans"]) > 20, "nothing to inspect"
    assert set(r["raw_hits"]) == set(NEEDLES)
    leaked = [n for n, hit in r["raw_hits"].items() if hit]
    assert leaked == []


def test_AC_9_attribute_keys_are_the_allowlist_and_values_are_short(runs):
    r = result(runs, "full")
    seen = set()
    for s in r["spans"]:
        records = [s["attributes"]] + [e["attributes"] for e in s["events"]]
        for attrs in records:
            for key, value in attrs.items():
                seen.add(key)
                assert key in ATTR_KEYS, (s["name"], key)
                if isinstance(value, str):
                    assert len(value) <= 128, (s["name"], key)
    assert {"legion.change.id", "legion.ticket.id", "legion.outcome", "legion.attempt"} <= seen
    assert r["resources"]
    for res in r["resources"]:
        for key, value in res.items():
            # service.instance.id: a random id the SDK's Resource.create adds itself (no content)
            assert key in ("service.name", "legion.project", "service.instance.id") \
                or key.startswith("telemetry.sdk."), key
            if isinstance(value, str):
                assert len(value) <= 128
