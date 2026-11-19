"""Preflight (WORKFLOW.md §5.4a): at R2+ the controller runs the oracle's setup and acceptance
commands in a fresh verifier sandbox before the baseline and H-2 – setup first in every verifier
(G-V3), install-only commands kept out of the other lists (G-P13)."""
from __future__ import annotations

import pytest
from engine_harness import ORACLE, TASKS, Flow, scenario

from legion_orch.workflow.guards.plan import setup_steps

INSTALL = "bun install --frozen-lockfile"
WITH_SETUP = ORACLE | {"setup_commands": [INSTALL]}
ACCEPT = ORACLE["acceptance_commands"][0]


def results(f: Flow, node: str, key: str) -> list[tuple[str, int, bool]]:
    v = f.ce.nodes[node].verification[key]
    return [(r.command, r.exit_code, r.setup) for r in v.results]


@scenario()
async def test_r2_runs_preflight_after_the_oracle_and_before_h2(f: Flow):
    await f.to_h2(tier="R2", oracle=WITH_SETUP)
    order = list(f.ce.nodes)
    assert order.index("oracle") < order.index("preflight") < order.index("H-2")
    assert f.status("preflight") == "ACCEPTED" and f.status("H-2") == "AWAITING_HUMAN"
    assert results(f, "preflight", "preflight") == [(INSTALL, 0, True), (ACCEPT, 0, False)]
    pre = f.ce.nodes["preflight"].verification["preflight"]
    assert pre.sha == f.ce.change.integration_head and pre.attestation      # the oracle is merged
    notes = [e["message"] for e in f.ce.timeline_events() if e["kind"] == "preflight"]
    assert notes and "already passes" in notes[0]                # the fake runner passes it


@scenario()
async def test_r3_baseline_follows_preflight(f: Flow):
    await f.to_h2(tier="R3")
    assert f.ce.nodes["baseline"].depends_on == ["preflight"]
    assert f.status("preflight") == "ACCEPTED"


@scenario()
async def test_a_failing_setup_sends_the_tests_back_to_the_oracle(f: Flow):
    f.runner.commands[INSTALL] = (1, "error: lockfile had changes, but lockfile is frozen\n")
    await f.to_h2(tier="R2", oracle=WITH_SETUP)
    oracle, pre = f.ce.nodes["oracle"], f.ce.nodes["preflight"]
    assert oracle.status == "RUNNING" and oracle.cycle == 2          # reworked in its session
    [fb] = [x for x in oracle.feedback if "preflight" in x.text]
    assert INSTALL in fb.text and "lockfile is frozen" in fb.text and fb.attestations
    assert pre.status == "PENDING"
    assert results(f, "preflight", "preflight") == [(INSTALL, 1, True)]   # nothing ran after it
    del f.runner.commands[INSTALL]
    await f.oracle(files={ORACLE["acceptance_tests"][0]: "grep -q hello src/greet.txt # v2\n"})
    assert oracle.status == "ACCEPTED" and pre.status == "ACCEPTED" and f.status("H-2") == "AWAITING_HUMAN"


@scenario(review_max_cycles=1)
async def test_preflight_escalates_once_the_oracle_budget_is_spent(f: Flow):
    f.runner.commands[INSTALL] = (1, "network unreachable\n")
    await f.to_h2(tier="R2", oracle=WITH_SETUP)
    pre = f.ce.nodes["preflight"]
    assert pre.status == "ESCALATED" and pre.escalation.kind == "setup_failed"
    assert "network unreachable" in pre.escalation.log_tail
    # the fix is in the plan: send it back from the escalation
    await f.human("preflight", "revise_plan", feedback="the verifier is offline: vendor the deps")
    assert f.status("plan") == "RUNNING" and pre.status == "PENDING"


@scenario()
async def test_a_command_the_verifier_lacks_escalates_and_retry_runs_it_again(f: Flow):
    f.runner.commands[ACCEPT] = (127, "sh: bun: not found\n")
    await f.to_h2(tier="R2")
    pre = f.ce.nodes["preflight"]
    assert pre.status == "ESCALATED" and pre.escalation.kind == "verify_unrunnable"
    del f.runner.commands[ACCEPT]
    await f.human("preflight", "retry")
    assert pre.status == "ACCEPTED" and f.status("H-2") == "AWAITING_HUMAN"


@scenario()
async def test_every_verifier_runs_the_setup_commands_first(f: Flow):
    await f.to_build(tier="R2", oracle=WITH_SETUP)
    await f.implement()
    assert results(f, "build:T1", "1") == [(INSTALL, 0, True), (TASKS[0]["verification"][0], 0, False)]
    await f.review()
    await f.integration()
    assert results(f, "integration", "integration")[0] == (INSTALL, 0, True)


@scenario()
async def test_g_p13_keeps_install_only_commands_out_of_the_other_lists(f: Flow):
    await f.intake()
    await f.human("H-1", "accept", tier="R2")
    out = await f.plan(oracle=ORACLE | {"integration_commands": [INSTALL, "bun test"]})
    assert not out.accepted and any(v.startswith("G-P13: `bun install") for v in out.violations)


@pytest.mark.parametrize("cmd", [INSTALL, "npm ci", "  pnpm i", "uv sync --frozen", "pip install -e .",
                                 "poetry install", "go mod download", "yarn install"])
def test_install_only_commands(cmd):
    assert setup_steps({"acceptance_commands": [cmd]}), cmd


@pytest.mark.parametrize("cmd", [f"{INSTALL} && bun test", "bun test", "npm test", "pytest -q",
                                 "bun run install-hooks", "uv run pytest"])
def test_other_commands_are_fine(cmd):
    assert setup_steps({"integration_commands": [cmd]}) == [], cmd
