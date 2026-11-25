"""Selectable workflows: ``assets/workflows/*.yaml`` load, are validated, and render their skill."""
import shutil
from pathlib import Path

import pytest
import yaml

from legion_orch.workflow.assets import TEMPLATE_VARS, AssetError, Assets
from legion_orch.workflow.tickets.spec import WorkflowSpec
from legion_orch.workflow.tickets.spec import render as render_spec

REPO = Path(__file__).resolve().parent.parent / "assets"


@pytest.fixture
def copy(tmp_path):
    n = [0]

    def make(change=None, second: bool = False) -> Path:
        n[0] += 1
        d = tmp_path / str(n[0])
        shutil.copytree(REPO, d)
        if second:
            shutil.copy(d / "workflows" / "default.yaml", d / "workflows" / "other.yaml")
        if change:
            change(d)
        return d
    return make


def edit(name: str, fn):
    def go(d: Path):
        p = d / "workflows" / name
        data = yaml.safe_load(p.read_text())
        fn(data)
        p.write_text(yaml.safe_dump(data))
    return go


def test_two_valid_workflows_load(copy):
    a = Assets(copy(second=True))
    assert list(a.workflows) == ["default", "design-review", "other"]
    assert a.workflow is a.workflows["default"]


def test_malformed_file_is_refused_naming_it(copy):
    def bad(d):
        (d / "workflows" / "broken.yaml").write_text("- just\n- a list\n")
    with pytest.raises(AssetError, match="broken.yaml"):
        Assets(copy(bad))


def test_unknown_role_is_refused_naming_the_file(copy):
    def bad(data):
        data["skeleton"]["subtasks"][0]["type"] = "agent:nobody"
    with pytest.raises(AssetError, match=r"other\.yaml.*nobody"):
        Assets(copy(edit("other.yaml", bad), second=True))


def test_unknown_controller_type_is_refused_naming_the_file(copy):
    def bad(data):
        data["skeleton"]["subtasks"][0]["type"] = "controller:teleport"
    with pytest.raises(AssetError, match=r"other\.yaml.*teleport"):
        Assets(copy(edit("other.yaml", bad), second=True))


def test_bad_file_name_is_refused_naming_it(copy):
    def bad(d):
        shutil.copy(d / "workflows" / "default.yaml", d / "workflows" / "Bad_Name.yaml")
    with pytest.raises(AssetError, match="Bad_Name.yaml"):
        Assets(copy(bad))


def test_non_yaml_entry_is_refused(copy):
    def bad(d):
        (d / "workflows" / "notes.txt").write_text("x")
    with pytest.raises(AssetError, match="notes.txt"):
        Assets(copy(bad))


def test_folder_without_default_is_refused(copy):
    def bad(d):
        (d / "workflows" / "default.yaml").rename(d / "workflows" / "other.yaml")
    with pytest.raises(AssetError, match="default.yaml"):
        Assets(copy(bad))


def test_no_workflows_folder_means_no_workflow(copy):
    a = Assets(copy(lambda d: shutil.rmtree(d / "workflows")))
    assert a.workflows == {} and a.workflow is None


def test_workflow_skill_is_the_rendered_spec(copy):
    a = Assets(copy())
    assert a.workflow_skill("default") == render_spec(WorkflowSpec.load(REPO / "workflows/default.yaml"))
    with pytest.raises(AssetError, match="nope"):
        a.workflow_skill("nope")


def test_render_role_follows_the_workflow(copy):
    def add(data):
        t = {"id": "extra-ticket", "type": "controller:sync", "goal": "one more"}
        data["skeleton"]["subtasks"].append(t)
    a = Assets(copy(edit("other.yaml", add), second=True))
    role = sorted(a.roles)[0]
    v = {k: "x" for k in TEMPLATE_VARS}
    d, o = a.render_role(role, v), a.render_role(role, v, workflow="other")
    assert a.render_role(role, v, "default").pin == d.pin
    assert d.skills["legion-workflow"] != o.skills["legion-workflow"]
    assert d.pin != o.pin
