"""The workspace's source on GitHub: pushing a branch to it, and its pull requests and labels,
through the ``gh`` CLI (the host's authentication; never a sandbox's). Every call goes through
one runner, so tests replace GitHub without a network."""
from __future__ import annotations

import json
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from .remote import run

#: runs a command (argv, working directory) and returns its exit code and output
Runner = Callable[[list[str], Path | None], Awaitable[tuple[int, str]]]

_GITHUB = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
                     r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    """A push, or a call to GitHub, failed: what it said."""


@dataclass
class PullRequest:
    number: int
    url: str
    state: str                                  # OPEN | MERGED | CLOSED
    labels: list[str] = field(default_factory=list)


async def _run(argv: list[str], cwd: Path | None) -> tuple[int, str]:
    return await run(*argv, cwd=cwd, check=False, timeout=120)


class GitHub:
    """Pushes and pull requests on GitHub (``gh``). ``run``: how commands run."""

    def __init__(self, run: Runner = _run, gh: str = "gh"):
        self._run, self._gh = run, gh

    def repo_of(self, source: str | None) -> str | None:
        """The ``owner/name`` a workspace source is on GitHub, or None (not a GitHub URL)."""
        m = _GITHUB.match((source or "").strip())
        return m.group("repo") if m else None

    async def _call(self, *args: str, cwd: Path | None = None) -> str:
        rc, out = await self._run([self._gh, *args], cwd)
        if rc != 0:
            raise GitHubError(f"gh {args[0]} {args[1] if len(args) > 1 else ''}: "
                              f"{out.strip()[-500:]}")
        return out

    async def push(self, hub: Path, source: str, sha: str, branch: str) -> None:
        """Push ``sha`` from the hub to ``branch`` on the source – never forced: a branch that
        moved elsewhere is refused."""
        rc, out = await self._run(["git", "push", "--porcelain", source,
                                   f"{sha}:refs/heads/{branch}"], hub)
        if rc != 0:
            raise GitHubError(f"pushing {branch} to {source} failed: {out.strip()[-500:]}")

    async def find_pr(self, repo: str, branch: str) -> PullRequest | None:
        """The open or merged pull request from ``branch`` (a closed one does not count)."""
        out = await self._call("pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                               "--json", "number,url,state,labels")
        prs = [PullRequest(p["number"], p["url"], p["state"],
                           [lb["name"] for lb in p.get("labels", [])])
               for p in json.loads(out or "[]")]
        return next((p for p in prs if p.state in ("OPEN", "MERGED")), None)

    async def create_pr(self, repo: str, base: str, branch: str, title: str,
                        body: str) -> PullRequest:
        """A new pull request, ready for review."""
        with _body_file(body) as f:
            await self._call("pr", "create", "--repo", repo, "--base", base, "--head", branch,
                             "--title", title, "--body-file", str(f))
        pr = await self.find_pr(repo, branch)
        if pr is None:
            raise GitHubError(f"the pull request from {branch} was created but is not found")
        return pr

    async def update_pr(self, repo: str, number: int, title: str, body: str) -> None:
        with _body_file(body) as f:
            await self._call("pr", "edit", str(number), "--repo", repo, "--title", title,
                             "--body-file", str(f))

    async def labels(self, repo: str) -> set[str]:
        out = await self._call("label", "list", "--repo", repo, "--limit", "1000",
                               "--json", "name")
        return {lb["name"] for lb in json.loads(out or "[]")}

    async def create_label(self, repo: str, name: str, color: str, description: str) -> None:
        await self._call("label", "create", name, "--repo", repo, "--color", color,
                         "--description", description)

    async def set_labels(self, repo: str, number: int, add: list[str],
                         remove: list[str]) -> None:
        if not add and not remove:
            return
        args = ["pr", "edit", str(number), "--repo", repo]
        for a in add:
            args += ["--add-label", a]
        for r in remove:
            args += ["--remove-label", r]
        await self._call(*args)


class _body_file:
    """A pull request's body as a temporary file (``gh --body-file``)."""

    def __init__(self, body: str):
        self.body = body

    def __enter__(self) -> Path:
        self._f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False)
        self._f.write(self.body)
        self._f.close()
        return Path(self._f.name)

    def __exit__(self, *exc) -> None:
        Path(self._f.name).unlink(missing_ok=True)
