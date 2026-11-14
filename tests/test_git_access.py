"""The controller's git remote: push restricted to the run's own branch; judges read-only."""
import asyncio
import socket
import subprocess
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route

from legion_orch.git import GitGrant, GitRemote, make_git_endpoint
from legion_orch.util import bearer


def _git(*a, cwd=None, check=True):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=check)


def test_push_rules(tmp_path: Path):
    asyncio.run(_push_rules(tmp_path))


async def _push_rules(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    (src / "f").write_text("1")
    _git("add", "f", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)

    remote = GitRemote(tmp_path / "git", "app", str(src), "main")
    await remote.ensure()
    base = await remote.resolve_base()
    await remote.init_integration("c1", base)
    grants = {"writer": GitGrant("r1", "refs/heads/legion/c1/build-t1"), "judge": GitGrant("r2", None)}
    endpoint = make_git_endpoint(remote, lambda req: grants.get(bearer(req.headers.get("authorization", ""))))
    app = Starlette(routes=[Route("/git/{path:path}", endpoint,
                                  methods=["GET", "POST"])])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    url = f"http://127.0.0.1:{port}/git/app.git"

    def as_(tok, *a, cwd=None):
        return _git("-c", f"http.extraHeader=Authorization: Bearer {tok}", *a, cwd=cwd, check=False)

    def run():
        work = tmp_path / "work"
        assert as_("nobody", "clone", "-q", url, str(tmp_path / "nope")).returncode != 0
        assert as_("judge", "clone", "-q", url, str(work)).returncode == 0
        (work / "f").write_text("2")
        _git("-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qam", "change", cwd=work)
        r = as_("judge", "push", "origin", "HEAD:refs/heads/legion/c1/build-t1", cwd=work)
        assert r.returncode != 0                                        # judges cannot push
        r = as_("writer", "push", "origin", "HEAD:refs/heads/legion/c1/integration", cwd=work)
        assert r.returncode != 0 and "denied" in r.stderr                # not your branch
        r = as_("writer", "push", "origin", "HEAD:refs/heads/legion/c1/build-t1", cwd=work)
        assert r.returncode == 0, r.stderr                               # own branch ok
        r = as_("writer", "push", "origin", ":refs/heads/legion/c1/build-t1", cwd=work)
        assert r.returncode != 0                                         # no deletes
        return _git("rev-parse", "HEAD", cwd=work).stdout.strip()

    try:
        sha = await asyncio.to_thread(run)
        assert await remote.ref_sha("refs/heads/legion/c1/build-t1") == sha
        assert await remote.integration_head("c1") == base
    finally:
        server.should_exit = True
        await task
