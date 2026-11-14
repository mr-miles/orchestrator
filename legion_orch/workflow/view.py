"""The narrow view of one change that governance and ship evaluation work against.

``ChangeEngine`` implements it; governance.py and ship.py depend only on this Protocol, so they
can be read, reasoned about and tested without the engine's dispatch, sandbox or scheduling code.
"""
from __future__ import annotations

from typing import Any, Protocol

from ..config import Config
from ..git import GitRemote
from ..store import DocStore, DocVersion
from .model import Change, Node, Run


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

    def write_record(self, rel: str, content: str | dict) -> DocVersion:
        """Write a controller-owned document (author 'controller') as a new version."""

    def build_nodes(self) -> list[Node]: ...
