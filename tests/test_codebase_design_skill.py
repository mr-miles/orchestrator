from pathlib import Path

from legion_orch.workflow.assets import MAX_DESCRIPTION, TEMPLATE_VARS, Assets

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "assets" / "skills" / "codebase-design.md"


def test_assets_load():
    assert "codebase-design" in Assets(ROOT / "assets").skills_


def test_description_non_empty_and_within_limit():
    desc = Assets(ROOT / "assets").skill_descriptions["codebase-design"]
    assert desc.strip()
    assert len(desc) <= MAX_DESCRIPTION


def test_records_upstream_and_licence():
    text = SKILL.read_text()
    assert "mattpocock/skills" in text
    assert "licen" in text.lower()


def test_no_template_braces():
    assert "{{" not in SKILL.read_text()


def test_rendered_as_legion_codebase_design():
    assets = Assets(ROOT / "assets")
    md = assets._skill_md("codebase-design", {v: "x" for v in TEMPLATE_VARS})
    assert md.startswith("---")
    assert "legion-codebase-design" in md
