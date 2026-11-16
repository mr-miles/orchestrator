"""The narrow view of one change that governance and ship evaluation work against.

``engine.ChangeCore`` implements it; governance/ and ship.py depend only on this Protocol, so
they can be read, reasoned about and tested without the engine's dispatch, sandbox or scheduling
code.
"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from ..config import Config
from ..git import GitRemote
from ..store import DocStore, DocVersion
from .model import Change, Node, Resume, Run


class ChangeView(Protocol):
    cid: str
    cfg: Config
    docs: DocStore
    git: GitRemote

    @property
    def change(self) -> Change: ...

    @property
    def nodes(self) -> dict[str, Node]: ...

    @property
    def runs(self) -> dict[str, Run]: ...

    def p(self, rel: str) -> str:
        """Qualify a path into the change namespace (changes/<id>/...)."""

    def doc(self, rel: str, version: int | None = None) -> DocVersion: ...

    def latest(self, rel: str) -> int | None: ...

    def latest_att(self, rel: str) -> str | None: ...

    def json_doc(self, rel: str, version: int | None = None) -> Any: ...

    def write_record(self, rel: str, content: str | dict | BaseModel) -> DocVersion:
        """Write a controller-owned document (author 'controller') as a new version."""

    def build_nodes(self) -> list[Node]: ...


class RoleContext(ChangeView, Protocol):
    """What a role handler (workflow/roles/) may use: the change view plus the engine operations
    a role's transition needs (``ChangeEngine`` implements it by delegating to its parts).
    Handlers never touch engine internals."""

    assets: Any        # workflow.assets.Assets (roles, schemas)

    def event(self, kind: str, message: str, node: str | None = None,
              run: str | None = None) -> None: ...

    def escalate(self, node: Node, kind: str, reason: str, resume: Resume, *,
                 run: str | None = None, missing_decision: str | None = None,
                 log_tail: str | None = None) -> None: ...

    def accept_runs(self, node: Node, roles: set[str] | None = None) -> None:
        """Mark the node's reported runs accepted and delete their sandboxes."""

    def delete_sandbox_later(self, name: str) -> None: ...

    def start_verification(self, node_id: str) -> None: ...

    def materialize_builds(self, taskgraph: dict) -> None:
        """Create the build nodes from an accepted task graph."""

    def panel_complete(self, node: Node) -> None:
        """Decide the review panel outcome once every member has reported."""

    def oracle_commands(self) -> list[str]: ...

    def environment_library(self) -> Any:
        """workflow.environment.Library: the tool and network-policy library."""
