"""Attestation references.

* ``file:<path>@<version>``  – a document version in the immutable store
* ``git:<repo>/<path>@<sha>`` – a file at a commit
* ``git:<repo>@<sha>``        – a whole commit
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..git import GitRemote, ShaError
from ..store import DocStore
from .tickets.plan_doc import expand_shas

# Attestations are shown to humans and stored in records: restrict them to a safe character set
# (the store's path alphabet), so agent-chosen file names cannot smuggle markup or script.
_SAFE = r"[A-Za-z0-9._+=:,~/-]+"
FILE_RE = re.compile(rf"^file:(?P<path>{_SAFE})@(?P<version>[1-9][0-9]*)$")
GIT_RE = re.compile(rf"^git:(?P<body>{_SAFE})@(?P<sha>[0-9a-f]{{40}})$")
# a git reference as written, its sha perhaps abbreviated (or in capitals): expanded before use
LOOSE_GIT_RE = re.compile(rf"^git:(?P<body>{_SAFE})@(?P<sha>[0-9a-fA-F]{{4,40}})$")


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
        f"malformed attestation {ref!r}; expected file:<path>@<version> or "
        "git:<repo>[/<path>]@<sha> (the full 40-character sha)")


async def expand(ref: str, mirror: GitRemote) -> str:
    """``ref`` with its git sha in full: an abbreviated one is expanded when exactly one commit
    of the workspace has it. Raises :class:`AttestationError` (no such commit, or several:
    what to give instead). Anything but a git reference is returned as it is."""
    m = LOOSE_GIT_RE.match(ref.strip())
    if m is None:
        return ref
    if GIT_RE.match(ref.strip()):
        return ref.strip()
    try:
        sha = await mirror.expand_commit(m["sha"])
    except ShaError as e:
        raise AttestationError(f"{ref.strip()}: {e}") from None
    return f"git:{m['body']}@{sha}"


async def expand_all(refs: list[str], mirror: GitRemote) -> tuple[list[str], list[str]]:
    """Every reference with its git sha in full (:func:`expand`), and what could not be."""
    out, errors = [], []
    for ref in refs:
        try:
            out.append(await expand(ref, mirror) if isinstance(ref, str) else ref)
        except AttestationError as e:
            errors.append(str(e))
            out.append(ref)
    return out, errors


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
            att = parse(await expand(ref, mirror), repo_names)   # an abbreviated sha, in full
        except AttestationError as e:
            violations.append(f"G-R3: {e}")
            continue
        if not await resolve(att, docs, mirror):
            violations.append(f"G-R3: attestation does not resolve: {ref}")
            continue
        parsed.append(att)
    return parsed, violations


async def plan_shas(data, mirror: GitRemote):
    """A plan document (a replan) with its ``work.context`` shas in full, and what could not be
    expanded: a commit no workspace commit starts with, or an ambiguous abbreviation."""
    return await expand_shas(data, lambda ref: expand(ref, mirror))
