"""The controller's git remote: push restricted to the run's own branch; judges read-only."""
import asyncio
import socket
import subprocess
from pathlib import Path

import pytest
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
    other = GitRemote(tmp_path / "git", "other", str(src), "main")     # another workspace's hub
    await other.ensure()
    grants = {"writer": GitGrant("r1", "refs/heads/legion/c1/build-t1", "app"),
              "judge": GitGrant("r2", None, "app"), "elsewhere": GitGrant("r3", None, "other")}
    endpoint = make_git_endpoint({"app": remote, "other": other},
                                 lambda req: grants.get(bearer(req.headers.get("authorization", ""))))
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
        # a grant reaches its own change's hub only
        assert as_("judge", "clone", "-q", url.replace("app.git", "other.git"),
                   str(tmp_path / "x")).returncode != 0
        assert as_("elsewhere", "clone", "-q", url, str(tmp_path / "y")).returncode != 0
        assert as_("elsewhere", "clone", "-q", url.replace("app.git", "other.git"),
                   str(tmp_path / "z")).returncode == 0
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


def test_transport_limits(tmp_path: Path):
    asyncio.run(_transport_limits(tmp_path))


async def _transport_limits(tmp_path: Path):
    import os

    import httpx
    src = tmp_path / "src"
    src.mkdir()
    _git("init", "-q", "-b", "main", cwd=src)
    (src / "f").write_text("1")
    _git("add", "f", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=src)
    # git itself refuses packs over 64 KiB; the HTTP layer refuses bodies over 1 MiB
    remote = GitRemote(tmp_path / "git", "app", str(src), "main", max_push_bytes=64 * 1024)
    await remote.ensure()
    for key, want in (("receive.fsckObjects", "true"), ("receive.maxInputSize", "65536"),
                      ("uploadpack.allowAnySHA1InWant", "false"), ("receive.denyDeletes", "true")):
        assert (await remote.git("config", key)).strip() == want, key
    grant = GitGrant("r1", "refs/heads/legion/c1/build-t1", "app")
    endpoint = make_git_endpoint({"app": remote}, lambda req: grant, max_request_bytes=1024 * 1024,
                                 max_concurrency=2)
    app = Starlette(routes=[Route("/git/{path:path}", endpoint, methods=["GET", "POST"])])
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    base = f"http://127.0.0.1:{port}/git"
    ctype = {"Content-Type": "application/x-git-receive-pack-request"}
    try:
        async def refused(content) -> bool:
            """413, or the server closing the connection while the client is still sending
            (normal when an upload is refused without being read)."""
            try:
                async with httpx.AsyncClient() as c1:
                    r = await c1.post(f"{base}/app.git/git-receive-pack", content=content, headers=ctype)
                return r.status_code == 413
            except (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError):
                return True

        async def chunks():                          # chunked (no Content-Length), 2 MiB
            for _ in range(32):
                yield b"x" * 65536
        assert await refused(b"x" * (1024 * 1024 + 1))   # declared too large: refused up front
        assert await refused(chunks())                    # streamed past the cap
        async with httpx.AsyncClient() as c:
            for path in ("other.git/info/refs", "app.git/%2e%2e/served/app.git/info/refs",
                         "app.git//info/refs"):
                assert (await c.get(f"{base}/{path}?service=git-upload-pack")).status_code == 404, path
            r = await c.get(f"{base}/app.git/info/refs?service=git-upload-pack")
            assert r.status_code == 200 and b"git-upload-pack" in r.content   # streamed response

        def push_big():                              # within HTTP cap, over git's maxInputSize
            work = tmp_path / "work"
            _git("clone", "-q", f"{base}/app.git", str(work))
            (work / "big.bin").write_bytes(os.urandom(200 * 1024))
            _git("add", "big.bin", cwd=work)
            _git("-c", "user.name=a", "-c", "user.email=a@a", "commit", "-qm", "big", cwd=work)
            return _git("push", "origin", "HEAD:refs/heads/legion/c1/build-t1", cwd=work, check=False)
        r = await asyncio.to_thread(push_big)
        assert r.returncode != 0
        assert await remote.ref_sha("refs/heads/legion/c1/build-t1") is None
    finally:
        server.should_exit = True
        await task


@pytest.mark.anyio
async def test_blank_hub_without_a_source_repository(tmp_path):
    """repo.path omitted: a new bare hub with one empty commit on the base branch, kept on restart."""
    remote = GitRemote(tmp_path / "git", "app", None, "trunk")
    await remote.ensure()
    base = await remote.resolve_base()
    assert len(base) == 40
    log = subprocess.run(["git", "log", "--format=%s", "trunk"], cwd=remote.repo, check=True,
                         capture_output=True, text=True).stdout.split("\n")
    assert log[0] == "legion: initial empty commit"
    tree = subprocess.run(["git", "ls-tree", "-r", base], cwd=remote.repo, check=True,
                          capture_output=True, text=True).stdout
    assert tree == ""
    await remote.ensure()                                # restart: same repository
    assert await remote.resolve_base() == base
    await remote.init_integration("c1", base)
    assert await remote.integration_head("c1") == base
