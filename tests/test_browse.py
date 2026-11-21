"""Browsing for the operator UI: workspaces, the repository's files, document downloads and the
configuration's definition files – read-only, and limited to what may be browsed."""
from __future__ import annotations

import httpx
from engine_harness import Flow, scenario

from legion_orch.server.web import build_app


def client(f: Flow) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(f.ctl)),
                             base_url="http://op")


@scenario()
async def test_the_workspace_lists_its_changes_and_its_repository(f: Flow):
    ws = f.cfg.workspaces[0].name
    async with client(f) as c:
        [w] = (await c.get("/api/workspaces")).json()
        assert w["id"] == ws and w["base_branch"] == f.cfg.workspaces[0].base_branch
        assert w["changes"] == {"ACTIVE": 1} and w["documents"] > 0
        [ch] = (await c.get("/api/changes")).json()
        assert ch["workspace"] == ws
        refs = (await c.get(f"/api/workspaces/{ws}/git/refs")).json()
        assert refs[0]["name"] == f.cfg.workspaces[0].base_branch and refs[0]["base"]
        assert f"legion/{f.ce.cid}/integration" in [r["name"] for r in refs]
        tree = (await c.get(f"/api/workspaces/{ws}/git/tree")).json()
        paths = [x["path"] for x in tree["files"]]
        assert "README.md" in paths and not tree["truncated"] and len(tree["sha"]) == 40
        v = (await c.get(f"/api/workspaces/{ws}/git/file", params={"path": "README.md"})).json()
        assert v["text"] and v["reason"] is None and v["sha"] == tree["sha"]
        d = await c.get(f"/api/workspaces/{ws}/git/file",
                        params={"path": "README.md", "download": "1", "ref": tree["sha"]})
        assert d.status_code == 200 and d.text == v["text"]
        assert d.headers["content-disposition"] == 'attachment; filename="README.md"'
        assert d.headers["content-type"].startswith("text/plain")
        for params in ({"ref": "HEAD~1"}, {"ref": "--all"}, {"ref": "0" * 40}):
            r = await c.get(f"/api/workspaces/{ws}/git/tree", params=params)
            assert r.status_code == 404 and "unknown branch" in r.json()["error"], params
        r = await c.get(f"/api/workspaces/{ws}/git/file", params={"path": "nope.txt"})
        assert r.status_code == 404
        assert (await c.get("/api/workspaces/other/git/refs")).status_code == 404


@scenario()
async def test_documents_download_at_a_version(f: Flow):
    await f.intake()
    path = f.ce.p("intake/requirements.md")
    async with client(f) as c:
        r = await c.get("/api/doc/raw", params={"path": path, "version": 1})
        assert r.status_code == 200 and "REQ-1" in r.text
        assert r.headers["content-disposition"] == 'attachment; filename="requirements-v1.md"'
        assert (await c.get("/api/doc/raw")).status_code == 400
        assert (await c.get("/api/doc/raw", params={"path": "changes/x/nope.md"})).status_code == 404


@scenario()
async def test_configuration_groups_list_their_definition_files_only(f: Flow):
    async with client(f) as c:
        skills = (await c.get("/api/config/skills")).json()
        paths = [x["path"] for x in skills["files"]]
        assert "skills/git-protocol.md" in paths and all(p.startswith("skills/") for p in paths)
        agents = [x["path"] for x in (await c.get("/api/config/agents")).json()["files"]]
        assert agents[0] == "roles.yaml" and "souls/plan-critic.md" in agents
        assert "prompts/implementer.md" in agents
        flows = [x["path"] for x in (await c.get("/api/config/workflows")).json()["files"]]
        assert "schemas/oracle.json" in flows
        v = (await c.get("/api/config/skills/file", params={"path": "skills/git-protocol.md"})).json()
        assert "Git protocol" in v["text"]
        d = await c.get("/api/config/agents/file", params={"path": "roles.yaml", "download": "1"})
        assert d.status_code == 200 and "implementer" in d.text
        for path in ("skills/../roles.yaml", "../orchestrator.yaml", "/etc/passwd", "roles.yaml"):
            r = await c.get("/api/config/skills/file", params={"path": path})
            assert r.status_code == 404, path
        assert (await c.get("/api/config/secrets")).status_code == 404
