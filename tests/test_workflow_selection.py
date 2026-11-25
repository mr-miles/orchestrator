"""A change chooses its workflow at creation and keeps it (REQ-2, REQ-3): the engine, the HTTP
API and the CLI client, over a tmp copy of ``assets/`` that has a second workflow."""
from __future__ import annotations

import shutil

import httpx
import pytest
import yaml
from engine_harness import REPO, ROOT, Flow

from legion_orch.server.web import build_app
from legion_orch.workflow.engine import WorkflowError

MARKER = "second workflow marker"


def _assets_with_second(tmp_path):
    """A copy of assets/ with workflows/second.yaml: the default with the describe goal changed."""
    dest = tmp_path / "assets-copy"
    shutil.copytree(ROOT / "assets", dest)
    text = (dest / "workflows" / "default.yaml").read_text()
    old = '"the pull request\'s description"'
    assert text.count(old) == 1
    (dest / "workflows" / "second.yaml").write_text(text.replace(old, f'"{MARKER}"'))
    return dest


def _flow(tmp_path, *, second: bool = True) -> Flow:
    """A Flow whose controller reads the assets copy (the config is changed before it is made)."""
    cls_init = Flow.__init__
    f = Flow.__new__(Flow)
    if second:
        # Flow.__init__ builds the config then the controller; swap the assets in between
        import engine_harness as eh
        orig = eh.make_config

        def patched(tp, **kw):
            cfg = orig(tp, **kw)
            cfg.assets_dir = _assets_with_second(tp)
            return cfg
        eh.make_config = patched
        try:
            cls_init(f, tmp_path)
        finally:
            eh.make_config = orig
    else:
        cls_init(f, tmp_path)
    return f


async def _start(f: Flow, workflow: str | None = None):
    await f.git.ensure()
    f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
    f.ce = await f.ctl.start_change("Say hello", REPO, workflow)
    await f.settle()
    return f.ce


def _describe_goal(ce) -> str:
    return next(t.goal for t in ce.tickets.t.values() if t.id.endswith("describe")
                or t.type == "agent:describer")


def _skill_texts(f: Flow) -> list[str]:
    out = []
    for run in f.ce.runs.values():
        p = f.ce.run_dir(run.id) / "payload" / "skills" / "legion-workflow" / "SKILL.md"
        if p.exists():
            out.append(p.read_text())
    return out


@pytest.mark.anyio
async def test_start_under_second_workflow(tmp_path):
    f = _flow(tmp_path)
    try:
        ce = await _start(f, "second")
        assert ce.change.workflow == "second"
        assert _describe_goal(ce) == MARKER
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_default_workflow_when_omitted(tmp_path):
    f = _flow(tmp_path)
    try:
        ce = await _start(f)
        assert ce.change.workflow == "default"
        assert _describe_goal(ce) == "the pull request's description"
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_unknown_workflow_creates_nothing(tmp_path):
    f = _flow(tmp_path)
    try:
        await f.git.ensure()
        with pytest.raises(WorkflowError, match="unknown workflow 'nope'"):
            await f.ctl.start_change("Say hello", REPO, "nope")
        assert f.ctl.changes == {}
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_restart_keeps_workflow_and_tickets(tmp_path):
    f = _flow(tmp_path)
    try:
        ce = await _start(f, "second")
        ids = sorted(ce.tickets.t)
        _, ce2 = await f.restart()
        assert ce2.change.workflow == "second"
        assert sorted(ce2.tickets.t) == ids
        assert _describe_goal(ce2) == MARKER
    finally:
        await f.stop()


@pytest.mark.anyio
async def test_rendered_skill_follows_the_workflow(tmp_path):
    f = _flow(tmp_path)
    (tmp_path / "other").mkdir()
    g = _flow(tmp_path / "other", second=False)
    try:
        await _start(f, "second")
        await _start(g)
        second, default = _skill_texts(f), _skill_texts(g)
        assert second and default
        assert all(MARKER in t for t in second)
        assert not any(MARKER in t for t in default)
    finally:
        await f.stop()
        await g.stop()


@pytest.mark.anyio
async def test_http_workflow_parameter_and_meta(tmp_path):
    f = _flow(tmp_path)
    try:
        await f.git.ensure()
        f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
        app = build_app(f.ctl)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://op") as c:
            meta = (await c.get("/api/meta")).json()
            assert meta["workflows"] == ["default", "design-review", "second"]
            assert meta["default_workflow"] == "default"
            for bad in ("x", 5):
                r = await c.post("/api/changes", json={"goal": "g", "workspace": REPO,
                                                        "workflow": bad})
                assert 400 <= r.status_code < 500, r.text
            assert (await c.get("/api/changes")).json() == []
            r = await c.post("/api/changes", json={"goal": "g", "workspace": REPO,
                                                    "workflow": "second"})
            assert r.status_code == 200, r.text
            [row] = (await c.get("/api/changes")).json()
            assert row["workflow"] == "second"
        f.ce = f.ctl.changes[r.json()["change"]]
        await f.settle()
    finally:
        await f.stop()


def test_second_workflow_file_is_valid_yaml(tmp_path):
    raw = yaml.safe_load((_assets_with_second(tmp_path) / "workflows" / "second.yaml").read_text())
    assert MARKER in yaml.safe_dump(raw)
