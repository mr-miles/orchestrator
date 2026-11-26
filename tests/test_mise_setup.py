"""The repository's setup through mise.toml (WORKFLOW.md §5.3c): its `setup` task runs after every
checkout, from the mise.toml of the commit checked out – the base branch's, the one approved at
H-E1 (committed by the controller, checked at provision, G-PV3), or a build's approved edit –
with its tools and network checked against the environment's profiles (G-E8), and mise.toml
protected for the rest of the change (G-B3, at push and at report)."""
from __future__ import annotations

import copy
import os
import shutil
import subprocess

import pytest
from engine_harness import ENVIRONMENT, REPO, Flow, make_source_repo

from legion_orch.workflow import setup
from legion_orch.workflow.environment import EnvironmentSpec, Library
from legion_orch.workflow.guards.environment import mise_setup
from legion_orch.workflow.roles.base import guarded

SETUP = '[tools]\npython = "3.12"\nuv = "0.9"\n\n[tasks.setup]\nrun = "uv sync --frozen"\n'
TOOLS_ONLY = '[tools]\npython = "3.12"\n'


def envs(f: Flow) -> list[dict[str, str]]:
    """Each dispatched run's run.env."""
    import shlex
    return [dict(kv.split("=", 1) for kv in shlex.split(text)) for text in f.runner.run_envs]


def verified(f: Flow, ticket: str):
    """The latest verification a ``controller:verify`` ticket ran (compact: no output)."""
    runs = [v for v in f.ce.change.verifications.values() if v.node == ticket and v.result]
    return runs[-1].result


# ====================================================================== the base branch's setup
@pytest.mark.anyio
async def test_a_base_setup_task_runs_first_in_every_verifier_and_agent_sandbox(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})
    f = await Flow.create(tmp_path)
    try:
        blob = f.ce.change.base_mise                       # recorded, for information
        assert blob == setup.blob_sha(SETUP) == subprocess.run(
            ["git", "rev-parse", "HEAD:mise.toml"], cwd=tmp_path / "src-repo",
            capture_output=True, text=True).stdout.strip()
        assert f.ce.json_doc("control/workspace.json")["mise_toml"] == blob
        [intake] = f.runs("intake")
        assert ["mise-setup.sh"] not in f.steps(intake)    # intake only reads the tree
        await f.to_build()
        await f.implement()
        r = verified(f, "verify:T1").results[0]
        assert (r.command, r.setup, r.exit_code) == ("mise run setup", True, 0)
        [impl], [rev] = f.runs("T1", "implementer"), f.runs("review:T1", "reviewer")
        assert f.steps(impl)[-1] == f.steps(rev)[-1] == ["mise-setup.sh"]
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_no_setup_task_means_no_setup(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": TOOLS_ONLY})
    f = await Flow.create(tmp_path)
    try:
        assert f.ce.change.base_mise == setup.blob_sha(TOOLS_ONLY)
        await f.to_build()
        await f.implement()
        assert not verified(f, "verify:T1").results[0].setup
        assert not any(a[1].endswith("mise-setup.sh") for _, a in f.runner.execs
                       if a[0] == "bash")
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_a_failing_setup_stops_a_build_verification_as_a_failure(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})
    f = await Flow.create(tmp_path)
    try:
        f.runner.commands["mise-setup.sh"] = (1, "lockfile out of date\n")
        await f.to_build()
        await f.implement()
        assert [r.command for r in verified(f, "verify:T1").results] == ["mise run setup"]
        t = f.ticket("T1")
        assert t.attempt == 2 and any("mise run setup" in x for x in t.findings)
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_a_build_changes_mise_toml_only_with_approval_and_then_its_setup_runs(tmp_path):
    """Protected (the hub refuses the push too: test_push_scope.py). Approved, the build's own
    mise.toml is what its verification sets up from – no pin keeps the old one running."""
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})
    f = await Flow.create(tmp_path)
    try:
        await f.to_build()
        edited = SETUP.replace("uv sync --frozen", "uv sync --frozen --extra test")
        out = await f.implement(files={"src/greet.txt": "hello\n", "mise.toml": edited})
        assert not out.accepted and "G-B3: mise.toml is a protected path" in out.violations, out
        run = f.run("T1", "implementer")
        async with f.ce.lock:
            q = f.ce.request_scope("q-1", "T1", "implementer", run.id, ["mise.toml"],
                                   "the tests need the test extra installed")
        assert any("every verifier runs this file's setup task" in w for w in q.warnings)
        await f.ce.answer_question(q.id, "", "miles", "approve")
        out = await f.implement(files={"src/greet.txt": "hello\n", "mise.toml": edited})
        assert out.accepted, out.violations
        v = verified(f, "verify:T1")
        assert (v.results[0].command, v.results[0].setup, v.results[0].exit_code) == \
            ("mise run setup", True, 0)
        assert await f.git.blob(v.sha, "mise.toml") == setup.blob_sha(edited)   # its own
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_a_repository_without_mise_toml_has_no_setup_and_mise_toml_is_not_guarded(tmp_path):
    f = await Flow.create(tmp_path)
    try:
        assert f.ce.change.base_mise is None
        await f.to_build()
        assert "mise.toml" not in guarded(f.ce)
    finally:
        await f.stop()


# ====================================================================== proposed at the environment stage
def env_spec(**kw) -> dict:
    s = copy.deepcopy(ENVIRONMENT)
    s["profiles"]["check"]["offline"] = False          # the verifier may reach pypi for setup
    s |= {"mise": {"content": SETUP}, "setup_network": ["pypi"]} | kw
    return s


async def _planned_with_mise(tmp_path) -> Flow:
    f = Flow(tmp_path, environment={})
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO)
    await f.settle()
    assert f.ce.change.base_mise is None                   # nothing on the base branch
    await f.intake()
    await f.signoff("H-1")
    await f.plan(environment=env_spec())
    await f.critique("PASS")
    return f


@pytest.mark.anyio
async def test_the_approved_mise_toml_is_committed_guarded_and_checked_at_provision(tmp_path):
    f = await _planned_with_mise(tmp_path)
    try:
        branch = f"legion/{f.ce.cid}/integration"
        before = await f.git.rev_parse(f"refs/heads/{branch}")
        await f.signoff("H-E1")
        head = await f.git.rev_parse(f"refs/heads/{branch}")
        assert head != before and await f.git.blob(head, "mise.toml") == setup.blob_sha(SETUP)
        assert "mise.toml" in guarded(f.ce)                 # the environment approved one
        assert f.status("provision") == "passed"
        assert any("setup ran in the verifier" in e["message"] for e in f.ce.timeline_events())
        # the setup was checked at the commit that has the approved mise.toml – not at the
        # provision ticket's base, which has no mise.toml
        checked = [argv[2] for sb, argv in f.runner.execs
                   if sb.startswith("lgv-") and argv[1].endswith("checkout.sh")]
        assert checked == [head], (checked, head, before)
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_a_setup_that_fails_at_provision_makes_it_wait(tmp_path):
    f = await _planned_with_mise(tmp_path)
    try:
        f.runner.commands["mise-setup.sh"] = (1, "uv: lockfile missing\n")
        await f.signoff("H-E1")
        t = f.ticket("provision")
        assert t.status == "waiting" and t.waiting == "system"
        assert any("G-PV3" in x for x in t.findings)       # why, for whoever decides
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_tool_versions_come_from_the_repositorys_mise_toml_else_the_latest(tmp_path):
    """The planner names tools, never versions. The repository's mise.toml pins uv 0.13 and
    python 3.12: every image installs those; ruff, which it does not name, at its latest –
    looked up once, in the images' own mise. One map for the whole change, recorded."""
    make_source_repo(tmp_path / "src-repo",
                     {"mise.toml": '[tools]\npython = "3.12"\nuv = "0.13"\n'})
    f = await Flow.create(tmp_path, environment={})
    try:
        assert f.ce.change.base_tools == {"python": "3.12", "uv": "0.13"}
        await f.intake()
        await f.signoff("H-1")
        pinned = copy.deepcopy(ENVIRONMENT)
        pinned["profiles"]["dev"]["tools"] = ["python", "uv@0.9", "ruff"]
        out = await f.plan(environment=pinned)
        assert not out.accepted
        assert any("tool 'uv@0.9': the spec names tools, not versions" in v
                   for v in out.violations), out.violations
        out = await f.plan(environment=ENVIRONMENT)
        assert out.accepted, out.violations
        from legion_orch.server.views import environment_spec
        shown = {v["tool"]: (v["version"], v["source"]) for v in environment_spec(f.ce)["versions"]}
        assert shown == {"python": ("3.12", "mise.toml"), "uv": ("0.13", "mise.toml"),
                         "ruff": (None, "latest")}           # at H-E1: what provisioning will do
        await f.critique()
        await f.signoff("H-E1")
        await f.settle()
        build = f.ce.change.environment
        assert {k: (v.version, v.source) for k, v in build.tools.items()} == {
            "python": ("3.12", "mise.toml"), "uv": ("0.13", "mise.toml"),
            "ruff": ("0.15.0", "latest")}
        assert f.builder.looked_up == [["ruff"]]                  # only what mise.toml lacks
        tomls = {r.files["mise.toml"].split("[tools]")[1] for r in f.builder.recipes.values()
                 if "mise.toml" in r.files}
        assert tomls == {'\n"python" = "3.12"\n"ruff" = "0.15.0"\n"uv" = "0.13"\n'}  # identical
        shown = {v["tool"]: v["version"] for v in environment_spec(f.ce)["versions"]}
        assert shown["ruff"] == "0.15.0"                          # after the build: what it is
        doc = f.ce.doc("control/environment/mise.toml").content
        assert '"uv" = "0.13"' in doc and '"ruff" = "0.15.0"' in doc
    finally:
        await f.stop()


# ====================================================================== G-E8 and the mise.toml helpers
@pytest.fixture(scope="module")
def lib():
    import json

    from engine_harness import ROOT
    return Library.load(ROOT / "environments", json.loads(
        (ROOT / "assets/schemas/environment.json").read_text()))


def g_e8(lib, raw: dict, repo_mise: str | None = None) -> list[str]:
    from legion_orch.workflow.environment import resolve
    spec = EnvironmentSpec.model_validate(raw)
    profiles, problems = resolve(spec, lib)
    assert problems == []
    return mise_setup(spec, profiles, {"implementer", "oracle", "reviewer"}, repo_mise)


def test_g_e8_checks_tools_versions_and_setup_network(lib):
    assert g_e8(lib, env_spec()) == []
    v = g_e8(lib, env_spec(mise={"content": '[tools]\nbun = "1.4"\n'}))
    assert v == ["G-E8: the proposed mise.toml uses tool 'bun', which none of the spec's tools "
                 "provides: add the library tool that does (or propose one)"]
    # a version mise.toml pins is what the images install: nothing to compare any more
    assert g_e8(lib, env_spec(mise={"content": '[tools]\npython = "3.11"\n'})) == []
    offline = env_spec()
    offline["profiles"]["check"]["offline"] = True
    [v] = g_e8(lib, offline)
    assert "profile check runs the setup task but lacks its network ['pypi']" in v
    assert g_e8(lib, env_spec(mise={"content": "[tools\n"}))[0].startswith("G-E8: mise.toml does not")
    assert g_e8(lib, env_spec(mise=None)) == []


def test_g_e8_checks_the_repositorys_mise_toml_when_the_spec_proposes_none(lib):
    """The spec keeps the repository's mise.toml: its tools must all be provided."""
    assert g_e8(lib, env_spec(mise=None), repo_mise='[tools]\npython = "3.12"\nuv = "0.13"\n') == []
    v = g_e8(lib, env_spec(mise=None), repo_mise='[tools]\nbun = "1.4"\n')
    assert v and "the repository's mise.toml uses tool 'bun'" in v[0]
    assert g_e8(lib, env_spec(mise=None), repo_mise=None) == []       # no mise.toml at all


def test_one_version_per_tool_for_the_change():
    from legion_orch.sandbox import Tool
    from legion_orch.workflow.environment import planned_versions, versions_toml
    tools = [Tool(name="python", description="", mise={"python": "3.11"}),   # library: unused
             Tool(name="uv", description="", mise={"uv": "0.9"}),
             Tool(name="ruff", description="", mise={"ruff": "0.1"})]
    v = planned_versions(tools, {"python": "3.12", "uv": "latest"}, kept={"ruff": "0.14"})
    assert {k: (x.version, x.source) for k, x in v.items()} == {
        "python": ("3.12", "mise.toml"), "uv": (None, "latest"), "ruff": ("0.14", "kept")}
    assert versions_toml({"uv": "0.13", "python": "3.12"}).splitlines()[-2:] == [
        '"python" = "3.12"', '"uv" = "0.13"']


def test_mise_toml_helpers():
    assert setup.has_setup(SETUP) and not setup.has_setup(TOOLS_ONLY)
    assert setup.tools(SETUP) == {"python": "3.12", "uv": "0.9"}
    assert setup.tools('[tools]\nnode = { version = "22" }\nbun = ["1.4", "1.3"]\n') == \
        {"node": "22", "bun": "1.4"}
    with pytest.raises(setup.MiseError):
        setup.has_setup("[tasks\n")


# ====================================================================== the script itself
@pytest.mark.skipif(shutil.which("mise") is None, reason="needs mise")
@pytest.mark.parametrize(("toml", "rc", "says"), [
    (None, 0, "no mise.toml: no setup"),
    (TOOLS_ONLY.replace('python = "3.12"', ""), 0, "mise.toml has no setup task: no setup"),
    ('[tasks.setup]\nrun = "echo set up"\n', 0, "set up"),
    ('[tasks.setup]\nrun = "exit 4"\n', 4, ""),
])
def test_the_setup_script_runs_the_checkouts_own_setup_task(tmp_path, toml, rc, says):
    from legion_orch.sandbox import MISE_SETUP_SH
    script = tmp_path / "mise-setup.sh"
    script.write_text(MISE_SETUP_SH)
    work = tmp_path / "work"
    work.mkdir()
    if toml is not None:
        (work / "mise.toml").write_text(toml)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "MISE_TRUSTED_CONFIG_PATHS": "", "MISE_DATA_DIR": str(tmp_path / "mise-data")}
    r = subprocess.run(["bash", str(script)], cwd=work, env=env, capture_output=True, text=True)
    assert r.returncode == rc, r.stderr
    assert says in r.stdout
