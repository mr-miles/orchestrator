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
import os
from collections.abc import Callable
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse

from .remote import GitRemote

MAX_CGI_HEADER = 64 * 1024
BODY_TIMEOUT_S = 600
EXIT_TIMEOUT_S = 30
CHUNK = 64 * 1024


@dataclass
class GitGrant:
    identity: str             # run id / verifier id
    allowed_ref: str | None   # None => read-only


def _kill(proc: asyncio.subprocess.Process | None) -> None:
    """Kill the backend if it is still running. It may already have exited (and been reaped,
    with its pid possibly reused), so "no such process" and "not permitted" are both fine."""
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            proc.kill()


def _valid_path(path: str, repo_name: str) -> bool:
    parts = path.split("/")
    return (parts[0] == f"{repo_name}.git" and "\\" not in path
            and not any(p in ("", ".", "..") for p in parts[1:]))


def make_git_endpoint(remote: GitRemote, authorize: Callable[[Request], GitGrant | None], *,
                      max_request_bytes: int = 256 * 1024 * 1024, max_concurrency: int = 8):
    backend = os.path.join(os.popen("git --exec-path").read().strip(), "git-http-backend")
    slots = asyncio.Semaphore(max_concurrency)

    async def endpoint(request: Request) -> Response:
        grant = authorize(request)
        if grant is None:
            return PlainTextResponse("authentication required", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="legion"'})
        path = request.path_params["path"]
        if not _valid_path(path, remote.repo_name):
            return PlainTextResponse("unknown repository", status_code=404)
        service = request.query_params.get("service", "")
        if (service == "git-receive-pack" or path.endswith("/git-receive-pack")) \
                and grant.allowed_ref is None:
            return PlainTextResponse("legion: this credential is read-only", status_code=403)
        length = request.headers.get("content-length")
        if length is not None:
            if not length.isdigit():
                return PlainTextResponse("bad Content-Length", status_code=400)
            if int(length) > max_request_bytes:
                return PlainTextResponse("payload too large", status_code=413)

        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(remote.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_PROJECT_ROOT": str(remote.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": "/" + path,
            "REQUEST_METHOD": request.method,
            "QUERY_STRING": request.url.query,
            "CONTENT_TYPE": request.headers.get("content-type", ""),
            "REMOTE_USER": grant.identity,
            "REMOTE_ADDR": request.client.host if request.client else "",
            "LEGION_ALLOWED_REF": grant.allowed_ref or "",
        }
        if length is not None:
            env["CONTENT_LENGTH"] = length          # absent => backend reads to EOF (chunked)
        if "content-encoding" in request.headers:
            env["HTTP_CONTENT_ENCODING"] = request.headers["content-encoding"]
        if "git-protocol" in request.headers:
            env["GIT_PROTOCOL"] = request.headers["git-protocol"]

        await slots.acquire()
        proc: asyncio.subprocess.Process | None = None
        tasks: list[asyncio.Task] = []
        state = {"too_large": False, "released": False}

        async def cleanup(graceful: bool = False) -> None:
            """Graceful: the response was streamed to the end, so the backend is exiting on its
            own – wait for it briefly. Otherwise (error, refusal, client gone) kill it."""
            if proc is not None and graceful:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), EXIT_TIMEOUT_S)
            _kill(proc)
            if proc is not None:
                with contextlib.suppress(Exception):
                    await proc.wait()
            for t in tasks:
                t.cancel()
            if not state["released"]:
                state["released"] = True
                slots.release()

        try:
            proc = await asyncio.create_subprocess_exec(
                backend, env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stderr_tail = bytearray()

            async def drain_stderr() -> None:
                while chunk := await proc.stderr.read(CHUNK):
                    stderr_tail.extend(chunk)
                    del stderr_tail[:-4096]

            async def pump() -> None:
                # Count the whole request even if the backend stops reading (it exits on bad
                # input): the cap applies to what the client sends, and the body must be consumed
                # before the response starts (see below).
                total, backend_open = 0, True
                try:
                    async for chunk in request.stream():
                        total += len(chunk)
                        if total > max_request_bytes:
                            state["too_large"] = True
                            _kill(proc)
                            return
                        if backend_open:
                            try:
                                proc.stdin.write(chunk)
                                await proc.stdin.drain()
                            except (BrokenPipeError, ConnectionResetError):
                                backend_open = False
                finally:
                    with contextlib.suppress(Exception):
                        proc.stdin.close()

            tasks += [asyncio.create_task(drain_stderr()), asyncio.create_task(pump())]

            # CGI response headers (bounded).
            head = b""
            while b"\r\n\r\n" not in head and b"\n\n" not in head and len(head) <= MAX_CGI_HEADER:
                chunk = await proc.stdout.read(CHUNK)
                if not chunk:
                    break
                head += chunk
            # Finish consuming the request before responding: the backend emits its headers before
            # reading the body, and once a StreamingResponse starts, Starlette's disconnect listener
            # reads the same receive channel and would swallow the rest of the body. git only
            # produces bulk output after reading all of its input, so this cannot deadlock.
            try:
                await asyncio.wait_for(asyncio.shield(tasks[1]), BODY_TIMEOUT_S)
            except TimeoutError:
                await cleanup()
                return PlainTextResponse("request body timeout", status_code=408)
            if state["too_large"]:
                await cleanup()
                return PlainTextResponse("payload too large", status_code=413)
            sep, sep_len = head.find(b"\r\n\r\n"), 4
            if sep < 0:
                sep, sep_len = head.find(b"\n\n"), 2
            if sep < 0:
                await cleanup()
                return PlainTextResponse(
                    f"git backend error: {bytes(stderr_tail).decode(errors='replace')[-500:]}",
                    status_code=500)
            status, headers = 200, {}
            for line in head[:sep].decode(errors="replace").splitlines():
                k, _, v = line.partition(":")
                if k.lower() == "status":
                    status = int(v.strip().split()[0])
                elif k:
                    headers[k.strip()] = v.strip()
            media = headers.pop("Content-Type", None)
            rest = head[sep + sep_len:]

            async def body():
                complete = False
                try:
                    if rest:
                        yield rest
                    while chunk := await proc.stdout.read(CHUNK):
                        yield chunk
                    complete = True
                finally:
                    await cleanup(graceful=complete)

            return StreamingResponse(body(), status_code=status, headers=headers, media_type=media)
        except BaseException:
            await cleanup()
            raise

    return endpoint
