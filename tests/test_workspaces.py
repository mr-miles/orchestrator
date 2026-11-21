"""Workspaces: the configured repositories changes are made in – one hub each, a change in
exactly one, and nothing of one reachable from another's change."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from engine_harness import REPO, Flow

from legion_orch.config import WorkspaceConfig
from legion_orch.git import GitRemote
from legion_orch.server.web import build_app
from legion_orch.workflow.engine import Controller
from legion_orch.workflow.engine.base.common import WorkflowError


async def two_workspaces(tmp_path) -> Flow:
    """The test workspace (a source repository) and ``scratch`` (no source: a blank one)."""
    f = Flow(tmp_path)
    f.cfg.workspaces.append(WorkspaceConfig("scratch"))
    f.ctl = Controller(f.cfg, runner=f.runner, builder=f.builder)
    for hub in f.ctl.hubs.values():
        await hub.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    return f


def test_a_change_is_made_in_one_workspace_and_its_hub(tmp_path):
    async def go():
        f = await two_workspaces(tmp_path)
        with pytest.raises(WorkflowError, match="workspace is required: one of app, scratch"):
            await f.ctl.start_change("Say hello", "")      # no workspace: kept for later
        with pytest.raises(WorkflowError, match="unknown workspace 'nope'"):
            await f.ctl.start_change("Say hello", "nope")
        a = await f.ctl.start_change("Say hello", REPO)
        b = await f.ctl.start_change("Start a new tool", "scratch")
        f.ce = a                                    # the harness settles on one change
        await f.settle()
        assert (a.change.workspace, b.change.workspace) == (REPO, "scratch")
        assert a.git is f.ctl.hub(REPO) and b.git is f.ctl.hub("scratch")
        assert not a.change.blank_repo and b.change.blank_repo       # scratch has no source
        assert a.git_url.endswith(f"/{REPO}.git") and b.git_url.endswith("/scratch.git")
        # each change's integration branch is in its own hub only
        for ce, other in ((a, b), (b, a)):
            branch = f"refs/heads/{GitRemote.integration_branch(ce.cid)}"
            assert await ce.git.ref_sha(branch) and not await other.git.ref_sha(branch)
        # a credential of a change's run reaches that change's hub only (GitGrant.repo)
        assert f.ctl._workspace_of(a.cid) == REPO and f.ctl._workspace_of(b.cid) == "scratch"
        assert f.ctl._workspace_of("c000000-0000") is None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                                     base_url="http://op") as c:
            ws = {w["id"]: w for w in (await c.get("/api/workspaces")).json()}
            assert set(ws) == {REPO, "scratch"}
            assert ws[REPO]["changes"] == ws["scratch"]["changes"] == {"ACTIVE": 1}
            assert ws[REPO]["publish_prefix"] == "agent/" and ws["scratch"]["source"] is None
            refs = (await c.get("/api/workspaces/scratch/git/refs")).json()
            assert f"legion/{b.cid}/integration" in [r["name"] for r in refs]
            assert f"legion/{a.cid}/integration" not in [r["name"] for r in refs]
            listed = {x["id"]: x["workspace"] for x in (await c.get("/api/changes")).json()}
            assert listed == {a.cid: REPO, b.cid: "scratch"}
            r = await c.post("/api/changes", json={"goal": "Again"})
            assert r.status_code == 400 and "workspace is required" in r.json()["error"]
            r = await c.post("/api/changes", json={"goal": "Again", "workspace": "scratch"})
            assert r.status_code == 200
            assert f.ctl.get(r.json()["change"]).change.workspace == "scratch"
        await f.ctl.stop()                      # its changes' work stops with it
    asyncio.run(go())


def test_a_change_whose_workspace_left_the_config_is_not_loaded(tmp_path):
    async def go():
        f = await two_workspaces(tmp_path)
        b = f.ce = await f.ctl.start_change("Start a new tool", "scratch")
        await f.settle()
        f.cfg.workspaces = [w for w in f.cfg.workspaces if w.name != "scratch"]
        again = Controller(f.cfg, runner=f.runner, builder=f.builder)
        again.load()
        assert b.cid not in again.changes
        await f.ctl.stop()
    asyncio.run(go())


# ---------------------------------------------------------------------------- syncing a hub
def _git(cwd, *args) -> str:
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def _source(path) -> str:
    """A source repository with one commit on main; returns the commit."""
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    (path / "a.txt").write_text("1\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "one")
    return _git(path, "rev-parse", "HEAD")


def _commit(path, text: str) -> str:
    (path / "a.txt").write_text(text)
    _git(path, "commit", "-qam", text)
    return _git(path, "rev-parse", "HEAD")


def test_sync_clones_then_fast_forwards_and_never_touches_legion_branches(tmp_path):
    async def go():
        src = tmp_path / "src"
        first = _source(src)
        hub = GitRemote(tmp_path / "git", "app", str(src), "main")
        assert not hub.exists
        assert await hub.sync() == first and hub.exists              # cloned
        await hub.init_integration("c1", first)
        second = _commit(src, "2\n")
        assert await hub.sync() == second                            # fast-forwarded
        assert await hub.ref_sha("refs/heads/main") == second
        assert await hub.integration_head("c1") == first             # legion/* untouched
    asyncio.run(go())


def test_sync_refuses_a_rewritten_base_or_another_origin(tmp_path):
    from legion_orch.git import WorkspaceUnsynced

    async def go():
        src = tmp_path / "src"
        _source(src)
        hub = GitRemote(tmp_path / "git", "app", str(src), "main")
        kept = _commit(src, "2\n")
        await hub.sync()
        _git(src, "reset", "-q", "--hard", "HEAD~1")                 # main rewritten
        _commit(src, "other\n")
        with pytest.raises(WorkspaceUnsynced, match="was rewritten"):
            await hub.sync()
        assert await hub.ref_sha("refs/heads/main") == kept          # nothing moved
        moved = GitRemote(tmp_path / "git", "app", str(tmp_path / "elsewhere"), "main")
        with pytest.raises(WorkspaceUnsynced, match="not the configured source"):
            await moved.sync()
    asyncio.run(go())


def test_one_sync_of_a_hub_at_a_time_and_a_waiting_one_takes_its_result(tmp_path, monkeypatch):
    async def go():
        src = tmp_path / "src"
        first = _source(src)
        hub = GitRemote(tmp_path / "git", "app", str(src), "main")
        running = peak = calls = 0
        real = hub._sync

        async def counted():
            nonlocal running, peak, calls
            running += 1
            calls += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            try:
                return await real()
            finally:
                running -= 1
        monkeypatch.setattr(hub, "_sync", counted)
        assert await asyncio.gather(*(hub.sync() for _ in range(5))) == [first] * 5
        assert peak == 1 and calls == 1                  # one ran; the others took its result
        second = _commit(src, "2\n")
        assert await hub.sync() == second and calls == 2  # a later sync fetches again
    asyncio.run(go())


def test_a_workspace_that_cannot_sync_escalates_and_a_retry_takes_it_up(tmp_path):
    """The source's base branch was rewritten: the change's first step escalates with the
    standard actions; the hub fixed by hand, a retry bases the change and intake starts."""
    from engine_harness import make_source_repo

    from legion_orch.workflow.model import ACCEPTED, ESCALATED, RUNNING

    async def go():
        make_source_repo(tmp_path / "src-repo")
        f = Flow(tmp_path)
        f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
        await f.git.sync()                                       # an earlier change's sync
        src = tmp_path / "src-repo"
        _git(src, "commit", "-q", "--amend", "-m", "init, rewritten")   # main rewritten
        f.ce = await f.ctl.start_change("Say hello", REPO)
        await f.settle()
        ws = f.ce.nodes["workspace"]
        assert ws.status == ESCALATED and ws.escalation.kind == "workspace_unsynced"
        assert "was rewritten" in ws.escalation.reason and f.ce.change.base_sha is None
        assert {"answer", "retry", "abandon"} <= set(f.ce._flow.actions("workspace"))
        assert "intake" not in [r.node_id for r in f.ce.runs.values()]   # nothing starts
        new = _git(src, "rev-parse", "HEAD")
        await f.git.git("update-ref", "refs/heads/main", new)    # the operator resets the hub
        await f.human("workspace", "retry")
        assert f.status("workspace") == ACCEPTED and f.ce.change.base_sha == new
        assert await f.git.integration_head(f.ce.cid) == new and f.status("intake") == RUNNING
        assert f.ce.json_doc("control/workspace.json")["base"] == new
        await f.ctl.stop()
    asyncio.run(go())
