"""The repository's setup (WORKFLOW.md §5.3c): ``mise.toml``'s ``[tools]`` are installed when the
environment's images are built, and its ``setup`` task runs after every checkout – in agent and
verifier sandboxes alike – as ``mise run setup``, from the ``mise.toml`` of the commit checked
out. That file is protected (``roles.base.guarded``) and the hub refuses a push that changes it
without approval (G-B3 at push), so whatever a sandbox finds there was approved. No ``setup``
task: no setup.
"""
from __future__ import annotations

import hashlib
import tomllib
from typing import Any

from ..git import GitRemote

MISE_TOML = "mise.toml"
SETUP_COMMAND = "mise run setup"      # what the results show for the setup step
MAX_BYTES = 256 * 1024


class MiseError(ValueError):
    """``mise.toml`` does not parse."""


def parse(text: str) -> dict[str, Any]:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise MiseError(f"{MISE_TOML} does not parse: {e}") from None


def has_setup(text: str) -> bool:
    """The file defines a ``setup`` task (``[tasks.setup]``)."""
    tasks = parse(text).get("tasks")
    return isinstance(tasks, dict) and "setup" in tasks


def tools(text: str) -> dict[str, str]:
    """``[tools]``: name -> version (a table entry's ``version``; anything else as text)."""
    raw = parse(text).get("tools") or {}
    out = {}
    for name, v in raw.items() if isinstance(raw, dict) else ():
        if isinstance(v, dict):
            v = v.get("version", "")
        elif isinstance(v, list):
            v = v[0] if v else ""
        out[str(name)] = str(v)
    return out


def tools_at(text: str | None) -> dict[str, str]:
    """``[tools]`` of a mise.toml that may be missing or unreadable: none then."""
    if text is None:
        return {}
    try:
        return tools(text)
    except MiseError:
        return {}


def blob_sha(text: str) -> str:
    """The git blob sha of ``text`` (what ``git rev-parse HEAD:mise.toml`` prints)."""
    data = text.encode()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324 - git's id


async def read_at(git: GitRemote, sha: str) -> str | None:
    """``mise.toml`` at ``sha`` (None: there is none, or it is not text)."""
    data = await git.browse_blob(sha, MISE_TOML, MAX_BYTES)
    if data is None:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


async def blob_at(git: GitRemote, sha: str) -> str | None:
    """``mise.toml``'s blob at ``sha`` (None: there is none) – recorded, for information."""
    return await git.blob(sha, MISE_TOML)


async def runs_setup_at(git: GitRemote, sha: str) -> bool:
    """Whether a sandbox checked out at ``sha`` runs the setup: its ``mise.toml`` has a
    ``setup`` task. One that does not parse runs it too, so the failure is seen, not skipped."""
    text = await read_at(git, sha)
    if text is None:
        return False
    try:
        return has_setup(text)
    except MiseError:
        return True
