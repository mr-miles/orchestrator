"""Build the runnable release zip: the controller with its built web UI, run with uv alone.

    python scripts/package.py [--ui-dist DIR] [--out DIR]

The zip holds one top-level directory with the project's runtime files – ``pyproject.toml`` and
``uv.lock``, the ``legion_orch`` package with the built UI in ``legion_orch/server/static/dist``,
and the ``assets``, ``policies`` and ``environments`` folders the config points at – so

    unzip legion-orchestrator-<version>.zip && cd legion-orchestrator-<version>
    cp orchestrator.example.yaml orchestrator.yaml        # then edit it
    uv run legion-orch -c orchestrator.yaml serve

needs neither bun nor Node: the UI is already built (``cd frontend && bun run build`` first).
Only files tracked by git are taken, so caches and local state never leak into a release.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI_DIST = Path("legion_orch/server/static/dist")
# What the controller needs at run time (config paths are relative to the config file).
FILES = ["pyproject.toml", "uv.lock", "README.md", "WORKFLOW.md", "orchestrator.example.yaml"]
DIRS = ["legion_orch", "assets", "policies", "environments", "images"]

RUNNING = """# Running Legion Orchestrator {version}

Needs only [uv](https://docs.astral.sh/uv/) (it fetches Python and the dependencies in
`uv.lock`); the web UI is already built into `legion_orch/server/static/dist`.

```bash
cp orchestrator.example.yaml orchestrator.yaml   # set repo, decision_owners, sandbox settings
uv run legion-orch -c orchestrator.yaml check     # validate the assets and the library
uv run legion-orch -c orchestrator.yaml serve     # UI at http://127.0.0.1:8765/
```

See README.md for OpenShell setup and the other commands.
"""


def tracked(paths: list[str]) -> list[Path]:
    out = subprocess.run(["git", "ls-files", "-z", "--", *paths], cwd=ROOT, check=True,
                         capture_output=True).stdout.decode()
    return [Path(p) for p in out.split("\0") if p and (ROOT / p).is_file()]


def build(ui_dist: Path, out_dir: Path) -> Path:
    if not (ui_dist / "index.html").is_file():
        sys.exit(f"{ui_dist}/index.html not found: build the UI first (cd frontend && bun run build)")
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    top = Path(f"legion-orchestrator-{version}")
    files = [p for p in tracked(FILES + DIRS) if UI_DIST not in p.parents]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{top}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files:
            z.write(ROOT / p, top / p)
        for f in sorted(x for x in ui_dist.rglob("*") if x.is_file()):
            z.write(f, top / UI_DIST / f.relative_to(ui_dist))
        z.writestr(str(top / "RUNNING.md"), RUNNING.format(version=version))
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ui-dist", type=Path, default=ROOT / UI_DIST, help="the built UI")
    ap.add_argument("--out", type=Path, default=ROOT / "dist", help="where the zip goes")
    args = ap.parse_args()
    print(build(args.ui_dist.resolve(), args.out.resolve()))


if __name__ == "__main__":
    main()
