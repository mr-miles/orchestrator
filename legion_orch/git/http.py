"""git smart-HTTP endpoint (``git http-backend`` as CGI) with workload-identity authentication.

The host decides who may do what: ``authorize(request)`` returns a :class:`GitGrant` (identity and
the one ref it may push, or ``None`` for read-only) or ``None`` to refuse. Push restrictions are
enforced by the ``pre-receive`` hook via ``LEGION_ALLOWED_REF``.

Callers are untrusted, so the transport is bounded:

* request bodies are streamed into the backend with a byte cap (an oversized ``Content-Length``
  is refused up front; chunked bodies are cut off when they exceed it) – never buffered whole;
* responses are streamed back, never buffered;
* at most ``max_concurrency`` backend processes run at once;
* only ``<repo>.git/...`` paths without ``.``/``..``/empty segments are served.

Object integrity and the unpacked size limit are enforced by the repository configuration
(``receive.fsckObjects``, ``receive.maxInputSize``; see remote.py).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from .remote import GitRemote

log = logging.getLogger("legion.git")

MAX_CGI_HEADER = 64 * 1024
BODY_TIMEOUT_S = 600
EXIT_TIMEOUT_S = 30
CHUNK = 64 * 1024


@dataclass
class GitGrant:
    identity: str             # run id / verifier id
    allowed_ref: str | None   # None => read-only
    repo: str                 # the one hub it may reach (its change's workspace)


def _valid_path(path: str, repo_name: str) -> bool:
    parts = path.split("/")
    return (parts[0] == f"{repo_name}.git" and "\\" not in path
            and not any(p in ("", ".", "..") for p in parts[1:]))


def _repo_of(path: str) -> str:
    """The hub a request path names (``<name>.git/...``), or '' if none."""
    first = path.split("/", 1)[0]
    return first[:-4] if first.endswith(".git") else ""


def _parse_cgi_head(head: bytes) -> tuple[int, dict[str, str], str | None, bytes] | None:
    """Split a CGI response into (status, headers, content type, start of body); None if the
    header block is incomplete."""
    sep, sep_len = head.find(b"\r\n\r\n"), 4
    if sep < 0:
        sep, sep_len = head.find(b"\n\n"), 2
    if sep < 0:
        return None
    status, headers = 200, {}
    for line in head[:sep].decode(errors="replace").splitlines():
        k, _, v = line.partition(":")
        if k.lower() == "status":
            status = int(v.strip().split()[0])
        elif k:
            headers[k.strip()] = v.strip()
    return status, headers, headers.pop("Content-Type", None), head[sep + sep_len:]


class _Backend:
    """One ``git http-backend`` process serving one request.

    Feeds it the request body under a byte cap, keeps the tail of its stderr, and on
    :meth:`close` stops it and releases its concurrency slot exactly once.
    """

    def __init__(self, proc: asyncio.subprocess.Process, slots: asyncio.Semaphore):
        self.proc, self._slots = proc, slots
        self.too_large = False
        self._stderr = bytearray()
        self._tasks = [asyncio.create_task(self._drain_stderr())]
        self._pump: asyncio.Task | None = None
        self._released = False

    @classmethod
    async def start(cls, exe: str, env: dict[str, str], slots: asyncio.Semaphore) -> _Backend:
        """Start the backend; the caller must already hold a slot (released by close())."""
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE)
        except BaseException:
            slots.release()
            raise
        return cls(proc, slots)

    @property
    def stderr_tail(self) -> str:
        return bytes(self._stderr).decode(errors="replace")

    async def _drain_stderr(self) -> None:
        while chunk := await self.proc.stderr.read(CHUNK):
            self._stderr.extend(chunk)
            del self._stderr[:-4096]

    def feed(self, request: Request, max_bytes: int) -> None:
        """Stream the request body into the backend in the background."""
        async def pump() -> None:
            # Count the whole request even if the backend stops reading (it exits on bad input):
            # the cap applies to what the client sends, and the body must be consumed before the
            # response starts (see body_consumed).
            total, backend_open = 0, True
            try:
                async for chunk in request.stream():
                    total += len(chunk)
                    if total > max_bytes:
                        self.too_large = True
                        self._kill()
                        return
                    if backend_open:
                        try:
                            self.proc.stdin.write(chunk)
                            await self.proc.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            backend_open = False
            finally:
                with contextlib.suppress(Exception):
                    self.proc.stdin.close()
        self._pump = asyncio.create_task(pump())
        self._tasks.append(self._pump)

    async def body_consumed(self, timeout: float) -> bool:
        """Wait until the whole request body was read. The backend emits its headers before
        reading the body, and once a StreamingResponse starts, Starlette's disconnect listener
        reads the same receive channel and would swallow the rest of the body. git only produces
        bulk output after reading all of its input, so waiting here cannot deadlock."""
        try:
            await asyncio.wait_for(asyncio.shield(self._pump), timeout)
            return True
        except TimeoutError:
            return False

    async def read_head(self) -> bytes:
        """The CGI header block (bounded), possibly with the start of the body."""
        head = b""
        while b"\r\n\r\n" not in head and b"\n\n" not in head and len(head) <= MAX_CGI_HEADER:
            chunk = await self.proc.stdout.read(CHUNK)
            if not chunk:
                break
            head += chunk
        return head

    async def stream(self, first: bytes) -> AsyncIterator[bytes]:
        """The response body; closes the backend when done (gracefully if streamed to the end)."""
        complete = False
        try:
            if first:
                yield first
            while chunk := await self.proc.stdout.read(CHUNK):
                yield chunk
            complete = True
        finally:
            await self.close(graceful=complete)

    def _kill(self) -> None:
        """Kill the backend if still running. It may already have exited (and been reaped, with
        its pid possibly reused), so "no such process" and "not permitted" are both fine."""
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                self.proc.kill()

    async def close(self, graceful: bool = False) -> None:
        """Graceful: the response was streamed to the end, so the backend is exiting on its own –
        wait for it briefly. Otherwise (error, refusal, client gone) kill it."""
        if graceful:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.proc.wait(), EXIT_TIMEOUT_S)
        self._kill()
        with contextlib.suppress(Exception):
            await self.proc.wait()
        for t in self._tasks:
            t.cancel()
        if not self._released:
            self._released = True
            self._slots.release()


def git_exec_path() -> str:
    """git's helper directory (``git --exec-path``), waiting for git to exit (``os.popen`` left
    the process unreaped)."""
    return subprocess.run(["git", "--exec-path"], check=True, capture_output=True,
                          text=True).stdout.strip()


class GitEndpoint:
    """The smart-HTTP endpoint for the hubs (see the module docstring): a request reaches only
    the hub its grant names."""

    def __init__(self, remotes: Mapping[str, GitRemote],
                 authorize: Callable[[Request], GitGrant | None], *,
                 max_request_bytes: int = 256 * 1024 * 1024, max_concurrency: int = 8,
                 on_push: Callable[[GitGrant], Awaitable[None]] | None = None):
        self.remotes, self.authorize, self.on_push = remotes, authorize, on_push
        self.max_request_bytes = max_request_bytes
        self._slots = asyncio.Semaphore(max_concurrency)
        self._exe = os.path.join(git_exec_path(), "git-http-backend")

    async def handle(self, request: Request) -> Response:
        grant = self.authorize(request)
        if grant is None:
            return PlainTextResponse("authentication required", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="legion"'})
        refusal = self._refusal(request, grant)
        if refusal is not None:
            return refusal
        remote = self.remotes[grant.repo]
        await self._slots.acquire()
        backend = await _Backend.start(self._exe, self._cgi_env(request, grant, remote),
                                       self._slots)
        try:
            pushed = (self.on_push if request.method == "POST"
                      and request.path_params["path"].endswith("/git-receive-pack") else None)
            return await self._respond(request, backend,
                                       (lambda: pushed(grant)) if pushed else None)
        except BaseException:
            await backend.close()
            raise

    def _refusal(self, request: Request, grant: GitGrant) -> Response | None:
        """Checks that need no backend: repository path, read-only grants, declared size."""
        path = request.path_params["path"]
        if _repo_of(path) != grant.repo or grant.repo not in self.remotes \
                or not _valid_path(path, grant.repo):
            return PlainTextResponse("unknown repository", status_code=404)
        service = request.query_params.get("service", "")
        if (service == "git-receive-pack" or path.endswith("/git-receive-pack")) \
                and grant.allowed_ref is None:
            return PlainTextResponse("legion: this credential is read-only", status_code=403)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isdigit():
                return PlainTextResponse("bad Content-Length", status_code=400)
            if int(length) > self.max_request_bytes:
                return PlainTextResponse("payload too large", status_code=413)
        return None

    def _cgi_env(self, request: Request, grant: GitGrant, remote: GitRemote) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(remote.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PROJECT_ROOT": str(remote.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": "/" + request.path_params["path"],
            "REQUEST_METHOD": request.method,
            "QUERY_STRING": request.url.query,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "REMOTE_USER": grant.identity,
            "REMOTE_ADDR": request.client.host if request.client else "",
            "LEGION_ALLOWED_REF": grant.allowed_ref or "",
        }
        if "content-length" in request.headers:
            env["CONTENT_LENGTH"] = request.headers["content-length"]   # absent => read to EOF
        if "content-encoding" in request.headers:
            env["HTTP_CONTENT_ENCODING"] = request.headers["content-encoding"]
        if "git-protocol" in request.headers:
            env["GIT_PROTOCOL"] = request.headers["git-protocol"]
        return env

    async def _respond(self, request: Request, backend: _Backend,
                       done: Callable[[], Awaitable[None]] | None = None) -> Response:
        backend.feed(request, self.max_request_bytes)
        head = await backend.read_head()
        if not await backend.body_consumed(BODY_TIMEOUT_S):
            await backend.close()
            return PlainTextResponse("request body timeout", status_code=408)
        if backend.too_large:
            await backend.close()
            return PlainTextResponse("payload too large", status_code=413)
        parsed = _parse_cgi_head(head)
        if parsed is None:
            await backend.close()
            return PlainTextResponse(f"git backend error: {backend.stderr_tail[-500:]}",
                                     status_code=500)
        status, headers, media, first = parsed
        body = backend.stream(first)
        if done is not None and status == 200:
            body = _then(body, done)                    # a push: told once git has answered
        return StreamingResponse(body, status_code=status, headers=headers, media_type=media)


async def _then(body: AsyncIterator[bytes], done: Callable[[], Awaitable[None]]):
    async for chunk in body:
        yield chunk
    try:
        await done()
    except Exception as e:                          # never fails the push itself
        log.warning("after a push: %r", e)


def make_git_endpoint(remotes: Mapping[str, GitRemote],
                      authorize: Callable[[Request], GitGrant | None], *,
                      max_request_bytes: int = 256 * 1024 * 1024, max_concurrency: int = 8,
                      on_push: Callable[[GitGrant], Awaitable[None]] | None = None):
    """A Starlette request handler for ``/git/{path:path}`` (GET and POST). ``on_push``: told
    after each push (``git-receive-pack``) git accepted the request for."""
    return GitEndpoint(remotes, authorize, max_request_bytes=max_request_bytes,
                       max_concurrency=max_concurrency, on_push=on_push).handle
