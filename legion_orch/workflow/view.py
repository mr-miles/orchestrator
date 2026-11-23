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
from .model import Change, Run
from .tickets import Tickets


class ChangeView(Protocol):
    cid: str
    cfg: Config
    docs: DocStore
    git: GitRemote                   # the change's workspace's hub
    workspace: str                   # the change's workspace: attestations' git:<workspace>@…

    @property
    def change(self) -> Change: ...

    @property
    def tickets(self) -> Tickets:
        """The change's tickets (the root's pointers: the approved outputs)."""

    @property
    def runs(self) -> dict[str, Run]: ...

    def a2a_cards(self) -> list[Any]:
        """The change's A2A cards (workflow.model.Card), as the engine keeps them."""

    def p(self, rel: str) -> str:
        """Qualify a path into the change namespace (changes/<id>/...)."""

    def doc(self, rel: str, version: int | None = None) -> DocVersion: ...

    def latest(self, rel: str) -> int | None: ...

    def latest_att(self, rel: str) -> str | None: ...

    def json_doc(self, rel: str, version: int | None = None) -> Any: ...

    def write_record(self, rel: str, content: str | dict | BaseModel) -> DocVersion:
        """Write a controller-owned document (author 'controller') as a new version."""



class RoleContext(ChangeView, Protocol):
    """What a role handler (workflow/roles/) may use: the change view plus the engine operations
    a role's transition needs (``ChangeEngine`` implements it by delegating to its parts).
    Handlers never touch engine internals."""

    assets: Any        # workflow.assets.Assets (roles, schemas)

    def event(self, kind: str, message: str, ticket: str | None = None,
              run: str | None = None) -> None: ...

    def stored_log(self, run_id: str) -> str:
        """A run's whole archived agent log."""

    def timeline(self) -> str:
        """The change's controller events so far (JSON lines)."""

