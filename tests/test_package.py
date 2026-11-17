"""The release zip (scripts/package.py): the runtime files and the built UI, nothing else."""
from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("package", ROOT / "scripts/package.py")
package = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package)


def test_the_zip_runs_with_uv_alone(tmp_path):
    ui = tmp_path / "ui"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<!doctype html>")
    (ui / "assets/index-1.js").write_text("export {}")
    z = zipfile.ZipFile(package.build(ui, tmp_path / "out"))
    names = set(z.namelist())
    top = next(iter(names)).split("/")[0]
    assert top.startswith("legion-orchestrator-") and all(n.startswith(top + "/") for n in names)
    rel = {n[len(top) + 1:] for n in names}
    for need in ("pyproject.toml", "uv.lock", "orchestrator.example.yaml", "RUNNING.md",
                 "legion_orch/cli.py", "assets/roles.yaml", "policies/README.md",
                 "environments/tools/bun.yaml", "legion_orch/server/static/dist/index.html",
                 "legion_orch/server/static/dist/assets/index-1.js"):
        assert need in rel, need
    # no tests, frontend sources, caches or local state: only the runtime, from git
    assert not any(r.startswith(("tests/", "frontend/", ".venv/", "dist/")) or "__pycache__" in r
                   for r in rel)
    assert "uv run legion-orch" in z.read(f"{top}/RUNNING.md").decode()


def test_the_zip_needs_a_built_ui(tmp_path):
    with pytest.raises(SystemExit, match="build the UI first"):
        package.build(tmp_path / "nothing", tmp_path / "out")
