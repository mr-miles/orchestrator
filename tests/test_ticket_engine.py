"""The engine on tickets, end to end: a real controller, store and hub; agents and decision
owners played through the engine's API (tests/engine_harness.py)."""
from __future__ import annotations

from engine_harness import Flow, scenario


@scenario()
async def test_a_change_runs_from_intake_to_publication(f: Flow):
    await f.to_end()
    assert f.ce.change.status == "READY", f.status()
    assert all(t.status == "passed" for t in f.ce.st.tickets.values()), f.status()
    assert f.ce.change.tier == "R1"
    ptr = f.ce.tickets.root.pointers
    assert {"requirements", "risk", "spec", "oracle", "environment", "tickets", "pr",
            "head"} <= set(ptr)
    import subprocess
    files = subprocess.run(["git", "ls-tree", "-r", "--name-only", "main"], cwd=f.git.repo,
                           capture_output=True, text=True, check=True).stdout.split()
    assert "src/greet.txt" in files                    # published: merged into the base branch
    assert f.runner.deleted                             # closed tickets' sandboxes went
