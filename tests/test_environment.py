"""The sandbox-environment stage: spec, critique, H-E1, provisioning and attaching the
environment to sandboxes (WORKFLOW.md §5.3a)."""
from __future__ import annotations

import copy
import json

import pytest
import yaml
from engine_harness import ENVIRONMENT, ROOT, Flow, scenario

from legion_orch.config import load_config
from legion_orch.sandbox.environment import (
    Policy,
    Tool,
    dockerfile,
    driver_config,
    image_tag,
    mise_toml,
    network_profile,
)
from legion_orch.workflow.assets import Assets
from legion_orch.workflow.environment import LibraryError, load_library

ENV = {"environment": {}}
SCHEMA = json.loads((ROOT / "assets/schemas/environment.json").read_text())


def spec(**changes) -> dict:
    s = copy.deepcopy(ENVIRONMENT)
    s.update(changes)
    return s


def profile_sandboxes(f: Flow, role: str) -> list:
    return [sp for sp in f.runner.specs.values() if sp.role == role]


# ====================================================================== graph and flow
@scenario()
async def test_without_environment_config_the_graph_is_unchanged(f: Flow):
    await f.to_build()
    assert not {"environment", "H-E1", "provision"} & set(f.ce.nodes)
    assert all(sp.env is None for sp in f.runner.specs.values())


@scenario(**ENV)
async def test_environment_is_specified_critiqued_approved_provisioned_and_attached(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    assert f.ce.nodes["critique"].depends_on == ["environment"]
    await f.plan()
    assert f.status("environment") == "RUNNING" and f.status("critique") == "PENDING"
    env_run = f.run("environment")
    assert "`pypi` – Python Package Index" in env_run.assignment["environment_library"]
    await f.environment()
    await f.critique("PASS")
    assert f.status("H-E1") == "AWAITING_HUMAN" and f.status("provision") == "PENDING"
    await f.human("H-E1", "approve")
    assert f.status("provision") == "ACCEPTED"
    build = f.ce.change.environment
    assert build.roles == ENVIRONMENT["roles"]
    dev, check = build.profiles["dev"], build.profiles["check"]
    assert dev.image and dev.image.startswith("localhost/legion-env:") and dev.verifier_image is None
    assert check.verifier_image and check.image               # verifier and reviewer use check
    assert dev.providers and dev.providers[0].startswith("legion-net-pypi-") and check.providers == []
    assert json.loads(dev.driver_config) == {"firecracker": {"mem_mib": 4096, "vcpus": 2}}
    assert check.driver_config is None
    assert f.ce.latest("control/environment/build.json") and f.ce.latest("control/logs/provision.log")
    # the image recipe: tools by mise, the agent base for dev, the verifier base for check
    dockerfile_dev = f.builder.recipes[dev.image].files["Dockerfile"]
    assert "COPY mise.toml /etc/mise/config.toml" in dockerfile_dev
    assert '"python" = "3.12"' in f.builder.recipes[dev.image].files["mise.toml"]
    prof = f.builder.providers[dev.providers[0]]
    assert prof["credentials"] == [] and {e["host"] for e in prof["endpoints"]} >= {"pypi.org"}

    await f.human("H-2", "approve")
    impl = f.run("build:T1", "implementer")
    sp = f.runner.specs[impl.sandbox]
    assert sp.env.image == dev.image and sp.env.providers == dev.providers
    assert sp.env.driver_config == dev.driver_config
    await f.implement()
    verifiers = profile_sandboxes(f, "verifier")
    assert verifiers and all(v.env.image == check.verifier_image and v.env.providers == []
                             for v in verifiers)
    await f.review()
    reviewers = profile_sandboxes(f, "reviewer")
    assert reviewers and all(r.env.image == check.image for r in reviewers)
    assert f.status("build:T1") == "ACCEPTED"
    # roles before provisioning never get an environment
    assert all(sp.env is None for sp in f.runner.specs.values()
               if sp.role in ("intake", "planner", "environment", "critic"))


@scenario(**ENV)
async def test_identical_environments_reuse_images_and_providers(f: Flow):
    await f.to_build()
    first = list(f.builder.built)
    (f.tmp / "second").mkdir()
    g = await Flow.create(f.tmp / "second", goal="again")     # another controller, same spec
    g.builder = f.builder
    g.ctl.svc.builder = f.builder
    await g.to_build()
    assert f.builder.built == first      # nothing new built: the same recipes were found


# ====================================================================== guards
@scenario(**ENV)
async def test_unknown_tool_and_policy_are_rejected(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    bad = spec(profiles={"dev": {"tools": ["python", "cobol"], "network": {"python": ["everything"]}}},
               roles={"implementer": "dev", "reviewer": "nope"})
    out = await f.environment(bad)
    assert not out.accepted
    text = " | ".join(out.violations)
    assert "unknown tool 'cobol'" in text
    assert "unknown network policy 'everything' for tool python" in text
    assert "mapped to unknown profile 'nope'" in text


@scenario(**ENV)
async def test_proposals_are_usable_but_cannot_target_the_controller_or_shadow(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    bad = spec(proposed={
        "tools": {"python": {"description": "shadow", "mise": {"python": "3.9"}}},
        "policies": {"home": {"description": "controller",
                              "endpoints": [{"host": "host.openshell.internal", "port": 8766}]}}},
        profiles={"dev": {"tools": ["python", "uv"], "network": {"uv": ["home"]}}},
        roles={"implementer": "dev"})
    out = await f.environment(bad)
    text = " | ".join(out.violations)
    assert "G-E1: proposed tool 'python' shadows" in text
    assert "G-E2: network policy home: host.openshell.internal:8766" in text
    good = spec(proposed={"tools": {"taplo": {"description": "taplo", "mise": {"taplo": "0.9"}}}},
                profiles={"dev": {"tools": ["python", "taplo"]}},
                roles={"implementer": "dev"})
    run = f.run("environment")
    att = await f.write(run, "plan/environment.json", good)
    assert (await f.report(run, [att])).accepted


@scenario(environment={"max_memory_mib": 1024})
async def test_resources_over_the_cap_and_early_roles_are_rejected(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    bad = spec(roles={"implementer": "dev", "planner": "dev"})
    out = await f.environment(bad)
    text = " | ".join(out.violations)
    assert "G-E3: profile dev: memory_mib 4096 exceeds environment.max_memory_mib (1024)" in text
    assert "G-E4: role 'planner' cannot be given an environment" in text


def test_schema_has_no_place_for_credentials():
    from jsonschema import Draft202012Validator
    s = spec(proposed={"policies": {"gh": {
        "description": "x", "credentials": [{"name": "token"}],
        "endpoints": [{"host": "api.github.com", "port": 443}]}}})
    assert not Draft202012Validator(SCHEMA).is_valid(s)


# ====================================================================== gate and failures
@scenario(**ENV)
async def test_h_e1_revise_reworks_the_environment_and_recritiques(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.environment()
    await f.critique("PASS")
    await f.human("H-E1", "revise", feedback="the verifier needs node for the lint step")
    env = f.ce.nodes["environment"]
    assert env.cycle == 2 and env.status == "RUNNING"
    assert "node for the lint" in f.run("environment").assignment["feedback_items"][-1]["text"]
    await f.environment(spec(profiles={**ENVIRONMENT["profiles"],
                                       "check": {"tools": ["python", "uv", "node"]}}))
    assert f.status("critique") == "RUNNING"         # the revised environment is critiqued again
    await f.critique("PASS")
    await f.human("H-E1", "approve")
    assert f.status("provision") == "ACCEPTED"


@scenario(**ENV)
async def test_critique_rework_reruns_plan_and_environment(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.environment()
    await f.critique("REWORK")
    assert f.status("plan") == "RUNNING" and f.status("environment") == "PENDING"
    await f.plan()
    assert f.status("environment") == "RUNNING" and f.ce.nodes["environment"].cycle == 2
    await f.environment()
    await f.critique("PASS")
    assert f.status("H-E1") == "AWAITING_HUMAN"


@scenario(**ENV)
async def test_provision_failure_escalates_and_retry_provisions_again(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    await f.environment()
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
async def test_h2_revise_resets_environment_approval_and_build(f: Flow):
    await f.to_h2()
    assert f.ce.change.environment
    await f.human("H-2", "revise", feedback="split T1 in two")
    assert f.ce.change.environment is None and "H-E1" not in f.ce.change.approvals
    assert f.status("environment") == "PENDING" and f.status("provision") == "PENDING"
    assert f.ce.nodes["environment"].feedback[-1].text == "split T1 in two"


@scenario(**ENV)
async def test_stale_environment_approval_blocks_dispatch(f: Flow):
    """G-E5: a spec changed after approval is never used to create a sandbox."""
    await f.to_h2()
    await f.ce.human_action("H-2", "approve", "miles", {})
    f.ce.write_record("plan/environment.json", json.dumps(spec(summary="edited after approval")))
    await f.settle()
    node = f.ce.nodes["build:T1"]
    assert node.status == "ESCALATED" and "G-E5" in node.escalation.reason
    assert not f.runs("build:T1")


# ====================================================================== library and rendering
def test_library_loads_and_rejects_invalid_entries(tmp_path):
    lib = load_library(ROOT / "environments", SCHEMA)
    assert {"python", "uv", "node"} <= set(lib.tools) and "pypi" in lib.policies
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "bad.yaml").write_text(yaml.safe_dump({"description": "x"}))
    with pytest.raises(LibraryError, match="bad.yaml"):
        load_library(tmp_path, SCHEMA)


def test_image_recipe_rendering_is_content_addressed():
    py = Tool(name="python", description="py", mise={"python": "3.12"}, apt=("libpq-dev",))
    uv = Tool(name="uv", description="uv", mise={"uv": "0.9"})
    df = dockerfile("registry/agent:1", [py, uv], "v2026.11.0", "sandbox")
    assert df.startswith("# Generated") and "FROM registry/agent:1" in df
    assert "ca-certificates curl libpq-dev" in df and "MISE_VERSION=v2026.11.0" in df
    assert df.rstrip().endswith("USER sandbox")
    toml = mise_toml([uv, py])
    assert toml.splitlines()[-2:] == ['"python" = "3.12"', '"uv" = "0.9"']
    files = {"Dockerfile": df, "mise.toml": toml}
    assert image_tag(files, None) == image_tag(dict(files), None)
    assert image_tag(files, None) != image_tag(files | {"mise.toml": toml + "#"}, None)
    assert image_tag(files, "reg.example.com/legion/").startswith("reg.example.com/legion/legion-env:")


def test_network_profile_is_credentialless_and_content_addressed():
    pol = Policy(name="pypi", description="pypi", binaries=("/usr/bin/python3",),
                 endpoints=({"host": "pypi.org", "port": 443},))
    prof = network_profile(pol)
    assert prof["credentials"] == [] and prof["id"].startswith("legion-net-pypi-")
    assert prof["endpoints"] == [{"protocol": "rest", "enforcement": "enforce",
                                  "access": "read-only", "host": "pypi.org", "port": 443}]
    other = Policy(name="pypi", description="pypi", binaries=("/usr/bin/python3",),
                   endpoints=({"host": "pypi.org", "port": 443, "access": "read-write"},))
    assert network_profile(other)["id"] != prof["id"]


def test_driver_config_drops_unset_resources(tmp_path):
    from engine_harness import make_config
    cfg = make_config(tmp_path, environment={})
    assert driver_config(cfg, None) is None and driver_config(cfg, {}) is None
    assert json.loads(driver_config(cfg, {"disk_mib": 2048})) == {"firecracker": {"disk_mib": 2048}}


def test_check_command_lists_the_library(tmp_path, capsys):
    from engine_harness import make_config

    from legion_orch.cli import cmd_check
    cmd_check(make_config(tmp_path, environment={}), None)
    assert "environment library" in capsys.readouterr().out


# ====================================================================== promotion
@scenario(**ENV)
async def test_promote_copies_an_approved_proposal_into_the_library(f: Flow):
    import shutil

    from legion_orch.cli import promote
    lib_dir = f.tmp / "lib"
    shutil.copytree(ROOT / "environments", lib_dir)
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    s = spec(proposed={"tools": {"taplo": {"description": "taplo", "mise": {"taplo": "0.9"}}}},
             profiles={"dev": {"tools": ["python", "taplo"]}},
             roles={"implementer": "dev"})
    await f.environment(s)
    await f.critique("PASS")
    schema = Assets(ROOT / "assets").schemas.get("environment").body
    assert promote(f.cfg, lib_dir, schema, f.ce.cid, "tool", "taplo") == 1     # not approved yet
    await f.human("H-E1", "approve")
    assert promote(f.cfg, lib_dir, schema, f.ce.cid, "tool", "taplo") == 0
    assert "taplo" in load_library(lib_dir, schema).tools
    assert promote(f.cfg, lib_dir, schema, f.ce.cid, "tool", "taplo") == 1     # never overwrites
    assert promote(f.cfg, lib_dir, schema, f.ce.cid, "policy", "nope") == 1


def test_config_without_environment_section(tmp_path):
    from engine_harness import make_config
    assert make_config(tmp_path).environment is None
    cfg = load_config(make_config(tmp_path, environment={"registry": "r.example"}).base_dir
                      / "orchestrator.yaml")
    assert cfg.environment.registry == "r.example" and cfg.environment.max_vcpus == 16


def test_podman_builder_commands(tmp_path, monkeypatch):
    """Build when the image is missing (and push with a registry); import the profile and create
    the provider when missing; skip all of it when they exist."""
    import asyncio

    from engine_harness import make_config

    from legion_orch.sandbox import environment as envmod
    cfg = make_config(tmp_path, environment={"registry": "reg.example.com/legion"})
    cfg.sandbox.image = "registry/agent:1"
    calls: list[list[str]] = []
    exists: set[str] = set()

    async def fake_run(argv, timeout):
        calls.append(argv)
        probe = argv[1:3] in (["image", "exists"],) or "export" in argv or argv[-2:-1] == ["get"]
        if probe:
            return (0 if argv[-1] in exists or argv[-3] in exists else 1), ""
        return 0, "ok"
    monkeypatch.setattr(envmod, "_run", fake_run)
    b = envmod.PodmanBuilder(cfg)
    recipe = envmod.image_recipe(cfg, cfg.sandbox.image, [Tool("uv", "uv", {"uv": "0.9"})])
    ref, _ = asyncio.run(b.ensure_image(recipe))
    assert ref == recipe.tag and ref.startswith("reg.example.com/legion/legion-env:")
    verbs = [c[1] for c in calls]
    assert verbs == ["image", "build", "push"]
    prof = network_profile(Policy("pypi", "pypi", ({"host": "pypi.org", "port": 443},), ("/x",)))
    calls.clear()
    asyncio.run(b.ensure_provider(prof))
    ops = [" ".join(c[c.index("--workspace") + 2:][:2]) for c in calls]
    assert ops == ["profile export", "profile import", "provider get", "provider create"]
    assert calls[-1][-4:] == ["--name", prof["id"], "--type", prof["id"]]
    exists.update({recipe.tag, prof["id"]})
    calls.clear()
    asyncio.run(b.ensure_image(recipe))
    asyncio.run(b.ensure_provider(prof))
    assert [c[1] for c in calls][:1] == ["image"] and not any("create" in c or "build" in c
                                                              for c in calls)
    recipe.base = ""
    with pytest.raises(envmod.BuildError, match="sandbox.image"):
        asyncio.run(b.ensure_image(recipe))


def test_tool_env_is_baked_into_the_image_after_install():
    py = Tool(name="python", description="py", mise={"python": "3.12"})
    uv = Tool(name="uv", description="uv", mise={"uv": "0.9"},
              env=(("UV_PYTHON_DOWNLOADS", "never"), ("UV_PYTHON_PREFERENCE", "only-system")))
    lines = dockerfile("base", [py, uv], "v1", "sandbox").splitlines()
    install = next(i for i, x in enumerate(lines) if x.startswith("RUN mise install"))
    env = [i for i, x in enumerate(lines) if x.startswith("ENV UV_")]
    assert [lines[i] for i in env] == ['ENV UV_PYTHON_DOWNLOADS="never"',
                                       'ENV UV_PYTHON_PREFERENCE="only-system"']
    assert all(i > install for i in env) and lines[-1] == "USER sandbox"
    lib = load_library(ROOT / "environments", SCHEMA)
    assert lib.tools["uv"]["env"]["UV_PYTHON_DOWNLOADS"] == "never"


def test_tool_env_schema_rejects_path_mise_and_unsafe_values():
    from jsonschema import Draft202012Validator
    v = Draft202012Validator({"$defs": SCHEMA["$defs"], "$ref": "#/$defs/tool"})
    ok = {"description": "x", "mise": {"x": "1"}}
    assert v.is_valid(ok | {"env": {"X_MODE": "offline"}})
    for bad in ({"PATH": "/x"}, {"MISE_DATA_DIR": "/x"}, {"lower": "x"},
                {"X": "a\nRUN rm -rf /"}, {"X": "$HOME"}, {"X": 'a"b'}):
        assert not v.is_valid(ok | {"env": bad}), bad


@scenario(**ENV)
async def test_tool_env_credentials_and_conflicts_are_rejected(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    bad = spec(proposed={"tools": {
        "gh-cli": {"description": "gh", "mise": {"gh": "2"}, "env": {"GH_TOKEN": "abc"}},
        "uv-online": {"description": "uv", "mise": {"ruff": "0.14"},
                      "env": {"UV_PYTHON_DOWNLOADS": "automatic"}}}},
        profiles={"dev": {"tools": ["uv", "uv-online", "gh-cli"]}},
        roles={"implementer": "dev"})
    out = await f.environment(bad)
    text = " | ".join(out.violations)
    assert "G-E2: tool gh-cli: environment variable GH_TOKEN looks like a credential" in text
    assert "conflicting tool environment: UV_PYTHON_DOWNLOADS is 'never' in uv but 'automatic'" in text


def test_selected_tools_bring_their_network_for_their_own_binaries():
    from legion_orch.workflow.environment import resolve
    lib = load_library(ROOT / "environments", SCHEMA)
    s = spec(profiles={"uv": {"tools": ["python", "uv", "ruff"]},
                       "lint": {"tools": ["ruff", "python"]},
                       "quiet": {"tools": ["uv"], "offline": True},
                       "clone": {"tools": ["uv", "git"], "network": {"git": ["github-read"]}}},
             roles={"implementer": "uv", "verifier": "lint", "reviewer": "quiet", "oracle": "clone"})
    profiles, v = resolve(s, lib)
    assert not v, v
    uv_bins = ("/usr/local/share/mise/installs/uv/*/uv", "/usr/local/share/mise/installs/uv/*/*/uv")
    assert profiles["uv"].tools[1].binaries == uv_bins
    [pypi] = profiles["uv"].policies                 # brought by uv; python and ruff bring none
    assert pypi.name == "pypi" and pypi.tools == ("uv",) and pypi.binaries == uv_bins
    assert profiles["lint"].policies == [] and profiles["quiet"].policies == []
    by_name = {p.name: p for p in profiles["clone"].policies}
    assert set(by_name) == {"pypi", "github-read"}
    gh = by_name["github-read"]
    assert gh.tools == ("git",) and all("git-core" in b for b in gh.binaries)
    assert lib.tools["uv"]["network"] == ["pypi"] and "network" not in lib.tools["python"]


def test_binary_paths_for_backends_and_bundles():
    from legion_orch.sandbox.environment import binary_paths
    assert binary_paths({"npm:prettier": "3"}, ["bin/prettier"]) == (
        ("/usr/local/share/mise/installs/npm-prettier/*/bin/prettier",), None)
    assert binary_paths({"a": "1", "b": "2"}, ["/usr/bin/a"]) == (("/usr/bin/a",), None)
    _, err = binary_paths({"a": "1", "b": "2"}, ["bin/a"])
    assert "exactly one mise tool" in err


@scenario(**ENV)
async def test_network_extras_must_name_the_profiles_tools_and_reach_a_binary(f: Flow):
    await f.intake()
    await f.human("H-1", "accept")
    await f.plan()
    bad = spec(proposed={"policies": {"pkgs": {"description": "x",
                                               "endpoints": [{"host": "pkgs.example.com", "port": 443}]}},
                         "tools": {"docs": {"description": "x", "apt": ["pandoc"], "network": ["pkgs"]}}},
               profiles={"dev": {"tools": ["node", "docs"], "network": {"uv": ["pkgs"]}}},
               roles={"implementer": "dev"})
    out = await f.environment(bad)
    text = " | ".join(out.violations)
    assert "network extras for 'uv', which is not one of its tools" in text
    assert "network policy pkgs is for tools ['docs'], which declare no binaries" in text


def test_library_cross_checks(tmp_path):
    import shutil
    shutil.copytree(ROOT / "environments", tmp_path / "lib")
    (tmp_path / "lib/tools/x.yaml").write_text(yaml.safe_dump(
        {"description": "x", "apt": ["x"], "network": ["nope"]}))
    with pytest.raises(LibraryError, match=r"tool x needs unknown network policies \['nope'\]"):
        load_library(tmp_path / "lib", SCHEMA)
    (tmp_path / "lib/tools/x.yaml").unlink()
    (tmp_path / "lib/tools/two.yaml").write_text(yaml.safe_dump(
        {"description": "x", "mise": {"a": "1", "b": "2"}, "binaries": ["bin/a"]}))
    with pytest.raises(LibraryError, match="exactly one mise tool"):
        load_library(tmp_path / "lib", SCHEMA)


def test_language_preferences_reach_the_roles_that_plan_build_and_judge_code():
    a = Assets(ROOT / "assets")
    have = {r for r, role in a.roles.items() if "language-preferences" in role.skills}
    assert have == {"planner", "environment", "critic", "oracle", "implementer", "reviewer"}
    assert "uv" in a.skill("language-preferences") and "ruff" in a.skill("language-preferences")
