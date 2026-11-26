"""Scope at push (G-B3, G-O1, WORKFLOW.md §5.6): the hub's pre-receive hook refuses a push that
changes a path outside the pushing run's scope – the same rules the report is checked with – so
nothing out of scope reaches a verifier. Pushed for real, over the git endpoint, with the grant
the controller gives the run's bearer."""
from __future__ import annotations

import asyncio
import contextlib
import socket
import subprocess
from pathlib import Path

import pytest
import uvicorn
from engine_harness import REPO, Flow, make_source_repo, scenario, signals
from starlette.applications import Starlette
from starlette.routing import Route
from test_a2a import token

from legion_orch.git import PushScope, Rule, make_git_endpoint
from legion_orch.util import bearer
from legion_orch.workflow.model import Run
from legion_orch.workflow.roles.base import RoleHandler
from legion_orch.workflow.roles.implementer import Implementer

SETUP = '[tasks.setup]\nrun = "uv sync --locked"\n'


def _git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=False)


@contextlib.asynccontextmanager
async def hub(f: Flow):
    """The change's hub served over smart HTTP, each request authorised as the server does."""
    endpoint = make_git_endpoint(
        {REPO: f.ce.git},
        lambda req: f.ctl.auth.git_grant(bearer(req.headers.get("authorization", ""))))
    app = Starlette(routes=[Route("/git/{path:path}", endpoint, methods=["GET", "POST"])])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/git/{REPO}.git"
    finally:
        server.should_exit = True
        await task


class Clone:
    """A working copy of the run's branch, pushing as the run."""

    def __init__(self, f: Flow, run: Run, url: str, path: Path):
        self.run, self.url, self.path = run, url, path
        self.auth = f"http.extraHeader=Authorization: {token(f, run)['Authorization']}"

    def git(self, *a):
        return _git("-c", self.auth, "-c", "user.name=a", "-c", "user.email=a@a", *a,
                    cwd=self.path)

    def clone(self) -> Clone:
        r = _git("-c", self.auth, "clone", "-q", self.url, str(self.path))
        assert r.returncode == 0, r.stderr
        assert self.git("checkout", "-q", self.run.base_sha).returncode == 0
        return self

    def commit(self, files: dict[str, str | None], msg: str = "change") -> None:
        for rel, text in files.items():
            p = self.path / rel
            if text is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
        assert self.git("add", "-A").returncode == 0
        assert self.git("commit", "-qm", msg).returncode == 0

    def push(self) -> subprocess.CompletedProcess:
        return self.git("push", "-q", "origin", f"HEAD:refs/heads/{self.run.branch}")

    def reset(self) -> None:
        """Back to the hub's tip of the branch (the last push it let through), else the base."""
        fetched = self.git("fetch", "-q", "origin", self.run.branch).returncode == 0
        to = "FETCH_HEAD" if fetched else self.run.base_sha
        assert self.git("reset", "-q", "--hard", to).returncode == 0


async def pushing(f: Flow, clone: Clone, files: dict[str, str | None]) -> subprocess.CompletedProcess:
    def go():
        clone.reset()
        clone.commit(files)
        return clone.push()
    return await asyncio.to_thread(go)


# ====================================================================== the rules
def test_the_first_matching_rule_decides_and_the_scope_travels_as_json():
    s = PushScope("G-B3", "a" * 40, (
        Rule("oracle", ("tests/acc/",), why="{path} is the oracle's", hint=False),
        Rule("approved", ("src/routes.txt",), allow=True),
        Rule("protected", ("mise.toml", "src/routes.txt"), why="{path} is protected"),
        Rule("files", ("src/",), allow=True)), hint="ask for it")
    assert PushScope.from_json(s.to_json()) == s
    assert s.violations(["src/a.txt", "src/routes.txt"]) == []
    assert s.violations(["tests/acc/t.sh"]) == ["G-B3: tests/acc/t.sh is the oracle's"]
    assert s.violations(["mise.toml", "docs/x.md"]) == [
        "G-B3: mise.toml is protected", "G-B3: docs/x.md is outside the allowed paths",
        "G-B3: ask for it"]


# ====================================================================== a build's pushes
@pytest.mark.anyio
async def test_a_build_pushes_only_inside_its_scope(tmp_path):
    make_source_repo(tmp_path / "src-repo", {"mise.toml": SETUP})
    f = await Flow.create(tmp_path)
    try:
        await f.to_build()
        run = f.run("T1", "implementer")
        async with hub(f) as url:
            c = await asyncio.to_thread(Clone(f, run, url, tmp_path / "work").clone)
            r = await pushing(f, c, {"src/greet.txt": "hello\n"})
            assert r.returncode == 0, r.stderr                          # its own file
            r = await pushing(f, c, {"src/greet.txt": "hello\n", "mise.toml": SETUP + "#\n"})
            assert r.returncode != 0
            assert "G-B3: mise.toml is a protected path" in r.stderr     # the base has one
            assert "/request_scope" in r.stderr                          # how to ask
            r = await pushing(f, c, {"src/greet.txt": "hello\n", "docs/notes.md": "x\n"})
            assert "G-B3: docs/notes.md is outside the allowed paths" in r.stderr
            # the push that was refused is not on the hub: its last good push is
            tip = await f.ce.git.ref_sha(f"refs/heads/{run.branch}")
            assert await f.ce.git.changed_paths(run.base_sha, tip) == ["src/greet.txt"]
            # a decision owner's approval widens it, at the very next push
            async with f.ce.lock:
                q = f.ce.request_scope("q-1", "T1", "implementer", run.id, ["mise.toml"],
                                       "the build needs a new dependency installed")
            await f.ce.answer_question(q.id, "", "miles", "approve")
            r = await pushing(f, c, {"src/greet.txt": "hello\n", "mise.toml": SETUP + "#\n"})
            assert r.returncode == 0, r.stderr
    finally:
        await f.stop()


@scenario()
async def test_the_diff_is_counted_from_the_runs_base(f: Flow):
    """A push carrying a commit outside the scope and the commit reverting it changes nothing
    outside: let through, as the report's own check would."""
    await f.to_build()
    run = f.run("T1", "implementer")
    async with hub(f) as url:
        c = await asyncio.to_thread(Clone(f, run, url, f.tmp / "work").clone)

        def go():
            c.reset()
            c.commit({"src/greet.txt": "hello\n", "docs/notes.md": "x\n"}, "oops")
            c.commit({"docs/notes.md": None}, "revert the stray file")
            return c.push()
        r = await asyncio.to_thread(go)
        assert r.returncode == 0, r.stderr


@scenario()
async def test_a_build_never_pushes_the_oracles_tests(f: Flow):
    await f.to_build(signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2))
    run = f.run("T1", "implementer")
    async with hub(f) as url:
        c = await asyncio.to_thread(Clone(f, run, url, f.tmp / "work").clone)
        r = await pushing(f, c, {"src/greet.txt": "hello\n",
                                 "tests/acceptance/test_greet.sh": "exit 0\n"})
        assert r.returncode != 0
        assert "tests/acceptance/test_greet.sh is one of the oracle's protected tests" in r.stderr


@scenario()
async def test_the_oracle_pushes_only_its_tests(f: Flow):
    f.cfg.protected_paths = ["config/"]
    await f.intake(signals(scope_breadth=2, novelty_uncertainty=2, verification_quality=2))
    await f.signoff("H-1")
    await f.plan()
    await f.critique("PASS")
    await f.signoff("H-E1")
    if f.status("scaffold") != "cancelled":
        await f.implement("scaffold")
        await f.review("scaffold")
    run = f.run("oracle")
    async with hub(f) as url:
        c = await asyncio.to_thread(Clone(f, run, url, f.tmp / "work").clone)
        r = await pushing(f, c, {"tests/acceptance/test_greet.sh": "grep -q hello src/greet.txt\n"})
        assert r.returncode == 0, r.stderr
        r = await pushing(f, c, {"src/greet.txt": "hello\n"})
        assert "G-O1: src/greet.txt is outside the allowed paths" in r.stderr
        r = await pushing(f, c, {"config/app.ini": "x\n"})
        assert "G-O1: config/app.ini" in r.stderr


# ====================================================================== every pushing role
def test_a_role_that_pushes_without_a_scope_stops_the_controller(tmp_path, monkeypatch):
    monkeypatch.setattr(Implementer, "push_scope", RoleHandler.push_scope)

    async def go():
        with pytest.raises(RuntimeError, match=r"push without a push_scope.*implementer"):
            await Flow.create(tmp_path)
    asyncio.run(go())
