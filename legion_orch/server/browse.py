"""Read-only browsing for the operator UI: workspaces (a repository and its document store), the
repository's files at a branch, document downloads, and the configuration's definition files
(skills, agents, workflows). Presentation only: nothing here writes."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from starlette.responses import Response

from ..workflow.engine import Controller

MAX_VIEW_BYTES = 2 * 1024 * 1024        # larger files are listed, and downloadable only
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


class BrowseError(Exception):
    """A request for something that does not exist or may not be browsed (404)."""


# ---------------------------------------------------------------------------- workspaces
def workspace_id(ctl: Controller) -> str:
    """A controller serves one workspace: its repository and document store."""
    return ctl.cfg.repo.name


def workspaces(ctl: Controller) -> list[dict]:
    """Every workspace this controller serves (today: one) with its changes counted by status."""
    counts: dict[str, int] = {}
    for ce in ctl.changes.values():
        if ce.change:
            counts[str(ce.change.status)] = counts.get(str(ce.change.status), 0) + 1
    cfg = ctl.cfg
    return [{"id": workspace_id(ctl), "name": cfg.project, "repo": cfg.repo.name,
             "base_branch": cfg.repo.base_branch, "source": cfg.repo.path or None,
             "changes": counts, "documents": len(ctl.docs.list(""))}]


def check_workspace(ctl: Controller, ws: str) -> None:
    if ws != workspace_id(ctl):
        raise BrowseError(f"unknown workspace {ws!r}")


# ---------------------------------------------------------------------------- files
def _is_text(data: bytes) -> bool:
    if b"\0" in data[:8192]:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def file_view(path: str, data: bytes | None, size: int | None) -> dict:
    """A file for the viewer: its text, or why it is not shown (binary, too large)."""
    if data is None:
        return {"path": path, "size": size, "text": None, "reason": "too large to show"}
    if not _is_text(data):
        return {"path": path, "size": len(data), "text": None, "reason": "binary file"}
    return {"path": path, "size": len(data), "text": data.decode("utf-8"), "reason": None}


def download(path: str, data: bytes) -> Response:
    """An attachment: text as plain text, anything else as bytes – never a type a browser renders
    (the repository and documents are agent-written)."""
    name = Path(path).name or "file"
    kind = "text/plain; charset=utf-8" if _is_text(data) else "application/octet-stream"
    return Response(data, media_type=kind, headers={
        "Content-Disposition": f"attachment; filename=\"{name.replace(chr(34), '')}\"",
        "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------- the repository
async def git_refs(ctl: Controller) -> list[dict]:
    return [{"name": n, "sha": sha, "base": n == ctl.cfg.repo.base_branch}
            for n, sha in await ctl.git.browse_refs()]


async def _commit(ctl: Controller, ref: str) -> str:
    sha = await ctl.git.browse_commit(ref or ctl.cfg.repo.base_branch)
    if sha is None:
        raise BrowseError(f"unknown branch or commit {ref!r}")
    return sha


async def git_tree(ctl: Controller, ref: str) -> dict:
    sha = await _commit(ctl, ref)
    files, truncated = await ctl.git.browse_tree(sha)
    return {"ref": ref or ctl.cfg.repo.base_branch, "sha": sha, "truncated": truncated,
            "files": [{"path": p, "size": s} for p, s in files]}


async def git_file(ctl: Controller, ref: str, path: str, *, raw: bool) -> dict | Response:
    sha = await _commit(ctl, ref)
    if not path or path.startswith("/") or "\0" in path:
        raise BrowseError("a repository path is required")
    data = await ctl.git.browse_blob(sha, path, MAX_DOWNLOAD_BYTES if raw else MAX_VIEW_BYTES)
    if raw:
        if data is None:
            raise BrowseError(f"no file {path!r} at {sha[:12]} (or larger than 64 MiB)")
        return download(path, data)
    if data is None and await ctl.git.blob(sha, path) is None:
        raise BrowseError(f"no file {path!r} at {sha[:12]}")
    return file_view(path, data, None) | {"sha": sha}


# ---------------------------------------------------------------------------- documents
def doc_download(ctl: Controller, path: str, version: int | None) -> Response:
    d = ctl.docs.read(path, version)
    return download(f"{Path(path).stem}-v{d.version}{Path(path).suffix}", d.content.encode())


# ---------------------------------------------------------------------------- configuration
@dataclass(frozen=True)
class Source:
    """A folder (or single file) of definitions, shown under ``prefix``."""
    prefix: str
    path: Path


def _sources(ctl: Controller, group: str) -> list[Source]:
    assets, cfg = ctl.cfg.assets_dir, ctl.cfg
    groups = {
        "skills": [Source("skills", assets / "skills")],
        "agents": [Source("roles.yaml", assets / "roles.yaml"), Source("souls", assets / "souls"),
                   Source("prompts", assets / "prompts")],
        "workflows": [Source("WORKFLOW.md", cfg.workflow_doc),
                      Source("schemas", assets / "schemas")],
    }
    if group not in groups:
        raise BrowseError(f"unknown configuration group {group!r}; one of {sorted(groups)}")
    return groups[group]


def _files(src: Source) -> list[tuple[str, Path]]:
    if src.path.is_file():
        return [(src.prefix, src.path)]
    if not src.path.is_dir():
        return []
    root = src.path.resolve()
    return [(f"{src.prefix}/{f.relative_to(root).as_posix()}", f)
            for f in sorted(root.rglob("*")) if f.is_file() and not f.is_symlink()
            and not any(part.startswith(".") or part == "__pycache__"
                        for part in f.relative_to(root).parts)]


def config_files(ctl: Controller, group: str) -> dict:
    """The group's definition files as the controller reads them (its assets folder)."""
    files = [(rel, f) for src in _sources(ctl, group) for rel, f in _files(src)]
    return {"group": group, "root": str(ctl.cfg.assets_dir), "assets_sha": ctl.assets.sha,
            "files": [{"path": rel, "size": f.stat().st_size} for rel, f in files]}


def config_file(ctl: Controller, group: str, path: str, *, raw: bool) -> dict | Response:
    found = dict((rel, f) for src in _sources(ctl, group) for rel, f in _files(src))
    f = found.get(path)
    if f is None:           # only listed files: nothing outside the group's folders
        raise BrowseError(f"no {group} file {path!r}")
    size = f.stat().st_size
    if raw:
        return download(path, f.read_bytes())
    return file_view(path, f.read_bytes() if size <= MAX_VIEW_BYTES else None, size)
