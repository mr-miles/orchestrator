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

from ..util import run_command

PRE_RECEIVE = r"""#!/bin/sh
# legion-orch: a run token may only update its own branch, never delete refs, and – when the
# run has a scope (LEGION_SCOPE, JSON) – only change the paths in it (push_scope.py, push_hook.py).
z=0000000000000000000000000000000000000000
if [ -z "$LEGION_ALLOWED_REF" ]; then
  echo "legion: this credential is read-only" >&2; exit 1
fi
updates=""
while read old new ref; do
  if [ "$ref" != "$LEGION_ALLOWED_REF" ]; then
    echo "legion: push to $ref denied; you may only push to $LEGION_ALLOWED_REF" >&2; exit 1
  fi
  if [ "$new" = "$z" ]; then
    echo "legion: deleting refs is not allowed" >&2; exit 1
  fi
  updates="$updates$old $new $ref
"
done
if [ -n "$LEGION_SCOPE" ]; then
  printf '%s' "$updates" | "$LEGION_PYTHON" -I -m legion_orch.git.push_hook || exit 1
fi
exit 0
"""


class WorkspaceUnsynced(Exception):
    """The hub cannot follow its source: what differs, for the decision owner to fix."""


class GitError(Exception):
    pass


class ShaError(ValueError):
    """A sha that names no single commit: what to give instead."""


async def run(*args: str, cwd: Path | None = None, check: bool = True,
              timeout: float = 300, env: dict | None = None) -> tuple[int, str]:
    try:
        rc, text = await run_command(args, timeout, env=env, cwd=str(cwd) if cwd else None)
    except TimeoutError:
        raise GitError(f"timeout: {' '.join(args)}") from None
    if check and rc != 0:
        raise GitError(f"{' '.join(args)} failed ({rc}): {text.strip()[:2000]}")
    return rc, text


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


# What a repository may hold and still be blank (§5.3d): documentation, licence files, git or
# forge settings and tool-version files (mise) – nothing that builds, tests or runs code.
BLANK_REPO_FILES = ("*.md", "*.rst", "*.adoc", "LICENSE*", "LICENCE*", "COPYING*", "NOTICE*",
                    "AUTHORS*", ".gitignore", ".gitattributes", ".editorconfig", ".github/",
                    "mise.toml", ".mise.toml", ".tool-versions")


def is_blank_tree(paths: list[str]) -> bool:
    """A tree with no build tooling or code yet: every file matches ``BLANK_REPO_FILES``
    (documentation anywhere – fnmatch's ``*`` crosses ``/`` – the rest at the top level)."""
    return all(any(path_matches(p, pat) for pat in BLANK_REPO_FILES) for p in paths)


class GitRemote:
    """Bare hub repository + controller-private integration worktree."""

    def __init__(self, root: Path, repo_name: str, source: str | None, base_branch: str,
                 max_push_bytes: int = 256 * 1024 * 1024):
        self.root = Path(root)
        self.max_push_bytes = max_push_bytes
        self.repo_name = repo_name
        self.project_root = self.root / "served"           # GIT_PROJECT_ROOT for http-backend
        self.repo = self.project_root / f"{repo_name}.git"  # bare hub
        self.worktrees = self.root / "worktrees"   # one integration worktree per change
        self.source = source
        self.base_branch = base_branch
        self.lock = asyncio.Lock()
        # one sync of this hub with its source at a time; a sync that waited for another
        # reuses its result (``_synced``: how many have finished, ``_base``: the last base)
        self._sync_lock = asyncio.Lock()
        self._synced, self._base = 0, ""
        self._configured = False          # ensure() has configured the hub in this process

    async def git(self, *args: str, check: bool = True, cwd: Path | None = None) -> str:
        return (await run("git", *args, cwd=cwd or self.repo, check=check))[1]

    async def ensure(self) -> None:
        """Create the hub if there is none (cloned from its source, or blank), and configure it
        – once per process."""
        if self._configured and self.exists:
            return
        if not (self.repo / "HEAD").exists():
            self.repo.parent.mkdir(parents=True, exist_ok=True)
            if self.source is None:
                await self._init_blank()
            else:
                await run("git", "clone", "--bare", "--quiet", self.source, str(self.repo))
                await self.git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        # Pushes come from untrusted agents: verify every received object, cap the pack size,
        # never delete refs, and serve only objects reachable from advertised refs.
        for k, v in (("http.receivepack", "true"), ("http.uploadpack", "true"),
                     ("receive.denyDeletes", "true"), ("receive.denyNonFastForwards", "false"),
                     ("receive.fsckObjects", "true"),
                     ("receive.maxInputSize", str(self.max_push_bytes)),
                     ("uploadpack.allowAnySHA1InWant", "false"),
                     ("uploadpack.allowReachableSHA1InWant", "true"),
                     # partial clones for roles that only read (blobs fetched on demand)
                     ("uploadpack.allowFilter", "true"),
                     ("user.name", "legion-controller"),
                     ("user.email", "legion-controller@localhost")):
            await self.git("config", k, v)
        hook = self.repo / "hooks" / "pre-receive"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(PRE_RECEIVE)
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._configured = True

    async def _init_blank(self) -> None:
        """No source repository: a new bare hub whose base branch has one empty commit."""
        await run("git", "init", "--bare", "--quiet", "-b", self.base_branch, str(self.repo))
        tree = (await self.git("hash-object", "-w", "-t", "tree", "/dev/null")).strip()  # empty tree
        sha = (await self.git("-c", "user.name=legion-controller",
                              "-c", "user.email=legion-controller@localhost",
                              "commit-tree", tree, "-m", "legion: initial empty commit")).strip()
        await self.git("update-ref", f"refs/heads/{self.base_branch}", sha)

    @property
    def exists(self) -> bool:
        """The hub has been created (cloned, or a blank one initialised)."""
        return (self.repo / "HEAD").exists()

    async def sync(self) -> str:
        """Bring the hub up to date with its source and return the base branch's commit: clone
        it if there is no hub yet, otherwise fetch and fast-forward the base branch. ``legion/*``
        (changes' integration and run branches) is never touched. One sync of a hub runs at a
        time; one that waited for another takes that one's result. Raises
        :class:`WorkspaceUnsynced` when the hub cannot follow its source: the clone's origin is
        not the configured source, or the source's base branch was rewritten."""
        seen = self._synced
        async with self._sync_lock:
            if self._synced != seen and self._base:      # synced while this one waited
                return self._base
            base = await self._sync()
            self._synced, self._base = self._synced + 1, base
            return base

    async def _sync(self) -> str:
        await self.ensure()
        local = f"refs/heads/{self.base_branch}"
        if self.source is None:                  # a blank repository: its base lives here
            return await self.rev_parse(local)
        origin = (await self.git("config", "--get", "remote.origin.url", check=False)).strip()
        if origin != self.source:
            raise WorkspaceUnsynced(f"the hub's origin is {origin or '(none)'}, not the "
                                    f"configured source {self.source}")
        rc, out = await run("git", "fetch", "--quiet", "--prune", "origin", cwd=self.repo,
                            check=False)
        if rc != 0:
            raise WorkspaceUnsynced(f"fetching {self.source} failed: {out.strip()[-400:]}")
        remote = await self.ref_sha(f"refs/remotes/origin/{self.base_branch}")
        if remote is None:
            raise WorkspaceUnsynced(f"{self.source} has no branch {self.base_branch}")
        current = await self.ref_sha(local)
        if current is not None and current != remote and not await self.is_ancestor(current,
                                                                                 remote):
            raise WorkspaceUnsynced(
                f"{self.base_branch} on {self.source} was rewritten: the hub's {current[:12]} "
                f"is not an ancestor of {remote[:12]} – reset or re-clone the hub "
                f"({self.repo}) by hand, then retry")
        if current != remote:                    # a fast-forward (or the first time)
            await self.git("update-ref", local, remote, *([current] if current else []))
        return remote

    async def resolve_base(self) -> str:
        if self.source is None:          # a blank repository: the base branch lives in the hub
            return await self.rev_parse(f"refs/heads/{self.base_branch}")
        if "://" in self.source or Path(self.source).exists():
            await self.git("fetch", "--quiet", "origin")
        return await self.rev_parse(f"refs/remotes/origin/{self.base_branch}")

    async def has_object(self, sha: str, path: str | None = None) -> bool:
        spec = f"{sha}:{path}" if path else f"{sha}^{{commit}}"
        rc, _ = await run("git", "cat-file", "-e", spec, cwd=self.repo, check=False)
        return rc == 0

    async def expand_commit(self, sha: str) -> str:
        """The full sha of the one commit ``sha`` abbreviates (4–40 hex digits). Raises
        :class:`ShaError` saying what to fix when no commit, or more than one, has it."""
        s = sha.strip().lower()
        if not 4 <= len(s) <= 40 or any(c not in "0123456789abcdef" for c in s):
            raise ShaError(f"{sha!r} is not a commit sha: give the full 40-character sha")
        if len(s) == 40:
            if await self.has_object(s):
                return s
            raise ShaError(f"no commit {s} in the workspace")
        rc, out = await run("git", "rev-parse", f"--disambiguate={s}", cwd=self.repo,
                            check=False)
        found = [x for x in out.split() if len(x) == 40] if rc == 0 else []
        commits = [x for x in found if await self.has_object(x)]
        if len(commits) == 1:
            return commits[0]
        if not commits:
            raise ShaError(f"no commit starting {s} in the workspace: give the full "
                           "40-character sha")
        raise ShaError(f"{s} is ambiguous – it starts {len(commits)} commits "
                       f"({', '.join(sorted(commits))}): give the full 40-character sha")

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

    async def numstat(self, base: str, head: str, *, since_fork: bool = False
                      ) -> list[tuple[str, int | None, int | None]]:
        """(path, lines added, lines deleted) per file changed in ``base..head`` (``since_fork``:
        since their merge base, ``base...head``); binary files have no line counts (None)."""
        out = await self.git("diff", "--numstat", "--no-renames", "-z",
                             f"{base}{'...' if since_fork else '..'}{head}")
        rows = []
        for rec in out.split("\0"):
            parts = rec.split("\t", 2)
            if len(parts) == 3:
                a, d, path = parts
                rows.append((path, int(a) if a.isdigit() else None, int(d) if d.isdigit() else None))
        return rows

    # ----------------------------------------------------------- read-only browsing (operator UI)
    async def browse_refs(self) -> list[tuple[str, str]]:
        """(branch, sha): the base branch, then each change's integration branch."""
        out = await self.git("for-each-ref", "--format=%(refname:short)%09%(objectname)",
                             "refs/heads/", check=False)
        refs = [tuple(line.split("\t", 1)) for line in out.splitlines() if "\t" in line]
        base = [r for r in refs if r[0] == self.base_branch]
        rest = sorted(r for r in refs if r[0] != self.base_branch and r[0].startswith("legion/"))
        return base + rest

    async def browse_commit(self, ref: str) -> str | None:
        """The commit sha of ``ref`` when it is a browsable branch (browse_refs) or a full sha
        the hub has; None otherwise (nothing else is resolved)."""
        branches = dict(await self.browse_refs())
        if ref in branches:
            return branches[ref]
        if len(ref) == 40 and all(c in "0123456789abcdef" for c in ref) \
                and await self.has_object(ref):
            return ref
        return None

    async def browse_tree(self, sha: str, limit: int = 20000) -> tuple[list[tuple[str, int | None]], bool]:
        """(path, size) of every file in the commit's tree (None: a submodule), and whether the
        listing was cut at ``limit``."""
        out = await self.git("ls-tree", "-r", "-l", "-z", sha)
        files = []
        for rec in out.split("\0"):
            if "\t" not in rec:
                continue
            meta, path = rec.split("\t", 1)
            parts = meta.split()
            size = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            files.append((path, size))
        return files[:limit], len(files) > limit

    async def browse_blob(self, sha: str, path: str, max_bytes: int) -> bytes | None:
        """A file's bytes at ``sha`` (None: no such file, or larger than ``max_bytes``)."""
        spec = f"{sha}:{path}"
        rc, size = await run("git", "cat-file", "-s", spec, cwd=self.repo, check=False)
        if rc != 0 or not size.strip().isdigit() or int(size) > max_bytes:
            return None
        proc = await asyncio.create_subprocess_exec(
            "git", "cat-file", "blob", spec, cwd=str(self.repo),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        data, _ = await asyncio.wait_for(proc.communicate(), 60)
        return data if proc.returncode == 0 else None

    async def tree_paths(self, sha: str) -> list[str]:
        """Every file path in the commit's tree."""
        out = await self.git("ls-tree", "-r", "--name-only", "-z", sha)
        return [p for p in out.split("\0") if p]

    async def blob(self, sha: str, path: str) -> str | None:
        rc, out = await run("git", "rev-parse", f"{sha}:{path}", cwd=self.repo, check=False)
        return out.strip() if rc == 0 else None

    async def run_branches(self, change_id: str) -> list[str]:
        """The change's run branches on the hub (``legion/<change>/…``, not its integration
        branch): what agents pushed."""
        out = await self.git("for-each-ref", "--format=%(refname:short)",
                             f"refs/heads/legion/{change_id}/", check=False)
        keep = self.integration_branch(change_id)
        return sorted(b for b in out.split() if b != keep)

    async def delete_ref(self, ref: str) -> bool:
        """Delete a ref on the hub (the controller only: pushes may never delete); False when
        there was none."""
        rc, _ = await run("git", "update-ref", "-d", ref, cwd=self.repo, check=False)
        return rc == 0

    async def pin(self, name: str, sha: str) -> None:
        await self.git("update-ref", f"refs/legion/evidence/{name}", sha)

    # ----------------------------------------------------------- integration
    @staticmethod
    def integration_branch(change_id: str) -> str:
        return f"legion/{change_id}/integration"

    def worktree(self, change_id: str) -> Path:
        return self.worktrees / change_id

    async def init_integration(self, change_id: str, base_sha: str) -> None:
        branch = self.integration_branch(change_id)
        wt = self.worktree(change_id)
        async with self.lock:
            if wt.exists():
                await self.git("worktree", "remove", "--force", str(wt), check=False)
                shutil.rmtree(wt, ignore_errors=True)
            await self.git("worktree", "prune")
            await self.git("branch", "-f", branch, base_sha)
            wt.parent.mkdir(parents=True, exist_ok=True)
            await self.git("worktree", "add", "--quiet", str(wt), branch)

    async def integration_head(self, change_id: str) -> str:
        return await self.rev_parse(f"refs/heads/{self.integration_branch(change_id)}")

    async def commit_file(self, change_id: str, path: str, content: str, message: str) -> str:
        """Write ``path`` in the change's integration worktree and commit it as the controller;
        the new integration head (unchanged when the file already had this content)."""
        wt = self.worktree(change_id)
        async with self.lock:
            f = wt / path
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
            await run("git", "add", "--", path, cwd=wt)
            rc, _ = await run("git", "diff", "--cached", "--quiet", cwd=wt, check=False)
            if rc != 0:
                await run("git", "commit", "--quiet", "-m", message, cwd=wt)
            return (await run("git", "rev-parse", "HEAD", cwd=wt))[1].strip()

    async def merge(self, change_id: str, sha: str, message: str) -> tuple[bool, str]:
        """Merge a candidate into the change's integration worktree. Returns (clean, head|detail)."""
        wt = self.worktree(change_id)
        async with self.lock:
            rc, out = await run("git", "merge", "--no-ff", "-m", message, sha, cwd=wt, check=False)
            if rc != 0:
                await run("git", "merge", "--abort", cwd=wt, check=False)
                return False, out
            head = (await run("git", "rev-parse", "HEAD", cwd=wt))[1].strip()
            return True, head
