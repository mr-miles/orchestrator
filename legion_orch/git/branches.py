"""Branches that follow the ticket tree (plan: ~/plans/tickets-design.md §5), on the hub.

A code ticket's branch starts at its parent's branch head; when the ticket passes, the commit
its accepted report attests is merged back into the parent's branch (R5). Merges into a branch
are serialised (one lock per branch), and the check ("can merge") and the merge happen under the
same lock (R8). Everything is done in the bare hub – ``git merge-tree`` makes the merged tree,
``commit-tree`` the merge commit, and ``update-ref`` moves the branch only from the head the merge
was made on – so no worktree is involved and a branch that moved meanwhile is never overwritten.
"""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .remote import GitError, GitRemote, run

IDENTITY = ("-c", "user.name=legion-controller", "-c", "user.email=legion-controller@localhost")
_UNSAFE = re.compile(r"[\s~^:?*\[\\]|\.\.|@\{|\.lock$|^\.|/\.")


async def run_input(*args: str, cwd: Path, env: dict, data: str | None) -> tuple[int, str]:
    """A git command with ``data`` on its stdin (its stdout and stderr together)."""
    proc = await asyncio.create_subprocess_exec(
        *args, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await asyncio.wait_for(proc.communicate(data.encode() if data is not None else None),
                                    300)
    return proc.returncode, out.decode(errors="replace")


def ticket_branch(change: str, ticket: str) -> str:
    """A ticket's branch name: ``legion/<change>/t/<ticket>``, the id made ref-safe."""
    return f"legion/{change}/t/{_UNSAFE.sub('_', ticket)}"


def integration_branch(change: str) -> str:
    """The change's integration branch: its root ticket's (what is published)."""
    return GitRemote.integration_branch(change)


@dataclass(frozen=True)
class MergeCheck:
    """Whether ``sha`` merges cleanly into the branch at ``head``; ``conflicts``: the paths that
    conflict (none when clean); ``merged``: ``sha`` is in the branch already."""
    clean: bool
    head: str
    conflicts: tuple[str, ...] = ()
    merged: bool = False
    tree: str | None = None              # the merged tree (clean)

    def finding(self, branch: str) -> str:
        """What the ticket's agent is told when it does not merge (G-A2)."""
        return (f"G-A2: your commit conflicts with {branch} at {self.head[:12]} in "
                f"{', '.join(self.conflicts) or 'its history'}: rebase onto {self.head} "
                "and push your branch again")


class Branches:
    """The ticket branches of one hub."""

    def __init__(self, hub: GitRemote):
        self.hub = hub
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def lock(self, branch: str) -> asyncio.Lock:
        """The branch's lock: merges into it, and checks before them, one at a time."""
        return self._locks[branch]

    async def head(self, branch: str) -> str | None:
        return await self.hub.ref_sha(f"refs/heads/{branch}")

    async def create(self, branch: str, at: str) -> str:
        """The branch, starting at ``at`` – unless it exists (a restart, a rework: the ticket
        keeps its branch). Its head."""
        rc, _ = await run("git", "update-ref", f"refs/heads/{branch}", at, "0" * 40,
                          cwd=self.hub.repo, check=False)
        if rc != 0 and (head := await self.head(branch)) is not None:
            return head
        if rc != 0:
            raise GitError(f"cannot create {branch} at {at}")
        return at

    async def can_merge(self, sha: str, into: str) -> MergeCheck:
        """Whether ``sha`` merges cleanly into ``into`` now (under its lock)."""
        async with self.lock(into):
            return await self._check(sha, into)

    async def merge(self, sha: str, into: str, message: str) -> MergeCheck:
        """Merge ``sha`` into ``into`` (a merge commit, as the controller); the check's result,
        ``head`` the branch's new head when it was clean. Merged already: nothing changes."""
        async with self.lock(into):
            check = await self._check(sha, into)
            if not check.clean or check.merged:
                return check
            commit = (await self.hub.git(*IDENTITY, "commit-tree", check.tree, "-p", check.head,
                                         "-p", sha, "-m", message)).strip()
            await self.hub.git("update-ref", f"refs/heads/{into}", commit, check.head)
            return MergeCheck(True, commit)

    async def commit_file(self, branch: str, path: str, content: str, message: str) -> str:
        """Commit ``path`` with ``content`` on ``branch`` as the controller (in the bare hub, on
        the branch's head: never a stale worktree); its new head – unchanged when the file
        already had this content."""
        async with self.lock(branch):
            head = await self.head(branch)
            if head is None:
                raise GitError(f"no branch {branch}")
            with tempfile.TemporaryDirectory() as tmp:
                env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}

                async def git(*args: str, data: str | None = None) -> str:
                    rc, out = await run_input("git", *args, cwd=self.hub.repo, env=env, data=data)
                    if rc != 0:
                        raise GitError(f"git {' '.join(args)} failed ({rc}): {out.strip()[:500]}")
                    return out.strip()
                await git("read-tree", head)
                blob = await git("hash-object", "-w", "--stdin", data=content)
                await git("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}")
                tree = await git("write-tree")
                if tree == await git("rev-parse", f"{head}^{{tree}}"):
                    return head
                commit = await git(*IDENTITY, "commit-tree", tree, "-p", head, "-m", message)
            await self.hub.git("update-ref", f"refs/heads/{branch}", commit, head)
            return commit

    async def _check(self, sha: str, into: str) -> MergeCheck:
        head = await self.head(into)
        if head is None:
            raise GitError(f"no branch {into}")
        if await self.hub.is_ancestor(sha, head):
            return MergeCheck(True, head, merged=True)
        rc, out = await run("git", "merge-tree", "--write-tree", "--name-only", "--no-messages",
                            head, sha, cwd=self.hub.repo, check=False)
        if rc not in (0, 1):            # 0: clean, the tree; 1: the tree, the conflicted paths
            raise GitError(f"git merge-tree {head} {sha} failed ({rc}): {out.strip()[:2000]}")
        tree, *paths = out.splitlines()
        return MergeCheck(rc == 0, head, tuple(dict.fromkeys(paths)), tree=tree)
