"""The sandbox environment: the planner's spec, critique, H-E1, provisioning and attaching the
environment to sandboxes (WORKFLOW.md §5.3a)."""
from __future__ import annotations

import asyncio
import copy
import json
import shutil

import pytest
import yaml
from engine_harness import (
    ENVIRONMENT,
    ORACLE,
    REPO,
    ROOT,
    Flow,
    make_config,
    plan_doc,
    scenario,
    signals,
)

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
from legion_orch.workflow.engine import WorkflowError
from legion_orch.workflow.environment import (
    EnvironmentSpec,
    Kind,
    Library,
    LibraryError,
    binary_paths,
    resolve,
    resolve_tool,
)

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
    await f.signoff("H-1")


R2 = signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2)


# ====================================================================== tree and flow
@scenario()
async def test_without_environment_config_nothing_is_provisioned(f: Flow):
    await f.to_build()
    assert f.status("provision") == "passed" and f.ce.change.environment is None
    assert "default image" in f.ce.timeline()
    assert all(sp.env is None for sp in f.runner.specs.values())


@scenario(**ENV)
async def test_environment_is_specified_critiqued_approved_provisioned_and_attached(f: Flow):
    f.cfg.sandbox.image, f.cfg.sandbox.verifier_image = "reg/agent:1", "reg/verifier:1"
    await to_plan(f)
    assert "`pypi` – Python Package Index" in f.run("design").assignment["environment"]
    await f.plan()
    critic = f.run("critique", "critic")
    assert f.ticket("design").outputs["environment"] in critic.context_refs
    await f.critique("PASS")
    assert f.status("H-E1") == "working" and f.status("provision") == "pending"
    await f.signoff("H-E1")
    assert f.status("provision") == "passed"
    roles = f.ce.change.environment.roles
    assert {r: e.profile for r, e in roles.items()} == ENVIRONMENT["roles"]
    impl, ver, rev = roles["implementer"], roles["verifier"], roles["reviewer"]
    assert impl.image.startswith("localhost/legion-env:") and impl.image != ver.image
    assert rev.image != ver.image                    # same profile, agent vs verifier base image
    assert list(impl.network) == ["env_pypi"]
    assert ver.network == rev.network == {}          # check is offline
    assert (impl.cpu, impl.memory, impl.driver_config) == ("2", "4096Mi", None)
    assert (ver.cpu, ver.memory, ver.driver_config) == (None, None, None)
    assert f.ce.latest("control/environment/build.json")
    assert f.ce.latest("control/environment/provision.log")
    recipe = f.builder.recipes[impl.image]
    assert "COPY mise.toml /etc/mise/config.toml" in recipe.files["Dockerfile"]
    # no mise.toml in the repository: every tool at its latest, looked up once, in every image
    assert '"python" = "3.13.1"' in recipe.files["mise.toml"]
    assert recipe.files["mise.toml"].split("[tools]")[1] == \
        f.builder.recipes[ver.image].files["mise.toml"].split("[tools]")[1]
    assert f.builder.looked_up == [["python", "ruff", "uv"]]
    build = f.ce.change.environment
    assert {k: (v.version, v.source) for k, v in build.tools.items()} == {
        "python": ("3.13.1", "latest"), "ruff": ("0.15.0", "latest"), "uv": ("0.13.2", "latest")}
    assert '"uv" = "0.13.2"' in f.ce.doc("control/environment/mise.toml").content
    assert build.mise and build.mise.startswith("file:")
    assert {e["host"] for e in impl.network["env_pypi"]["endpoints"]} >= {"pypi.org"}

    name = f.run("T1", "implementer").sandbox
    tools = f.run("T1", "implementer").template_vars["sandbox_tools"]
    assert tools.startswith("profile `dev`: python 3.13.1, ruff 0.15.0, uv 0.13.2. Build, test")
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
    assert f.status("T1") == "passed"
    assert all(sp.env is None for sp in f.runner.specs.values()
               if sp.role in ("intake", "planner", "critic"))


@scenario(**ENV)
async def test_r2_the_oracle_runs_in_the_environment(f: Flow):
    await f.to_build(R2)
    roles = f.ce.change.environment.roles
    [oracle] = sandboxes(f, "oracle")
    assert oracle.env.image == roles["oracle"].image
    pre = sandboxes(f, "verifier", "preflight")             # its preflight, in the verifier's
    assert pre and all(v.env.image == roles["verifier"].image for v in pre)


@scenario(**ENV)
async def test_identical_environments_build_the_same_recipes(f: Flow):
    await f.to_build()
    first = list(f.builder.built)
    (f.tmp / "second").mkdir()
    g = Flow(f.tmp / "second", environment={})              # another controller, same spec
    g.builder = g.ctl.svc.builder = f.builder
    await g.git.ensure()
    g.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    g.ce = await g.ctl.start_change("again", REPO)
    await g.settle()
    try:
        await g.to_build()
    finally:
        await g.stop()
    # the same recipes: another tag of the change's own on the same layers (images::image_tag)
    assert [t.rsplit("-", 1)[1] for t in f.builder.built] == [t.rsplit("-", 1)[1] for t in first] * 2


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
    assert "the library is invalid" in f.run("design").assignment["environment"]
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


# ====================================================================== sign-off, rework, failures
@scenario(**ENV)
async def test_the_planner_must_report_the_environment_spec(f: Flow):
    await to_plan(f)
    r = f.run("design")
    spec_ = await f.write(r, "spec.md", "# Spec\n")
    orc = await f.write(r, "oracle.json", ORACLE)
    plan = await f.write(r, "tickets.json", plan_doc([{"id": "T1", "files_allowed": ["a"],
                                                        "verification": ["true"]}], ("verify",)))
    req = await f.write(r, "specs/REQ-1.md", "# REQ-1\n\n## Current behaviour\nx\n\n"
                        "## Change\ny\n\n## Acceptance criteria\n- z\n")
    out = await f.report(r, [spec_, orc, plan, req], outputs={
        "spec": spec_, "oracle": orc, "tickets": plan, "specs": [req]})
    assert not out.accepted and any("environment" in v for v in out.violations), out.violations


@scenario()
async def test_without_the_stage_the_planner_is_told_not_to_write_a_spec(f: Flow):
    await to_plan(f)
    assert "every sandbox uses the default image" in f.run("design").assignment["environment"]


@scenario(**ENV)
async def test_an_h_e1_rejection_reworks_the_plan_and_critiques_it_again(f: Flow):
    await to_plan(f)
    first = f.run("design")
    await f.plan()
    await f.critique("PASS")
    await f.signoff("H-E1", approve=False, reason="the verifier needs node for the lint step")
    design = f.ticket("design")
    assert design.attempt == 2 and design.status == "working"
    assert any("node for the lint" in x for x in f.run("design").assignment["findings"])
    assert f.run("design").sandbox == first.sandbox            # its own sandbox, kept
    await f.plan(environment=spec(profiles={**ENVIRONMENT["profiles"],
                                            "check": {"tools": ["python", "uv", "node"]}}))
    critic = f.run("critique", "critic")                       # a fresh judge, a new round
    assert len(f.runs("critique")) == 2 and critic.sandbox != f.runs("critique")[0].sandbox
    await f.critique("PASS")
    await f.signoff("H-E1")
    assert f.status("provision") == "passed"


@scenario(**ENV)
async def test_a_provision_failure_waits_and_a_retry_provisions_again(f: Flow):
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    f.builder.fail = "podman build failed (1): mise: python 3.12 not found"
    await f.signoff("H-E1")
    t = f.ticket("provision")
    assert (t.status, t.waiting) == ("waiting", "system")
    why = " ".join(t.findings)
    assert "G-PV1" in why and "mise: python" in why
    assert f.ce.change.environment is None
    f.builder.fail = None
    await f.human("provision", "retry")
    assert f.status("provision") == "passed" and f.ce.change.environment


@scenario(**ENV)
async def test_library_changed_after_approval_stops_provisioning(f: Flow):
    """The approved spec is resolved again at provisioning: a library entry removed (or a
    library broken) since then stops the build instead of building something else."""
    own_library(f)
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    (f.tmp / "lib/tools/uv.yaml").unlink()
    await f.signoff("H-E1")
    t = f.ticket("provision")
    assert t.status == "waiting"
    why = " ".join(t.findings)
    assert "no longer resolves against the library" in why
    assert "unknown tool 'uv'" in why and not f.builder.built
    (f.tmp / "lib/tools/uv.yaml").write_text("mise: {uv: '0.9'}\n")           # invalid now
    await f.human("provision", "retry")
    assert "the environment library is invalid" in " ".join(f.ticket("provision").findings)
    shutil.copy(ROOT / "environments/tools/uv.yaml", f.tmp / "lib/tools/uv.yaml")
    await f.human("provision", "retry")
    assert f.status("provision") == "passed"


@scenario(**ENV)
async def test_a_stale_environment_stops_the_verifier(f: Flow):
    """G-E5 for verifier sandboxes: the build's verification does not use an environment built
    from a spec the change no longer has approved."""
    await f.to_build()
    root = f.ce.tickets.root
    root.pointers["environment"] = f.ce.write_record(
        "tickets/design/environment.json", json.dumps(spec(summary="replanned"))).attestation
    await f.implement()
    t = f.ticket("verify:T1")
    assert t.status == "waiting" and "G-E5" in " ".join(t.findings)
    assert not sandboxes(f, "verifier")


@scenario(**ENV)
async def test_environment_survives_a_restart_and_provisioning_resumes(f: Flow):
    await to_plan(f)
    await f.plan()
    await f.critique("PASS")
    f.builder.hold = asyncio.Event()
    await f.ce.human_action("H-E1", "approve", "miles", {})
    for _ in range(100):                                # provisioning starts, then its build waits
        await asyncio.sleep(0.02)
        if f.status("provision") == "working":
            break
    assert f.status("provision") == "working"
    await f.ctl.stop()                                  # the controller dies mid-build
    f.builder.hold.set()
    ctl2, ce2 = await f.restart(recover=True)
    f.ctl, f.ce = ctl2, ce2
    for _ in range(200):
        await asyncio.sleep(0.02)
        if f.status("provision") == "passed":
            break
    assert f.status("provision") == "passed"
    ctl3, _ = await f.restart()      # and the build persists
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
    assert flag(agent, "--provider") == ["legion-mcp", "legion-a2a", "legion-git", "claude-code"]
    assert flag(create(True, env, push=True), "--provider") == ["legion-mcp", "legion-a2a",
                                                                "legion-git-push", "claude-code"]
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
    assert "--no-install-recommends ca-certificates curl " in df and "MISE_VERSION=v2026.11.0" in df
    assert df.index("MISE_VERSION=") < df.index("install -y --no-install-recommends libpq-dev")
    from legion_orch.sandbox.images import lookup_recipe
    lookup = lookup_recipe("registry/agent:1", "v2026.11.0", "c1").files["Dockerfile"]
    assert lookup.split("FROM", 1)[1] in df.split("FROM", 1)[1]      # the images' first layers
    install = next(i for i, x in enumerate(lines) if x.startswith("RUN mise install"))
    env = [i for i, x in enumerate(lines) if x.startswith("ENV UV_")]
    assert [lines[i] for i in env] == ['ENV UV_PYTHON_DOWNLOADS="never"',
                                       'ENV UV_PYTHON_PREFERENCE="only-system"']
    assert all(i > install for i in env) and lines[-1] == "USER sandbox"
    toml = mise_toml([uv, py])
    assert toml.splitlines()[-2:] == ['"python" = "3.12"', '"uv" = "0.9"']
    files = {"Dockerfile": df, "mise.toml": toml}
    assert image_tag(files, None, "c1") == image_tag(dict(files), None, "c1")
    assert image_tag(files, None, "c1") != image_tag(files | {"mise.toml": toml + "#"}, None, "c1")
    # one tag per change: the same recipe in another change is its own (on the same layers)
    assert image_tag(files, None, "c1") != image_tag(files, None, "c2")
    assert image_tag(files, None, "c1").startswith("localhost/legion-env:c1-")
    r = ImageRecipe.make("b", [py], mise_version="v1", user="u", registry="reg.example.com/legion/",
                         change="c1")
    assert r.tag.startswith("reg.example.com/legion/legion-env:c1-")
    assert set(r.files) == {"Dockerfile", "mise.toml"}
    assert r.labels == {"org.legion.change": "c1", "org.legion.recipe": r.tag.rsplit("-", 1)[1]}


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
                              mise_version="v1", user="sandbox", registry=cfg.environment.registry,
                              change="c1")
    ref, _ = asyncio.run(b.ensure_image(recipe))
    assert ref == recipe.tag and [c[1] for c in calls] == ["image", "build", "push"]
    build = calls[1]
    assert build[build.index("--label") + 1] == "org.legion.change=c1"
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


def test_podman_removes_a_changes_images_by_label(tmp_path, monkeypatch):
    """Local images labelled for the change are removed; one still in use is kept and logged."""
    from legion_orch.sandbox import builders
    cfg = make_config(tmp_path, environment={})
    calls: list[list[str]] = []

    async def fake_run(argv, timeout):
        calls.append(argv)
        if argv[1] == "images":
            return 0, "sha256:aaa\nsha256:bbb\nsha256:aaa\n"
        return (0, "") if argv[-1] == "sha256:aaa" else (2, "image is in use by a container")
    monkeypatch.setattr(builders, "_run", fake_run)
    log = asyncio.run(builders.PodmanBuilder(cfg).remove_change_images("c1"))
    assert calls[0][-2:] == ["--filter", "label=org.legion.change=c1"]
    assert [c[-1] for c in calls[1:]] == ["sha256:aaa", "sha256:bbb"]
    assert "sha256:aaa: removed" in log and "kept (2): image is in use" in log
    assert asyncio.run(builders.NoBuilder().remove_change_images("c1")) == ""


def test_builder_runs_time_out():
    from legion_orch.sandbox.builders import _run
    rc, out = asyncio.run(_run(["sleep", "5"], 0.1))
    assert rc == 124 and "timeout" in out


# ====================================================================== operators
@scenario(**ENV)
async def test_promote_copies_an_approved_proposal_into_the_library(f: Flow):
    own_library(f)
    await to_plan(f)
    s = spec(proposed={"tools": {"taplo": {"description": "taplo", "mise": {"taplo": "0.9"}}}},
             profiles={"dev": {"tools": ["python", "taplo"]}}, roles={"implementer": "dev"})
    await f.plan(environment=s)
    await f.critique("PASS")
    promote = lambda kind, name: f.change_action("promote_proposal", kind=kind, name=name)
    with pytest.raises(WorkflowError, match="approved"):
        await promote("tool", "taplo")
    await f.signoff("H-E1")
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
    assert have == {"planner", "critic", "oracle", "implementer", "scaffolder", "reviewer"}
    assert "uv" in a.skill("language-preferences") and "ruff" in a.skill("language-preferences")


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
