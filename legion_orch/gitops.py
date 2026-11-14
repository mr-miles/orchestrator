"""Controller-owned git hub (bare repository) served to agents over smart-HTTP.

* Agents clone/fetch from ``/git/<repo>.git`` and push only to their run branch
  (``pre-receive`` hook, fed ``LEGION_ALLOWED_REF`` by the HTTP layer).
* The integration branch is updated only by the controller, through a private worktree.
* Accepted evidence is pinned under ``refs/legion/evidence/<run>``.
"""
from __future__ import annotations

import asyncio
import fnmatch
import shutil
import stat
from pathlib import Path

PRE_RECEIVE = r"""#!/bin/sh
# legion-orch: a run token may only update its own branch; never delete refs.
z=0000000000000000000000000000000000000000
if [ -z "$LEGION_ALLOWED_REF" ]; then
  echo "legion: this credential is read-only" >&2; exit 1
fi
while read old new ref; do
  if [ "$ref" != "$LEGION_ALLOWED_REF" ]; then
    echo "legion: push to $ref denied; you may only push to $LEGION_ALLOWED_REF" >&2; exit 1
  fi
  if [ "$new" = "$z" ]; then
    echo "legion: deleting refs is not allowed" >&2; exit 1
  fi
done
exit 0
"""


class GitError(Exception):
    pass


async def run(*args: str, cwd: Path | None = None, check: bool = True,
              timeout: float = 300, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd) if cwd else None, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise GitError(f"timeout: {' '.join(args)}")
    text = out.decode(errors="replace")
    if check and proc.returncode != 0:
        raise GitError(f"{' '.join(args)} failed ({proc.returncode}): {text.strip()[:2000]}")
    return proc.returncode, text


def path_matches(path: str, pattern: str) -> bool:
    """Exact path, directory prefix (``dir/``) or glob."""
    if pattern.endswith("/"):
        return path.startswith(pattern)
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatchcase(path, pattern)
    return path == pattern


def paths_overlap(a: str, b: str) -> bool:
    if a == b:
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    if b.endswith("/") and a.startswith(b):
        return True
    if any(c in a + b for c in "*?["):
        return fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a)
    return False


class GitRemote:
    """Bare hub repository + controller-private integration worktree."""

    def __init__(self, root: Path, repo_name: str, source: str, base_branch: str):
        self.root = Path(root)
        self.repo_name = repo_name
        self.project_root = self.root / "served"           # GIT_PROJECT_ROOT for http-backend
        self.repo = self.project_root / f"{repo_name}.git"  # bare hub
        self.wt = self.root / "integration-wt"
        self.source = source
        self.base_branch = base_branch
        self.lock = asyncio.Lock()

    async def git(self, *args: str, check: bool = True, cwd: Path | None = None) -> str:
        return (await run("git", *args, cwd=cwd or self.repo, check=check))[1]

    async def ensure(self) -> None:
        if not (self.repo / "HEAD").exists():
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            await run("git", "clone", "--bare", "--quiet", self.source, str(self.repo))
            await self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        for k, v in (("http.receivepack", "true"), ("http.uploadpack", "true"),
                     ("receive.denyDeletes", "true"), ("uploadpack.allowAnySHA1InWant", "true"),
                     ("user.name", "legion-controller"),
                     ("user.email", "legion-controller@localhost")):
            await self.git("config", k, v)
        hook = self.repo / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(PRE_RECEIVE)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    async def resolve_base(self) -> str:
        if "://" in self.source or Path(self.source).exists():
            await self.git("fetch", "--quiet", "origin")
        return await self.rev_parse(f"refs/remotes/origin/{self.base_branch}")

    async def has_object(self, sha: str, path: str | None = None) -> bool:
        spec = f"{sha}:{path}" if path else f"{sha}^{{commit}}"
        rc, _ = await run("git", "cat-file", "-e", spec, cwd=self.repo, check=False)
        return rc == 0

    async def rev_parse(self, ref: str) -> str:
        return (await self.git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")).strip()

    async def ref_sha(self, ref: str) -> str | None:
        rc, out = await run("git", "rev-parse", "--verify", "--quiet", ref, cwd=self.repo,
                            check=False)
        return out.strip() if rc == 0 else None

    async def is_ancestor(self, a: str, b: str) -> bool:
        rc, _ = await run("git", "merge-base", "--is-ancestor", a, b, cwd=self.repo, check=False)
        return rc == 0

    async def changed_paths(self, base: str, head: str) -> list[str]:
        out = await self.git("diff", "--name-only", "--no-renames", f"{base}..{head}")
        return [line for line in out.splitlines() if line]

    async def blob(self, sha: str, path: str) -> str | None:
        rc, out = await run("git", "rev-parse", f"{sha}:{path}", cwd=self.repo, check=False)
        return out.strip() if rc == 0 else None

    async def pin(self, name: str, sha: str) -> None:
        await self.git("update-ref", f"refs/legion/evidence/{name}", sha)

    # ----------------------------------------------------------- integration
    @staticmethod
    def integration_branch(change_id: str) -> str:
        return f"legion/{change_id}/integration"

    async def init_integration(self, change_id: str, base_sha: str) -> None:
        branch = self.integration_branch(change_id)
        if self.wt.exists():
            await self.git("worktree", "remove", "--force", str(self.wt), check=False)
            shutil.rmtree(self.wt, ignore_errors=True)
        await self.git("worktree", "prune")
        await self.git("branch", "-f", branch, base_sha)
        await self.git("worktree", "add", "--quiet", str(self.wt), branch)

    async def integration_head(self, change_id: str) -> str:
        return await self.rev_parse(f"refs/heads/{self.integration_branch(change_id)}")

    async def merge(self, sha: str, message: str) -> tuple[bool, str]:
        """Merge a candidate into the integration worktree. Returns (clean, head | detail)."""
        async with self.lock:
            rc, out = await run("git", "merge", "--no-ff", "-m", message, sha,
                                cwd=self.wt, check=False)
            if rc != 0:
                await run("git", "merge", "--abort", cwd=self.wt, check=False)
                return False, out
            head = (await run("git", "rev-parse", "HEAD", cwd=self.wt))[1].strip()
            return True, head
