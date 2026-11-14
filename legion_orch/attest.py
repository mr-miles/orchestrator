"""Attestation references.

* ``file:<path>@<version>``  – a document version in the immutable store
* ``git:<repo>/<path>@<sha>`` – a file at a commit
* ``git:<repo>@<sha>``        – a whole commit
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .git import GitRemote
from .store import DocStore

FILE_RE = re.compile(r"^file:(?P<path>[^@\s]+)@(?P<version>[1-9][0-9]*)$")
GIT_RE = re.compile(r"^git:(?P<body>[^@\s]+)@(?P<sha>[0-9a-f]{40})$")


class AttestationError(ValueError):
    pass


@dataclass(frozen=True)
class Attestation:
    kind: str  # file | git
    path: str | None
    version: int | None = None
    repo: str | None = None
    sha: str | None = None

    def __str__(self) -> str:
        if self.kind == "file":
            return f"file:{self.path}@{self.version}"
        body = self.repo if self.path is None else f"{self.repo}/{self.path}"
        return f"git:{body}@{self.sha}"


def qualify(path: str, cid: str) -> str:
    """Resolve a document path inside a change namespace. ``changes/...`` paths are absolute."""
    path = path.strip().lstrip("/")
    return path if path.startswith("changes/") else f"changes/{cid}/{path}"


def normalize(ref: str, cid: str) -> str:
    """``file:plan/spec.md@2`` -> ``file:changes/<cid>/plan/spec.md@2``; git refs unchanged."""
    m = FILE_RE.match(ref.strip())
    if not m:
        return ref.strip()
    return f"file:{qualify(m['path'], cid)}@{m['version']}"


def parse(ref: str, repo_names: list[str]) -> Attestation:
    ref = ref.strip()
    m = FILE_RE.match(ref)
    if m:
        return Attestation("file", m["path"], version=int(m["version"]))
    m = GIT_RE.match(ref)
    if m:
        body = m["body"]
        # Longest configured repo name that prefixes the body wins (repo names may contain '/').
        for name in sorted(repo_names, key=len, reverse=True):
            if body == name:
                return Attestation("git", None, repo=name, sha=m["sha"])
            if body.startswith(name + "/"):
                return Attestation("git", body[len(name) + 1:], repo=name, sha=m["sha"])
        raise AttestationError(f"unknown repository in {ref!r} (known: {repo_names})")
    raise AttestationError(
        f"malformed attestation {ref!r}; expected file:<path>@<version> or git:<repo>[/<path>]@<40-hex sha>")


async def resolve(att: Attestation, docs: DocStore, mirror: GitRemote) -> bool:
    if att.kind == "file":
        return docs.exists(att.path, att.version)
    return await mirror.has_object(att.sha, att.path)


async def check_all(refs: list[str], repo_names: list[str], docs: DocStore,
                    mirror: GitRemote) -> tuple[list[Attestation], list[str]]:
    """Guard G-R3. Returns (parsed, violations)."""
    parsed, violations = [], []
    for ref in refs:
        try:
            att = parse(ref, repo_names)
        except AttestationError as e:
            violations.append(f"G-R3: {e}")
            continue
        if not await resolve(att, docs, mirror):
            violations.append(f"G-R3: attestation does not resolve: {ref}")
            continue
        parsed.append(att)
    return parsed, violations
