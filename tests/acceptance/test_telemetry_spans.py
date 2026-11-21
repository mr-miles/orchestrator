"""Acceptance (black-box, out of process): the controller's OpenTelemetry spans (AC-1 .. AC-9).

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
    "legion.change.id", "legion.workspace", "legion.stage.id", "legion.stage.title", "legion.group.id",
    "legion.node.id", "legion.node.kind", "legion.cycle", "legion.phase", "legion.result",
    "legion.outcome", "legion.run.id", "legion.run.role", "legion.escalation.kind", "legion.expected",
    "legion.resumed"}
GATES = ["H-1", "H-E1", "H-2", "H-3"]
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
    "sendback": {"scenario": "sendback", "telemetry": TELEMETRY_ON},
    "reject": {"scenario": "reject", "telemetry": TELEMETRY_ON},
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
def named(r, prefix):
    return [s for s in r["spans"] if s["name"] == prefix or s["name"].startswith(prefix + " ")]


def attr(s, key, default=None):
    return s["attributes"].get(key, default)


def steps(r, node):
    return sorted((s for s in r["spans"] if s["name"] == f"step {node}"), key=lambda s: s["start"])


def last_step(r, node):
    got = steps(r, node)
    assert got, f"no span 'step {node}'"
    return got[-1]


def index(r):
    return {s["span_id"]: s for s in r["spans"]}


def parent_of(r, s):
    return index(r).get(s["parent_span_id"])


def is_container(s):
    return s["name"].startswith(("stage ", "group ", "change "))


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
    assert named(r, "step workspace"), [s["name"] for s in r["spans"]]
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
    step = named(r, "step workspace")
    assert step and attr(step[0], "legion.outcome") == "accepted"
    root = the_root(r)[0]
    assert attr(root, "legion.outcome") == "interrupted"
    assert step[0]["end"] <= root["end"]
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


def test_AC_3_stage_spans_nest_with_titles_and_outcomes(runs):
    r = result(runs, "full")
    root = the_root(r)[0]
    stages = [s for s in r["spans"] if s["name"].startswith("stage ")]
    assert stages
    for s in stages:
        parent = parent_of(r, s)
        assert parent is not None and (parent is root or parent["name"].startswith(("stage ", "group "))), s
        assert attr(s, "legion.outcome") in ("accepted", "rework"), s["attributes"]
        assert attr(s, "legion.stage.id") and attr(s, "legion.stage.title")
        assert s["name"].startswith("stage " + attr(s, "legion.stage.title"))
    assert "Design" in {attr(s, "legion.stage.title") for s in stages}


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
RAN = ["intake", "plan", "H-1", "H-E1", "H-2", "H-3", "build:T1", "build:T2", "build:T3"]


def test_AC_4_every_node_that_ran_has_a_step_span_under_a_stage(runs):
    r = result(runs, "full")
    nodes = r["final"]["nodes"]
    assert set(RAN) <= {n for n, st in nodes.items() if st == "ACCEPTED"}, nodes
    root = the_root(r)[0]
    for node, status in nodes.items():
        if status != "ACCEPTED":
            continue
        got = steps(r, node)
        assert got, f"no span for step {node}"
        for s in got:
            assert attr(s, "legion.node.id") == node and attr(s, "legion.node.kind")
            parent = parent_of(r, s)
            assert parent is not None and (parent is root or parent["name"].startswith(("stage ", "group ")))
        assert attr(got[-1], "legion.outcome") == "accepted", (node, got[-1]["attributes"])
    for gate in GATES:
        assert attr(last_step(r, gate), "legion.node.kind") == "human"
    for task in ("build:T1", "build:T2", "build:T3"):
        assert attr(last_step(r, task), "legion.node.kind") == "build"


def test_AC_4_rejecting_at_h3_fails_that_step_and_keeps_earlier_ones_accepted(runs):
    r = result(runs, "reject")
    assert r["final"]["status"] == "ABANDONED"
    h3 = last_step(r, "H-3")
    assert attr(h3, "legion.outcome") == "failed" and h3["status_code"] == "ERROR"
    for node in ("intake", "plan", "H-1", "H-E1", "H-2", "build:T1", "build:T2", "build:T3"):
        assert attr(last_step(r, node), "legion.outcome") == "accepted", node


# ================================================================================== AC-5
def phases_of(r, step_span):
    return sorted((s for s in r["spans"]
                   if s["name"].startswith("phase ") and s["parent_span_id"] == step_span["span_id"]),
                  key=lambda s: s["start"])


@pytest.mark.parametrize("node", ["plan", "build:T1", "build:T2"])
def test_AC_5_phase_spans_under_the_step_with_repeats_as_distinct_spans(runs, node):
    r = result(runs, "full")
    phases = [p for st in steps(r, node) for p in phases_of(r, st)]
    phases.sort(key=lambda s: s["start"])
    assert phases, f"no phase spans under step {node}"
    for p in phases:
        assert attr(p, "legion.phase") and attr(p, "legion.result"), p["attributes"]
        assert p["name"] == "phase " + attr(p, "legion.phase")
        assert attr(p, "legion.node.id") == node
    names = [attr(p, "legion.phase") for p in phases]
    assert len(set(names)) < len(names), f"no repeated phase for {node}: {names}"
    repeated = [n for n in set(names) if names.count(n) > 1]
    for n in repeated:
        assert len({p["span_id"] for p in phases if attr(p, "legion.phase") == n}) == names.count(n)
    assert "rework" in {attr(p, "legion.result") for p in phases}, [attr(p, "legion.result") for p in phases]


def test_AC_5_build_t1_runs_from_produce_to_merge(runs):
    r = result(runs, "full")
    phases = sorted((p for st in steps(r, "build:T1") for p in phases_of(r, st)), key=lambda s: s["start"])
    assert attr(phases[0], "legion.phase") == "Produce"
    assert attr(phases[-1], "legion.phase") == "Merge" and attr(phases[-1], "legion.result") == "ok"


def test_AC_5_agent_spans_carry_run_id_and_role_under_a_step(runs):
    r = result(runs, "full")
    agents = [s for s in r["spans"] if s["name"].startswith("agent ")]
    assert agents
    roles = {attr(s, "legion.run.role") for s in agents}
    assert {"intake", "planner", "implementer"} <= roles and roles & {"critic", "reviewer"}, roles
    for s in agents:
        assert attr(s, "legion.run.id") and s["name"] == "agent " + attr(s, "legion.run.role")
        parent = parent_of(r, s)
        assert parent is not None and parent["name"].startswith("step "), s["name"]
        assert attr(s, "legion.node.id") == parent["name"][len("step "):]


# ================================================================================== AC-6
@pytest.mark.parametrize("gate", GATES)
def test_AC_6_gate_steps_show_awaiting_human_then_resumed(runs, gate):
    r = result(runs, "full")
    s = last_step(r, gate)
    names = [e["name"] for e in s["events"]]
    assert "awaiting_human" in names and "resumed" in names, names
    first = names.index("awaiting_human")
    assert "resumed" in names[first:]
    ev = s["events"][first]
    assert attr(ev, "legion.expected") is True
    assert ev["attributes"].get("legion.escalation.kind")
    assert attr(s, "legion.outcome") == "accepted"


def test_AC_6_a_send_back_keeps_the_finished_step_accepted_and_starts_a_new_one(runs):
    r = result(runs, "sendback")
    assert r["final"]["status"] == "READY"
    plans = steps(r, "plan")
    assert len(plans) >= 2, [(s["start"], attr(s, "legion.outcome")) for s in plans]
    assert attr(plans[0], "legion.outcome") == "accepted" and plans[0]["end"] > 0
    assert plans[1]["span_id"] != plans[0]["span_id"] and plans[1]["start"] >= plans[0]["end"]
    assert attr(plans[-1], "legion.outcome") == "accepted"


def test_AC_6_rejecting_at_h3_leaves_no_open_span_and_no_failed_phase(runs):
    r = result(runs, "reject")
    assert r["spans"]
    assert all(s["end"] > 0 and s["end"] >= s["start"] for s in r["spans"]), "an unended span arrived"
    h3 = last_step(r, "H-3")
    assert attr(h3, "legion.outcome") == "failed" and h3["status_code"] == "ERROR"
    assert "awaiting_human" in [e["name"] for e in h3["events"]]
    approval = [s for s in r["spans"]
                if s["name"] == "phase Approval" and s["parent_span_id"] == h3["span_id"]]
    assert approval, "no `phase Approval` under step H-3"
    assert attr(approval[-1], "legion.result") == "escalate" and approval[-1]["status_code"] == "UNSET"
    root = the_root(r)[0]
    assert attr(root, "legion.outcome") == "rejected" and root["status_code"] == "ERROR"
    assert not [s for s in r["spans"] if attr(s, "legion.result") == "failed"]
    late = [s["name"] for s in r["spans"] if s["name"].startswith("step ")
            and attr(s, "legion.outcome") == "accepted" and s["end"] > h3["end"]]
    assert late == []


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
    interrupted = [s for s in first if "interrupted" in (attr(s, "legion.outcome"), attr(s, "legion.result"))]
    assert any(s["name"] == f"change {cid}" for s in interrupted), [s["name"] for s in first]
    assert any(s["name"] == "step H-1" for s in interrupted)
    root2 = [s for s in second if s["name"] == f"change {cid}"]
    assert root2 and attr(root2[0], "legion.resumed") is True
    assert attr(root2[0], "legion.outcome") == "completed"
    h1 = sorted((s for s in second if s["name"] == "step H-1"), key=lambda s: s["start"])
    assert h1 and attr(h1[0], "legion.resumed") is True and attr(h1[-1], "legion.outcome") == "accepted"
    later = {s["name"] for s in second}
    assert "step plan" in later and "step H-3" in later


# ================================================================================== AC-8
@pytest.mark.parametrize("name", ["refuse", "hang", "error"])
def test_AC_8_a_bad_collector_changes_nothing_in_the_workflow(runs, name):
    healthy = result(runs, "full")
    assert healthy["spans"], "the healthy run exported nothing: telemetry is not in use"
    r = result(runs, name)
    if name != "refuse":
        assert r["requests"], "the exporter never tried the collector"
    assert r["final"]["status"] == "READY"
    assert r["final"]["nodes"] == healthy["final"]["nodes"]
    assert r["final"]["cycles"] == healthy["final"]["cycles"]
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
    assert {"legion.change.id", "legion.node.id", "legion.outcome", "legion.phase"} <= seen
    assert r["resources"]
    for res in r["resources"]:
        for key, value in res.items():
            # service.instance.id: a random id the SDK's Resource.create adds itself (no content)
            assert key in ("service.name", "legion.project", "service.instance.id") \
                or key.startswith("telemetry.sdk."), key
            if isinstance(value, str):
                assert len(value) <= 128
