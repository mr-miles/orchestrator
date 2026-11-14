"""git smart-HTTP endpoint (``git http-backend`` as CGI) with workload-identity authentication.

The host decides who may do what: ``authorize(request)`` returns a :class:`GitGrant` (identity and
the one ref it may push, or ``None`` for read-only) or ``None`` to refuse. Push restrictions are
enforced by the ``pre-receive`` hook via ``LEGION_ALLOWED_REF``.
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Callable

from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

from .remote import GitRemote

MAX_PUSH_BYTES = 512 * 1024 * 1024


@dataclass
class GitGrant:
    identity: str             # run id / verifier id
    allowed_ref: str | None   # None => read-only


def make_git_endpoint(remote: GitRemote, authorize: Callable[[Request], GitGrant | None]):
    backend = os.path.join(os.popen("git --exec-path").read().strip(), "git-http-backend")

    async def endpoint(request: Request) -> Response:
        grant = authorize(request)
        if grant is None:
            return PlainTextResponse("authentication required", status_code=401,
                                     headers={"WWW-Authenticate": 'Basic realm="legion"'})
        path = request.path_params["path"]
        if not path.startswith(f"{remote.repo_name}.git/") and path != f"{remote.repo_name}.git":
            return PlainTextResponse("unknown repository", status_code=404)
        service = request.query_params.get("service", "")
        if (service == "git-receive-pack" or path.endswith("/git-receive-pack")) \
                and grant.allowed_ref is None:
            return PlainTextResponse("legion: this credential is read-only", status_code=403)

        body = await request.body()
        if len(body) > MAX_PUSH_BYTES:
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
            "CONTENT_LENGTH": str(len(body)),
            "REMOTE_USER": grant.identity,
            "REMOTE_ADDR": request.client.host if request.client else "",
            "LEGION_ALLOWED_REF": grant.allowed_ref or "",
        }
        if "content-encoding" in request.headers:
            env["HTTP_CONTENT_ENCODING"] = request.headers["content-encoding"]
        if "git-protocol" in request.headers:
            env["GIT_PROTOCOL"] = request.headers["git-protocol"]
        proc = await asyncio.create_subprocess_exec(
            backend, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate(body)
        sep = out.find(b"\r\n\r\n")
        sep_len = 4
        if sep < 0:
            sep, sep_len = out.find(b"\n\n"), 2
        if sep < 0:
            return PlainTextResponse(f"git backend error: {err.decode(errors='replace')[:500]}",
                                     status_code=500)
        status, headers = 200, {}
        for line in out[:sep].decode(errors="replace").splitlines():
            k, _, v = line.partition(":")
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            elif k:
                headers[k.strip()] = v.strip()
        media = headers.pop("Content-Type", None)
        return Response(out[sep + sep_len:], status_code=status, headers=headers, media_type=media)

    return endpoint
