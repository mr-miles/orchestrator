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
    asyncio.run(go())
