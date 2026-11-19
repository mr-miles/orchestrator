"""The sandbox environment: the planner's spec, critique, H-E1, provisioning and attaching the
environment to sandboxes (WORKFLOW.md §5.3a)."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil

import pytest
import yaml
from engine_harness import ENVIRONMENT, ORACLE, ROOT, TASKS, Flow, make_config, scenario

from legion_orch.config import load_config
from legion_orch.sandbox import (
    Endpoint,
    ImageRecipe,
    Policy,
    Resources,
    SandboxEnv,
    SandboxSpec,
    Tool,
    driver_config,
    merge_network,
    policy_entry,
    render_policy,
)
from legion_orch.sandbox.images import dockerfile, image_tag, mise_toml
from legion_orch.workflow.assets import Assets
from legion_orch.workflow.engine import Controller, WorkflowError
from legion_orch.workflow.environment import (
    EnvironmentSpec,
    Kind,
    Library,
    LibraryError,
    binary_paths,
    resolve,
    resolve_tool,
)
from legion_orch.workflow.model import RUNNING

ENV = {"environment": {}}
SCHEMA = json.loads((ROOT / "assets/schemas/environment.json").read_text())
PYPI = (Endpoint(host="pypi.org", port=443),)


def spec(**changes) -> dict:
    s = copy.deepcopy(ENVIRONMENT)
    s.update(changes)
    return s


def library() -> Library:
    return Library.load(ROOT / "environments", SCHEMA)


def own_library(f: Flow) -> Library:
    """A private copy of the library for tests that edit it (the config reads it fresh)."""
    shutil.copytree(ROOT / "environments", f.tmp / "lib")
    f.cfg.environment.library_dir = str(f.tmp / "lib")
    return Library.load(f.tmp / "lib", SCHEMA)


def sandboxes(f: Flow, role: str, task: str | None = None) -> list[SandboxSpec]:
    return [sp for sp in f.runner.specs.values()
            if sp.role == role and (task is None or sp.task == task)]


async def to_plan(f: Flow) -> None:
    """Up to the planner's run."""
    await f.intake()
    await f.human("H-1", "accept")


# ====================================================================== graph and flow
@scenario()
async def test_without_environment_config_the_graph_is_unchanged(f: Flow):
    await f.to_build()
    assert not {"environment", "H-E1", "provision"} & set(f.ce.nodes)
    assert all(sp.env is None for sp in f.runner.specs.values())


@scenario(**ENV)
async def test_environment_is_specified_critiqued_approved_provisioned_and_attached(f: Flow):
    f.cfg.sandbox.image, f.cfg.sandbox.verifier_image = "reg/agent:1", "reg/verifier:1"
    await f.intake()
    await f.human("H-1", "accept")
    assert f.ce.nodes["H-E1"].depends_on == ["plan"] and "environment" not in f.ce.nodes
    assert "`pypi` – Python Package Index" in f.run("plan").assignment["environment"]
    await f.plan()
    assert any("plan/environment.json" in r for r in f.run("plan", "critic").context_refs)
    await f.critique("PASS")
    assert f.status("H-E1") == "AWAITING_HUMAN" and f.status("provision") == "PENDING"
    await f.human("H-E1", "approve")
    assert f.status("provision") == "ACCEPTED"
    roles = f.ce.change.environment.roles
    assert {r: e.profile for r, e in roles.items()} == ENVIRONMENT["roles"]
    impl, ver, rev = roles["implementer"], roles["verifier"], roles["reviewer"]
    assert impl.image.startswith("localhost/legion-env:") and impl.image != ver.image
    assert rev.image != ver.image                    # same profile, agent vs verifier base image
    assert list(impl.network) == ["env_pypi"]
    assert ver.network == rev.network == {}          # check is offline
    assert (impl.cpu, impl.memory, impl.driver_config) == ("2", "4096Mi", None)
    assert (ver.cpu, ver.memory, ver.driver_config) == (None, None, None)
    assert f.ce.latest("control/environment/build.json") and f.ce.latest("control/logs/provision.log")
    recipe = f.builder.recipes[impl.image]
    assert "COPY mise.toml /etc/mise/config.toml" in recipe.files["Dockerfile"]
    assert '"python" = "3.12"' in recipe.files["mise.toml"]
    assert {e["host"] for e in impl.network["env_pypi"]["endpoints"]} >= {"pypi.org"}

    await f.human("H-2", "approve")
    name = f.run("build:T1", "implementer").sandbox
    tools = f.run("build:T1", "implementer").template_vars["sandbox_tools"]
    assert tools.startswith("profile `dev`: python@3.12, uv, ruff. Build, test and verify only")
    sp = f.runner.specs[name]
    assert sp.env == SandboxEnv(image=impl.image, network=impl.network, cpu="2", memory="4096Mi")
    # a credentialless policy is a rule in the sandbox's own policy; controller access is not
    # (it comes from the attached provider profiles)
    rules = f.runner.policies[name]["network_policies"]
    assert set(rules) == {"env_pypi"}
    assert rules["env_pypi"] == impl.network["env_pypi"] and rules["env_pypi"]["binaries"]
    assert sp.push                                   # the implementer gets legion-git-push
    await f.implement()
    verifiers = sandboxes(f, "verifier")
    assert verifiers and all(v.env.image == ver.image and v.env.network == {} for v in verifiers)
    assert all(f.runner.policies[v.name]["network_policies"] == {} and not v.push
               for v in verifiers)
    await f.review()
    assert all(r.env.image == rev.image and not r.push for r in sandboxes(f, "reviewer"))
    assert f.status("build:T1") == "ACCEPTED"
    assert all(sp.env is None for sp in f.runner.specs.values()
               if sp.role in ("intake", "planner", "critic"))


@scenario(**ENV)
async def test_r2_oracle_and_integration_run_in_the_environment(f: Flow):
    await f.to_build(tier="R2")
    assert f.ce.nodes["oracle"].depends_on == ["provision"]
    roles = f.ce.change.environment.roles
    [oracle] = sandboxes(f, "oracle")
    assert oracle.env.image == roles["oracle"].image
    await f.implement()
    await f.review()
    await f.integration(verdict="PASS")
    assert f.status("integration") == "ACCEPTED"
    integ = sandboxes(f, "verifier", "integration")
    assert integ and all(v.env.image == roles["verifier"].image for v in integ)
    assert all(v.env is None for v in sandboxes(f, "integration"))     # integration is unmapped


@scenario(**ENV)
async def test_identical_environments_reuse_images(f: Flow):
    await f.to_build()
    first = list(f.builder.built)
    (f.tmp / "second").mkdir()
    g = await Flow.create(f.tmp / "second", goal="again")     # another controller, same spec
    g.builder = g.ctl.svc.builder = f.builder
    await g.to_build()
    assert f.builder.built == first      # nothing new built: the same recipes were found


# ====================================================================== guards
@scenario(**ENV)
async def test_unknown_tool_and_policy_are_rejected(f: Flow):
    await to_plan(f)
    bad = spec(profiles={"dev": {"tools": ["python", "cobol"], "network": {"python": ["everything"]}}},
               roles={"implementer": "dev", "reviewer": "nope"})
    out = await f.plan(environment=bad)
    assert not out.accepted
    text = " | ".join(out.violations)
    assert "G-E1: profile dev: unknown tool 'cobol'" in text
    assert "unknown network policy 'everything' for tool python" in text
    assert "mapped to unknown profile 'nope'" in text


@scenario(**ENV)
async def test_proposals_are_usable_but_cannot_target_the_controller_or_shadow(f: Flow):
    await to_plan(f)
    bad = spec(proposed={
        "tools": {"python": {"description": "shadow", "mise": {"python": "3.9"}}},
        "policies": {"home": {"description": "controller",
                              "endpoints": [{"host": "host.openshell.internal", "port": 8766}]}}},
        profiles={"dev": {"tools": ["python", "uv"], "network": {"uv": ["home"]}}},
        roles={"implementer": "dev"})
    text = " | ".join((await f.plan(environment=bad)).violations)
    assert "G-E1: proposed tool 'python' shadows" in text
    assert "G-E2: network policy home: host.openshell.internal:8766" in text
    good = spec(proposed={"tools": {"taplo": {"description": "taplo", "mise": {"taplo": "0.9"}}}},
                profiles={"dev": {"tools": ["python", "taplo"]}}, roles={"implementer": "dev"})
    assert (await f.plan(environment=good)).accepted


@scenario(environment={"max_memory_mib": 1024})
async def test_resources_over_the_cap_and_early_roles_are_rejected(f: Flow):
    await to_plan(f)
    out = await f.plan(environment=spec(roles={"implementer": "dev", "planner": "dev"}))
    text = " | ".join(out.violations)
    assert "G-E3: profile dev: memory_mib 4096 exceeds environment.max_memory_mib (1024)" in text
    assert "G-E4: role 'planner' cannot be given an environment" in text


@scenario(**ENV)
async def test_tool_env_credentials_and_conflicts_are_rejected(f: Flow):
    await to_plan(f)
    bad = spec(proposed={"tools": {
        "gh-cli": {"description": "gh", "mise": {"gh": "2"}, "env": {"GH_TOKEN": "abc"}},
        "uv-online": {"description": "uv", "mise": {"ruff": "0.14"},
                      "env": {"UV_PYTHON_DOWNLOADS": "automatic"}}}},
        profiles={"dev": {"tools": ["uv", "uv-online", "gh-cli"]}}, roles={"implementer": "dev"})
    text = " | ".join((await f.plan(environment=bad)).violations)
    assert "G-E2: tool gh-cli: environment variable GH_TOKEN looks like a credential" in text
    assert "conflicting tool environment: UV_PYTHON_DOWNLOADS is 'never' in uv but 'automatic'" in text


@scenario(**ENV)
async def test_network_extras_must_name_the_profiles_tools_and_reach_a_binary(f: Flow):
    await to_plan(f)
    bad = spec(proposed={"policies": {"pkgs": {"description": "x",
                                               "endpoints": [{"host": "pkgs.example.com", "port": 443}]}},
                         "tools": {"docs": {"description": "x", "apt": ["pandoc"], "network": ["pkgs"]}}},
               profiles={"dev": {"tools": ["node", "docs"], "network": {"uv": ["pkgs"]}}},
               roles={"implementer": "dev"})
    text = " | ".join((await f.plan(environment=bad)).violations)
    assert "network extras for 'uv', which is not one of its tools" in text
    assert "network policy pkgs is for tools ['docs'], which declare no binaries" in text


@scenario(**ENV)
async def test_an_invalid_library_is_reported_to_the_agent_and_rejects_its_spec(f: Flow):
    own_library(f)
    (f.tmp / "lib/tools/broken.yaml").write_text("description: [unclosed\n")
    await to_plan(f)
    assert "the library is invalid" in f.run("plan").assignment["environment"]
    out = await f.plan()
    assert not out.accepted and "G-E1: the environment library is invalid" in out.violations[0]


def test_schema_has_no_place_for_credentials_or_unsafe_env():
    from jsonschema import Draft202012Validator
    s = spec(proposed={"policies": {"gh": {
        "description": "x", "credentials": [{"name": "token"}],
        "endpoints": [{"host": "api.github.com", "port": 443}]}}})
    assert not Draft202012Validator(SCHEMA).is_valid(s)
    v = Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": "#/$defs/tool"})
    ok = {"description": "x", "mise": {"x": "1"}}
    assert v.is_valid(ok | {"env": {"X_MODE": "offline"}})
    for bad in ({"PATH": "/x"}, {"MISE_DATA_DIR": "/x"}, {"lower": "x"},
                {"X": "a\nRUN rm -rf /"}, {"X": "$HOME"}, {"X": 'a"b'}):
        assert not v.is_valid(ok | {"env": bad}), bad


# ====================================================================== gate, resets, failures
@scenario(**ENV)
async def test_the_planner_must_attest_the_environment_spec(f: Flow):
    await to_plan(f)
    r = f.run("plan")
    atts = [await f.write(r, "plan/spec.md", "# Spec\n"),
            await f.write(r, "plan/oracle.json", ORACLE),
            await f.write(r, "plan/taskgraph.json", {"tasks": TASKS}),
            await f.write(r, "plan/specs/REQ-1.md", "# REQ-1\n\n## Current behaviour\nx\n\n"
                          "## Change\ny\n\n## Acceptance criteria\n- z\n")]
    out = await f.report(r, atts)
    assert not out.accepted and any("G-E1: " in v and "plan/environment.json not attested" in v
                                    for v in out.violations)


@scenario()
async def test_without_the_stage_the_planner_is_told_not_to_write_a_spec(f: Flow):
    await to_plan(f)
    assert "do not write `plan/environment.json`" in f.run("plan").assignment["environment"]


@scenario(**ENV)
async def test_h_e1_revise_reworks_the_plan_and_recritiques(f: Flow):
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "revise", feedback="the verifier needs node for the lint step")
    plan = f.ce.nodes["plan"]
    assert plan.cycle == 2 and plan.status == "RUNNING" and f.status("H-E1") == "PENDING"
    assert "node for the lint" in f.run("plan").assignment["feedback_items"][-1]["text"]
    await f.plan(environment=spec(profiles={**ENVIRONMENT["profiles"],
                                            "check": {"tools": ["python", "uv", "node"]}}))
    assert f.status("plan") == "REVIEWING" and f.run("plan", "critic").cycle == 2
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    assert f.status("provision") == "ACCEPTED"


@scenario(**ENV)
async def test_a_plan_rework_may_keep_the_environment_spec(f: Flow):
    await to_plan(f)
    await f.plan()
    first = f.ce.latest("plan/environment.json")
    await f.critique("REWORK")
    assert f.status("plan") == "RUNNING"
    assert (await f.plan(keep_environment=True)).accepted
    assert f.ce.latest("plan/environment.json") == first     # not rewritten
    await f.critique("PASS")
    assert f.status("H-E1") == "AWAITING_HUMAN"


@scenario(**ENV)
async def test_a_rework_must_attest_the_latest_environment_spec(f: Flow):
    await to_plan(f)
    await f.plan()
    old = f.ce.latest_att("plan/environment.json")
    await f.critique("REWORK")
    f.ce.write_record("plan/environment.json", json.dumps(spec(summary="newer")))
    r = f.run("plan")
    atts = [await f.write(r, p, f.ce.doc(p).content) for p in
            ("plan/spec.md", "plan/oracle.json", "plan/taskgraph.json", "plan/specs/REQ-1.md")]
    out = await f.report(r, [*atts, old])
    assert not out.accepted and any("is not the latest version" in v for v in out.violations)


@scenario(critique_max_cycles=1, **ENV)
async def test_critique_budget_answer_reworks_the_plan(f: Flow):
    await to_plan(f)
    await f.plan()
    await f.critique("REWORK")
    assert f.ce.nodes["plan"].escalation.kind == "critique_budget"
    await f.human("plan", "answer", answer="keep the single task")
    assert f.status("plan") == "RUNNING" and f.status("H-E1") == "PENDING"


@scenario(**ENV)
async def test_h2_revise_resets_environment_approval_and_build(f: Flow):
    await f.to_h2()
    assert f.ce.change.environment
    await f.human("H-2", "revise", feedback="split T1 in two")
    assert f.ce.change.environment is None and "H-E1" not in f.ce.change.approvals
    assert f.status("H-E1") == "PENDING" and f.status("provision") == "PENDING"
    assert f.status("plan") == "RUNNING"


@scenario(**ENV)
async def test_provision_failure_escalates_and_retry_provisions_again(f: Flow):
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    f.builder.fail = "podman build failed (1): mise: python 3.12 not found"
    await f.human("H-E1", "approve")
    node = f.ce.nodes["provision"]
    assert node.status == "ESCALATED" and node.escalation.kind == "provision_failed"
    assert "G-PV1" in node.escalation.reason and "mise: python" in node.escalation.reason
    assert f.ce.change.environment is None
    await f.human("provision", "retry")
    assert node.status == "ACCEPTED" and f.ce.change.environment


@scenario(**ENV)
async def test_library_changed_after_approval_escalates_provisioning(f: Flow):
    """The approved spec is resolved again at provisioning: a library entry removed (or a
    library broken) since then stops the build instead of building something else."""
    own_library(f)
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    (f.tmp / "lib/tools/uv.yaml").unlink()
    await f.human("H-E1", "approve")
    node = f.ce.nodes["provision"]
    assert node.status == "ESCALATED"
    assert "no longer resolves against the library" in node.escalation.reason
    assert "unknown tool 'uv'" in node.escalation.reason and not f.builder.built
    (f.tmp / "lib/tools/uv.yaml").write_text("mise: {uv: '0.9'}\n")           # invalid now
    await f.human("provision", "retry")
    assert "the environment library is invalid" in node.escalation.reason
    shutil.copy(ROOT / "environments/tools/uv.yaml", f.tmp / "lib/tools/uv.yaml")
    await f.human("provision", "retry")
    assert node.status == "ACCEPTED"


@scenario(**ENV)
async def test_stale_environment_approval_blocks_dispatch(f: Flow):
    """G-E5: a spec changed after approval is never used to create an agent sandbox."""
    await f.to_h2()
    await f.ce.human_action("H-2", "approve", "miles", {})
    f.ce.write_record("plan/environment.json", json.dumps(spec(summary="edited after approval")))
    await f.settle()
    node = f.ce.nodes["build:T1"]
    assert node.status == "ESCALATED" and "G-E5" in node.escalation.reason
    assert not f.runs("build:T1")


@scenario(**ENV)
async def test_stale_environment_approval_blocks_the_verifier(f: Flow):
    """G-E5 for verifier sandboxes: verification escalates instead of using a stale environment."""
    await f.to_build()
    f.ce.write_record("plan/environment.json", json.dumps(spec(summary="edited after dispatch")))
    await f.implement()
    node = f.ce.nodes["build:T1"]
    assert node.status == "ESCALATED" and node.escalation.kind == "verify_infra"
    assert "G-E5" in node.escalation.reason and not sandboxes(f, "verifier")


@scenario(**ENV)
async def test_environment_survives_a_restart_and_provisioning_resumes(f: Flow):
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    await f.ce.human_action("H-E1", "approve", "miles", {})
    async with f.ctl.svc.lock:                          # as if the controller died mid-build
        f.ce.nodes["provision"].status = RUNNING
        f.ce.save()
    ctl2 = Controller(f.cfg, runner=f.runner, builder=f.builder)
    ctl2.load()
    ce2 = ctl2.changes[f.ce.cid]
    async with ctl2.svc.lock:
        ce2.recover()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if ce2.nodes["provision"].status == "ACCEPTED":
            break
    assert ce2.nodes["provision"].status == "ACCEPTED"
    ctl3 = Controller(f.cfg, runner=f.runner, builder=f.builder)      # and the build persists
    ctl3.load()
    assert ctl3.changes[f.ce.cid].change.environment == ce2.change.environment


# ====================================================================== sandbox create commands
def test_openshell_create_attaches_the_environment(tmp_path):
    """What OpenShell is actually asked for: the profile's image, the controller's providers
    (exactly one git profile) and the configured ones – network policies are in the policy file –,
    CPU and memory as --cpu / --memory and disk as --driver-config-json – for agents and
    verifiers."""
    from legion_orch.sandbox.runner import OpenShellRunner
    cfg = make_config(tmp_path, environment={})
    cfg.sandbox.image, cfg.sandbox.providers = "reg/agent:1", ["claude-code"]
    calls = []

    async def fake_os(*args, timeout=600):
        calls.append(list(args))
        return '{"id": "sb-1"}' if args[:2] == ("sandbox", "get") else ""
    r = OpenShellRunner(cfg)
    r._os = fake_os
    env = SandboxEnv(image="localhost/legion-env:abc", network={"env_pypi": {"name": "env-pypi"}},
                     cpu="2", memory="4096Mi", driver_config='{"firecracker": {"disk_mib": 2048}}')

    def create(agent: bool, env: SandboxEnv | None, push: bool = False) -> list[str]:
        calls.clear()
        asyncio.run(r.create(SandboxSpec(name="lg-ada", payload_dir=tmp_path, policy_file=tmp_path / "p",
                                         agent=agent, role="implementer" if agent else "verifier",
                                         env=env, push=push)))
        return calls[0]

    def flag(args, name):
        return [args[i + 1] for i, a in enumerate(args) if a == name]
    agent = create(True, env)
    assert flag(agent, "--from") == ["localhost/legion-env:abc"]
    assert flag(agent, "--provider") == ["legion-mcp", "legion-git", "claude-code"]
    assert flag(create(True, env, push=True), "--provider") == ["legion-mcp", "legion-git-push",
                                                                "claude-code"]
    assert flag(agent, "--cpu") == ["2"] and flag(agent, "--memory") == ["4096Mi"]
    assert flag(agent, "--driver-config-json") == ['{"firecracker": {"disk_mib": 2048}}']
    verifier = create(False, env)
    assert flag(verifier, "--from") == ["localhost/legion-env:abc"]
    assert flag(verifier, "--cpu") == ["2"] and flag(verifier, "--memory") == ["4096Mi"]
    assert flag(verifier, "--provider") == ["legion-git"]                   # no MCP, no model
    plain = create(True, None)
    assert flag(plain, "--from") == ["reg/agent:1"]
    assert not {"--cpu", "--memory", "--driver-config-json"} & set(plain)
    assert flag(create(True, SandboxEnv()), "--from") == ["reg/agent:1"]  # no image: default


# ====================================================================== library and rendering
def test_library_loads_and_rejects_invalid_entries(tmp_path):
    lib = library()
    assert {"python", "uv", "node", "ruff"} <= set(lib.tools) and "pypi" in lib.policies
    assert lib.problems() == []
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "bad.yaml").write_text(yaml.safe_dump({"description": "x"}))
    with pytest.raises(LibraryError, match="bad.yaml"):
        Library.load(tmp_path, SCHEMA)


def test_library_cross_checks(tmp_path):
    shutil.copytree(ROOT / "environments", tmp_path / "lib")
    (tmp_path / "lib/tools/x.yaml").write_text(yaml.safe_dump(
        {"description": "x", "apt": ["x"], "network": ["nope"]}))
    with pytest.raises(LibraryError, match=r"tool x needs unknown network policies \['nope'\]"):
        Library.load(tmp_path / "lib", SCHEMA)
    (tmp_path / "lib/tools/x.yaml").unlink()
    (tmp_path / "lib/tools/two.yaml").write_text(yaml.safe_dump(
        {"description": "x", "mise": {"a": "1", "b": "2"}, "binaries": ["bin/a"]}))
    with pytest.raises(LibraryError, match="exactly one mise tool"):
        Library.load(tmp_path / "lib", SCHEMA)


def test_every_seeded_tool_resolves_to_absolute_binaries():
    lib = library()
    for name, entry in lib.tools.items():
        tool, err = resolve_tool(name, entry, None)
        assert err is None and all(b.startswith("/") for b in tool.binaries), name
        assert tool.binaries, f"{name} declares no binaries"
    node, _ = resolve_tool("node", lib.tools["node"], None)       # node, npm and npx
    assert node.binaries == ("/usr/local/share/mise/installs/node/*/bin/*",)
    bun, _ = resolve_tool("bun", lib.tools["bun"], None)          # bun and bunx (a link to it)
    assert bun.binaries == ("/usr/local/share/mise/installs/bun/*/bin/*",)
    assert lib.tools["bun"].network == ["npm-registry"]            # scoped packages: encoded slash
    assert "shadcn-registry" in lib.policies                       # opt-in, per profile


def test_selected_tools_bring_their_network_for_their_own_binaries():
    s = EnvironmentSpec.model_validate(spec(
        profiles={"uv": {"tools": ["python", "uv", "ruff"]},
                  "lint": {"tools": ["ruff", "python"]},
                  "quiet": {"tools": ["uv"], "offline": True},
                  "clone": {"tools": ["uv", "git"], "network": {"git": ["github-read"]}}},
        roles={"implementer": "uv", "verifier": "lint", "reviewer": "quiet", "oracle": "clone"}))
    lib = library()
    profiles, problems = resolve(s, lib)
    assert not problems, problems
    uv_bins = ("/usr/local/share/mise/installs/uv/*/uv", "/usr/local/share/mise/installs/uv/*/*/uv")
    assert profiles["uv"].tools[1].binaries == uv_bins
    [pypi] = profiles["uv"].policies                 # brought by uv; python and ruff bring none
    assert pypi.name == "pypi" and pypi.tools == ("uv",) and pypi.binaries == uv_bins
    assert profiles["lint"].policies == [] and profiles["quiet"].policies == []
    by_name = {p.name: p for p in profiles["clone"].policies}
    assert set(by_name) == {"pypi", "github-read"}
    assert by_name["github-read"].tools == ("git",)
    assert all("git-core" in b for b in by_name["github-read"].binaries)
    assert lib.tools["uv"].network == ["pypi"] and lib.tools["python"].network == []


def test_used_proposals_follow_profiles_and_proposed_tools():
    s = EnvironmentSpec.model_validate(spec(
        proposed={"tools": {"t1": {"description": "x", "apt": ["a"], "network": ["p1"]},
                            "t2": {"description": "unused", "apt": ["b"]}},
                  "policies": {"p1": {"description": "x", "endpoints": [{"host": "a.b", "port": 1}]},
                               "p2": {"description": "x", "endpoints": [{"host": "c.d", "port": 1}]},
                               "p3": {"description": "x", "endpoints": [{"host": "e.f", "port": 1}]}}},
        profiles={"dev": {"tools": ["t1", "uv"], "network": {"uv": ["p2"]}}}))
    assert s.used_proposals() == {Kind.TOOL: ["t1"], Kind.POLICY: ["p1", "p2"]}


def test_binary_paths_for_backends_bundles_and_escapes():
    assert binary_paths({"npm:prettier": "3"}, ["bin/prettier"]) == (
        ("/usr/local/share/mise/installs/npm-prettier/*/bin/prettier",), None)
    assert binary_paths({"a": "1", "b": "2"}, ["/usr/bin/a"]) == (("/usr/bin/a",), None)
    assert "exactly one mise tool" in binary_paths({"a": "1", "b": "2"}, ["bin/a"])[1]
    assert "'..'" in binary_paths({"a": "1"}, ["../../etc/x"])[1]


def test_image_recipe_rendering_is_content_addressed():
    py = Tool(name="python", description="py", mise={"python": "3.12"}, apt=("libpq-dev",))
    uv = Tool(name="uv", description="uv", mise={"uv": "0.9"},
              env=(("UV_PYTHON_DOWNLOADS", "never"), ("UV_PYTHON_PREFERENCE", "only-system")))
    df = dockerfile("registry/agent:1", [py, uv], "v2026.11.0", "sandbox")
    lines = df.splitlines()
    assert lines[0].startswith("# Generated") and "FROM registry/agent:1" in lines
    assert "ca-certificates curl libpq-dev" in df and "MISE_VERSION=v2026.11.0" in df
    install = next(i for i, x in enumerate(lines) if x.startswith("RUN mise install"))
    env = [i for i, x in enumerate(lines) if x.startswith("ENV UV_")]
    assert [lines[i] for i in env] == ['ENV UV_PYTHON_DOWNLOADS="never"',
                                       'ENV UV_PYTHON_PREFERENCE="only-system"']
    assert all(i > install for i in env) and lines[-1] == "USER sandbox"
    toml = mise_toml([uv, py])
    assert toml.splitlines()[-2:] == ['"python" = "3.12"', '"uv" = "0.9"']
    files = {"Dockerfile": df, "mise.toml": toml}
    assert image_tag(files, None) == image_tag(dict(files), None)
    assert image_tag(files, None) != image_tag(files | {"mise.toml": toml + "#"}, None)
    r = ImageRecipe.make("b", [py], mise_version="v1", user="u", registry="reg.example.com/legion/")
    assert r.tag.startswith("reg.example.com/legion/legion-env:")
    assert set(r.files) == {"Dockerfile", "mise.toml"}


def test_network_policy_becomes_a_sandbox_policy_rule(tmp_path):
    """No credential, no provider: the policy is a network_policies entry of the sandbox policy,
    open to the profile's binaries, merged after any rules of the template's own."""
    pol = Policy(name="npm-registry", description="npm", binaries=("/opt/node/bin/npm",),
                 endpoints=(Endpoint(host="registry.npmjs.org", port=443),))
    key, entry = policy_entry(pol)
    assert key == "env_npm_registry"
    assert entry == {"name": "env-npm-registry", "binaries": [{"path": "/opt/node/bin/npm"}],
                     "endpoints": [{"host": "registry.npmjs.org", "port": 443, "protocol": "rest",
                                    "enforcement": "enforce", "access": "read-only"}]}
    cfg = make_config(tmp_path, environment={})
    for agent in (True, False):
        doc = yaml.safe_load(render_policy(cfg, agent=agent, network={key: entry}))
        assert doc["network_policies"] == {key: entry}
    own = {"network_policies": {"site_proxy": {"name": "site-proxy"}}}
    assert list(merge_network(own, {key: entry})["network_policies"]) == ["site_proxy", key]
    with pytest.raises(ValueError, match="site_proxy"):
        merge_network(own, {"site_proxy": entry})


def test_resources_caps_and_driver_config(tmp_path):
    cfg = make_config(tmp_path, environment={})
    assert driver_config(cfg, Resources()) is None
    assert driver_config(cfg, Resources(memory_mib=4096, vcpus=2)) is None      # flags, not driver
    assert json.loads(driver_config(cfg, Resources(disk_mib=2048))) == {"firecracker": {"disk_mib": 2048}}
    assert (Resources(memory_mib=4096, vcpus=2).cpu, Resources(memory_mib=4096).memory) == ("2", "4096Mi")
    assert (Resources().cpu, Resources().memory) == (None, None)
    caps = Resources(memory_mib=1024, vcpus=4)
    assert Resources(memory_mib=2048, vcpus=4, disk_mib=9).over(caps) == [("memory_mib", 2048, 1024)]


# ====================================================================== builders
def test_podman_builder_commands(tmp_path, monkeypatch):
    """Build when the image is missing (and push with a registry); skip it when it exists;
    fail with the log."""
    from legion_orch.sandbox import builders
    cfg = make_config(tmp_path, environment={"registry": "reg.example.com/legion"})
    calls: list[list[str]] = []
    exists: set[str] = set()
    fail: set[str] = set()

    async def fake_run(argv, timeout):
        calls.append(argv)
        if any(v in argv for v in fail):
            return 1, "denied: push to reg.example.com"
        probe = argv[1:3] == ["image", "exists"] or "export" in argv or argv[-2:-1] == ["get"]
        if probe:
            return (0 if argv[-1] in exists or argv[-3] in exists else 1), ""
        return 0, "ok"
    monkeypatch.setattr(builders, "_run", fake_run)
    b = builders.PodmanBuilder(cfg)
    recipe = ImageRecipe.make("registry/agent:1", [Tool("uv", "uv", {"uv": "0.9"})],
                              mise_version="v1", user="sandbox", registry=cfg.environment.registry)
    ref, _ = asyncio.run(b.ensure_image(recipe))
    assert ref == recipe.tag and [c[1] for c in calls] == ["image", "build", "push"]
    exists.add(recipe.tag)
    calls.clear()
    asyncio.run(b.ensure_image(recipe))
    assert calls[0][1] == "image" and not any("create" in c or "build" in c for c in calls)
    exists.clear()
    fail.add("push")
    with pytest.raises(builders.BuildError, match="denied: push") as e:
        asyncio.run(b.ensure_image(recipe))
    assert "$ podman build" in str(e.value)                  # the log so far is in the error
    recipe.base = ""
    with pytest.raises(builders.BuildError, match="sandbox.image"):
        asyncio.run(b.ensure_image(recipe))


def test_builder_runs_time_out():
    from legion_orch.sandbox.builders import _run
    rc, out = asyncio.run(_run(["sleep", "5"], 0.1))
    assert rc == 124 and "timeout" in out


# ====================================================================== operators
@scenario(**ENV)
async def test_promote_copies_an_approved_proposal_into_the_library(f: Flow):
    from legion_orch.workflow.engine import WorkflowError
    own_library(f)
    await to_plan(f)
    s = spec(proposed={"tools": {"taplo": {"description": "taplo", "mise": {"taplo": "0.9"}}}},
             profiles={"dev": {"tools": ["python", "taplo"]}}, roles={"implementer": "dev"})
    await f.plan(environment=s)
    await f.critique("PASS")
    promote = lambda kind, name: f.change_action("promote_proposal", kind=kind, name=name)
    with pytest.raises(WorkflowError, match="not approved"):
        await promote("tool", "taplo")
    await f.human("H-E1", "approve")
    res = await promote("tool", "taplo")
    written = f.tmp / "lib/tools/taplo.yaml"
    assert str(written) in res.note and "taplo" in Library.load(f.tmp / "lib", SCHEMA).tools
    assert written.read_text().startswith("# Promoted from file:changes/")
    for kind, name, msg in (("tool", "taplo", "not overwritten"), ("policy", "nope", "proposes no"),
                            ("plugin", "x", "kind must be")):
        with pytest.raises(WorkflowError, match=msg):
            await promote(kind, name)


@scenario(**ENV)
async def test_ui_view_shows_effective_network_and_problems(f: Flow):
    from legion_orch.server.views import change_state
    await to_plan(f)
    assert change_state(f.ctl, f.ce)["environment"] is None
    await f.plan()
    env = change_state(f.ctl, f.ce)["environment"]
    assert env["problems"] == [] and env["proposals"] == {"tools": [], "policies": []}
    assert env["network"]["dev"] == [{"policy": "pypi", "tools": ["uv"],
                                      "hosts": ["pypi.org:443", "files.pythonhosted.org:443"]}]
    assert env["network"]["check"] == []                 # offline


def test_cli_library_list_and_check(tmp_path, capsys):
    from legion_orch.cli import cmd_check, main
    cfg = make_config(tmp_path, environment={})
    assert main(["-c", str(cfg.base_dir / "orchestrator.yaml"), "library", "list"]) == 0
    out = capsys.readouterr().out
    assert "`uv` – " in out and "brings network: pypi" in out
    cmd_check(cfg, None)
    assert "environment library" in capsys.readouterr().out
    (tmp_path / "plain").mkdir()
    plain = make_config(tmp_path / "plain")
    assert main(["-c", str(plain.base_dir / "orchestrator.yaml"), "library", "list"]) == 1


def test_config_without_environment_section(tmp_path):
    assert make_config(tmp_path).environment is None
    cfg = load_config(make_config(tmp_path, environment={"registry": "r.example"}).base_dir
                      / "orchestrator.yaml")
    assert cfg.environment.registry == "r.example" and cfg.environment.max_vcpus == 16


def test_language_preferences_reach_the_roles_that_plan_build_and_judge_code():
    a = Assets(ROOT / "assets")
    have = {r for r, role in a.roles.items() if "language-preferences" in role.skills}
    assert have == {"planner", "critic", "oracle", "implementer", "reviewer"}
    assert "uv" in a.skill("language-preferences") and "ruff" in a.skill("language-preferences")


def test_the_web_frontend_conventions_match_the_projects_own_frontend():
    """The commands the skill tells agents to verify with are scripts of this repository's own
    frontend (the reference for the conventions), its CLIs run on bun, and the library entries
    the skill names exist."""
    import json
    import re
    skill = Assets(ROOT / "assets").skill("stack-bun-typescript")
    scripts = json.loads((ROOT / "frontend/package.json").read_text())["scripts"]
    for name in re.findall(r"`bun run ([a-z0-9:_-]+)`", skill):
        assert name in scripts, f"the skill names `bun run {name}`, which frontend/ lacks"
    assert scripts["test"] == "bun test" and "`bun test" in skill
    for cmd in scripts.values():                      # no Node: package CLIs run on bun
        for cli in ("vite", "prettier"):
            assert not re.search(rf"(?<!--bun )\b{cli}\b", cmd), cmd
    assert (ROOT / "frontend/bun.lock").is_file() and "bun install --frozen-lockfile" in skill
    lib = library()
    assert {"bun"} <= set(lib.tools) and {"npm-registry", "shadcn-registry"} <= set(lib.policies)
    assert "`bun`" in skill and "`shadcn-registry`" in skill


def test_npm_registry_keeps_encoded_slashes_for_scoped_packages():
    """npm fetches scoped packages as /@scope%2fname; the gateway rejects %2F unless allowed."""
    lib = library()
    entry = lambda name: policy_entry(Policy(name=name, description="", binaries=("/x",),
                                             endpoints=tuple(lib.policies[name].endpoints)))[1]
    assert [e.get("allow_encoded_slash") for e in entry("npm-registry")["endpoints"]] == [True]
    assert all("allow_encoded_slash" not in e for e in entry("pypi")["endpoints"])   # unset: omitted


def test_npm_registry_is_read_only_and_keeps_encoded_slashes():
    """npm fetches scoped packages as /@scope%2fname; the gateway rejects %2F unless the endpoint
    allows it."""
    lib = library()
    pol = Policy(name="npm-registry", description="", binaries=("/x",),
                 endpoints=tuple(lib.policies["npm-registry"].endpoints))
    assert policy_entry(pol)[1]["endpoints"] == [{
        "host": "registry.npmjs.org", "port": 443, "protocol": "rest", "enforcement": "enforce",
        "access": "read-only", "allow_encoded_slash": True}]
    # read-only denies npm's audit POST, so node turns the audit off in the image
    node, err = resolve_tool("node", lib.tools["node"], None)
    assert err is None and ("NPM_CONFIG_AUDIT", "false") in node.env
    assert 'ENV NPM_CONFIG_AUDIT="false"' in dockerfile("base", [node], "v1", "sandbox")


# ====================================================================== reprovision
@scenario(**ENV)
async def test_reprovision_rebuilds_against_the_current_library(f: Flow):
    """A fixed library entry reaches a provisioned change: running agents of mapped roles finish
    on the old build, their next runs wait, then provisioning builds again and released
    sandboxes start fresh in the new build. The spec and its H-E1 approval stay."""
    lib_dir = f.tmp / "lib"
    own_library(f)
    await f.to_build()
    old = f.ce.change.environment.roles
    approval = f.ce.change.approvals["H-E1"]
    implementer = f.run("build:T1", "implementer")            # live, in its kept sandbox
    assert f.ce.nodes["build:T1"].sandbox == implementer.sandbox

    with pytest.raises(WorkflowError, match="reason"):
        await f.change_action("reprovision", reason="fix")
    uv = lib_dir / "tools/uv.yaml"
    uv.write_text(uv.read_text() + "  UV_NO_PROGRESS: \"1\"\n")
    res = await f.change_action("reprovision", reason="the uv library entry gained a setting")
    assert "once running agents of mapped roles finish" in res.note
    assert f.status("provision") == "PENDING"                 # the implementer is still live

    await f.implement()                                       # verify, then the panel waits
    assert f.status("provision") == "ACCEPTED"
    new = f.ce.change.environment.roles
    assert new["implementer"].image != old["implementer"].image
    assert new["reviewer"].image != old["reviewer"].image
    assert f.ce.change.approvals["H-E1"] == approval          # same spec, same approval
    assert f.ce.nodes["build:T1"].sandbox is None
    assert implementer.sandbox in f.runner.deleted
    recipe = f.builder.recipes[new["implementer"].image]
    assert 'ENV UV_NO_PROGRESS="1"' in recipe.files["Dockerfile"]
    reviewers = sandboxes(f, "reviewer", "build:T1")
    assert reviewers and all(r.env.image == new["reviewer"].image for r in reviewers)


@scenario(**ENV)
async def test_reprovision_needs_a_provisioned_current_environment(f: Flow):
    reason = "rebuild against the updated library"
    await to_plan(f)
    with pytest.raises(WorkflowError, match="no provisioned environment"):
        await f.change_action("reprovision", reason=reason)
    await f.plan()
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    assert f.status("provision") == "ACCEPTED"
    path = "plan/environment.json"
    f.ce.docs.write(f.ce.p(path), json.dumps(spec(summary="changed after approval")),
                    author="someone", role="planner", base_version=f.ce.latest(path))
    with pytest.raises(WorkflowError, match="G-E5"):
        await f.change_action("reprovision", reason=reason)
