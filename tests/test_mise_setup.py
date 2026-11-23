"""The repository's setup through mise.toml (WORKFLOW.md §5.3c): its `setup` task runs after every
checkout, only from the pinned version (G-MS1) – the base branch's, or the one approved at H-E1
(committed by the controller, checked at provision, G-PV3) – with its tools and network checked
against the environment's profiles (G-E8), and mise.toml protected for the rest of the change."""
from __future__ import annotations

import copy
import subprocess

import pytest
from engine_harness import ENVIRONMENT, REPO, Flow, make_source_repo

from legion_orch.workflow import setup
from legion_orch.workflow.environment import EnvironmentSpec, Library
from legion_orch.workflow.guards.environment import mise_setup

SETUP = '[tools]\npython = "3.12"\nuv = "0.9"\n\n[tasks.setup]\nrun = "uv sync --frozen"\n'
TOOLS_ONLY = '[tools]\npython = "3.12"\n'


def envs(f: Flow) -> list[dict[str, str]]:
    """Each dispatched run's run.env."""
    import shlex
    return [dict(kv.split("=", 1) for kv in shlex.split(text)) for text in f.runner.run_envs]


def first(f: Flow, node: str, key: str):
    return f.ce.nodes[node].verification[key].results[0]


# ====================================================================== the base branch's setup
@pytest.mark.anyio
async def test_a_base_setup_task_runs_first_in_every_verifier_and_agent_sandbox(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})

    f = await Flow.create(tmp_path)
    pin = f.ce.change.setup_pin
    assert pin and pin.source == "base" and pin.blob == setup.blob_sha(SETUP)
    assert pin.blob == subprocess.run(["git", "rev-parse", "HEAD:mise.toml"],
                                      cwd=tmp_path / "src-repo", capture_output=True,
                                      text=True).stdout.strip()
    [intake] = f.runs("intake")
    assert ["mise-setup.sh", pin.blob] not in f.steps(intake)   # intake only reads the tree
    await f.to_build()
    await f.implement()
    r = first(f, "build:T1", "1")
    assert (r.command, r.setup, r.exit_code) == ("mise run setup", True, 0)
    [impl], [rev] = f.runs("build:T1", "implementer"), f.runs("build:T1", "reviewer")
    assert f.steps(impl)[-1] == f.steps(rev)[-1] == ["mise-setup.sh", pin.blob]


@pytest.mark.anyio
async def test_no_setup_task_means_no_setup(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": TOOLS_ONLY})

    f = await Flow.create(tmp_path)
    assert f.ce.change.setup_pin is None
    await f.to_build()
    await f.implement()
    assert not first(f, "build:T1", "1").setup
    assert not any(a[1].endswith("mise-setup.sh") for _, a in f.runner.execs
                   if a[0] == "bash")


@pytest.mark.anyio
async def test_a_failing_setup_stops_a_build_verification_as_a_failure(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})

    f = await Flow.create(tmp_path)
    f.runner.commands[f.ce.change.setup_pin.blob] = (1, "lockfile out of date\n")
    await f.to_build()
    await f.implement()
    node = f.ce.nodes["build:T1"]
    assert [r.command for r in node.verification["1"].results] == ["mise run setup"]
    assert node.cycle == 2 and any("mise run setup" in fb.text for fb in node.feedback)


@pytest.mark.anyio
async def test_a_failing_setup_escalates_the_baseline(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})

    f = await Flow.create(tmp_path)
    await f.to_h2(tier="R3")
    f.runner.commands[f.ce.change.setup_pin.blob] = (1, "network unreachable\n")
    # the preflight verified the same commands at this head: what broke since is outside
    # the verification's key (the network), so it would be reused – forget it here
    f.ce.change.verifications.clear()
    r = f.run("baseline")
    doc = await f.write(r, "baseline/report.md", "results")
    await f.report(r, [f"git:{REPO}@{r.judged_sha}", doc], {"results": [
        {"command": c, "exit_code": 1} for c in f.ce.baseline_commands()]})
    base = f.ce.nodes["baseline"]
    assert base.status == "ESCALATED" and base.escalation.kind == "setup_failed"
    assert "network unreachable" in base.escalation.log_tail


@pytest.mark.anyio
async def test_a_task_may_not_change_the_pinned_mise_toml(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})

    f = await Flow.create(tmp_path)
    await f.to_build()
    out = await f.implement(files={"src/greet.txt": "hello\n", "mise.toml": SETUP + "#\n"})
    assert not out.accepted and any("mise.toml is a protected path" in v
                                    for v in out.violations), out


# ====================================================================== proposed at the environment stage
def env_spec(**kw) -> dict:
    s = copy.deepcopy(ENVIRONMENT)
    s["profiles"]["check"]["offline"] = False          # the verifier may reach pypi for setup
    s |= {"mise": {"content": SETUP}, "setup_network": ["pypi"]} | kw
    return s


@pytest.mark.anyio
async def test_the_approved_mise_toml_is_committed_pinned_and_checked_at_provision(tmp_path):
    f = Flow(tmp_path, environment={})
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO)
    await f.settle()
    assert f.ce.change.setup_pin is None                   # nothing on the base branch
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan(environment=env_spec())
    await f.critique("PASS")
    before = f.ce.change.integration_head
    await f.human("H-E1", "approve")
    ch = f.ce.change
    assert ch.integration_head != before
    assert await f.git.blob(ch.integration_head, "mise.toml") == setup.blob_sha(SETUP)
    assert ch.setup_pin.source == "H-E1" and ch.setup_pin.blob == setup.blob_sha(SETUP)
    assert f.status("provision") == "ACCEPTED"
    assert any("setup ran in the verifier" in e["message"] for e in f.ce.timeline_events())


@pytest.mark.anyio
async def test_a_setup_that_fails_at_provision_escalates_it(tmp_path):
    f = Flow(tmp_path, environment={})
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO)
    await f.settle()
    f.runner.commands[setup.blob_sha(SETUP)] = (1, "uv: lockfile missing\n")
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan(environment=env_spec())
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    node = f.ce.nodes["provision"]
    assert node.status == "ESCALATED" and node.escalation.kind == "provision_failed"
    assert "G-PV3" in node.escalation.reason


# ====================================================================== G-E8 and the mise.toml helpers
@pytest.fixture(scope="module")
def lib():
    import json

    from engine_harness import ROOT
    return Library.load(ROOT / "environments", json.loads(
        (ROOT / "assets/schemas/environment.json").read_text()))


def g_e8(lib, raw: dict) -> list[str]:
    from legion_orch.workflow.environment import resolve
    spec = EnvironmentSpec.model_validate(raw)
    profiles, problems = resolve(spec, lib)
    assert problems == []
    return mise_setup(spec, profiles, {"implementer", "oracle", "reviewer"})


def test_g_e8_checks_tools_versions_and_setup_network(lib):
    assert g_e8(lib, env_spec()) == []
    v = g_e8(lib, env_spec(mise={"content": '[tools]\nbun = "1.4"\n'}))
    assert v and all("'bun' is not installed" in x for x in v)
    v = g_e8(lib, env_spec(mise={"content": '[tools]\npython = "3.11"\n'}))
    assert v and "wants python 3.11" in v[0]
    offline = env_spec()
    offline["profiles"]["check"]["offline"] = True
    [v] = g_e8(lib, offline)
    assert "profile check runs the setup task but lacks its network ['pypi']" in v
    assert g_e8(lib, env_spec(mise={"content": "[tools\n"}))[0].startswith("G-E8: mise.toml does not")
    assert g_e8(lib, env_spec(mise=None)) == []


def test_mise_toml_helpers():
    assert setup.has_setup(SETUP) and not setup.has_setup(TOOLS_ONLY)
    assert setup.tools(SETUP) == {"python": "3.12", "uv": "0.9"}
    assert setup.tools('[tools]\nnode = { version = "22" }\nbun = ["1.4", "1.3"]\n') == \
        {"node": "22", "bun": "1.4"}
    assert setup.pin_for(TOOLS_ONLY, "base") is None and setup.pin_for(None, "base") is None
    with pytest.raises(setup.MiseError):
        setup.has_setup("[tasks\n")
