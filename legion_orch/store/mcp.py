"""MCP tools for the document store.

The tools are registered on any :class:`MCPServer` with :func:`register_store_tools`. The host
supplies ``session(ctx) -> StoreSession`` to authenticate the caller and decide how paths resolve,
what is readable and how (and whether) a write is allowed. The store itself always enforces
immutability, optimistic concurrency and the path-bound schema (G-W2, G-W3).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

from .docstore import DocStore, DocStoreError, DocVersion
from .schemas import SchemaError

STORE_TOOLS = ("docs_list", "docs_read", "docs_history", "docs_write", "docs_schema", "schema_get")


@dataclass
class StoreSession:
    """The store as seen by one authenticated caller.

    ``session(ctx)`` and ``write`` should raise :class:`ToolError` (or :class:`DocStoreError`) for
    refusals: any other exception is treated by the MCP SDK as a crash and its message is hidden
    from the caller."""

    docs: DocStore
    resolve: Callable[[str], str]                    # caller path -> store path (namespacing)
    readable: Callable[[str], bool]                  # store path -> may the caller read it?
    write: Callable[[str, str, int | None], Awaitable[DocVersion]]  # store path, content, base


class ToolRegistrar(Protocol):
    """Where tools are registered: an ``MCPServer`` or anything exposing its ``tool`` decorator."""

    def tool(self, *, description: str) -> Callable[[Callable], Callable]: ...


def register_store_tools(server: ToolRegistrar, session: Callable[[Context], StoreSession]) -> None:
    def readable_path(s: StoreSession, path: str) -> str:
        full = s.resolve(path)
        if not s.readable(full):
            raise ToolError(f"{full} is not readable by this caller")
        return full

    @server.tool(description="List documents (latest version of each) under a path prefix. "
                             "Relative prefixes resolve inside your namespace.")
    async def docs_list(ctx: Context, prefix: str = "") -> list[dict]:
        s = session(ctx)
        return [d.to_json() for d in s.docs.list(s.resolve(prefix)) if s.readable(d.path)]

    @server.tool(description="Read a document version (latest if version omitted).")
    async def docs_read(ctx: Context, path: str, version: int | None = None) -> dict:
        s = session(ctx)
        try:
            d = s.docs.read(readable_path(s, path), version)
        except DocStoreError as e:
            raise ToolError(str(e)) from None
        return d.to_json()

    @server.tool(description="All versions of a document with author, role, sha256 and schema.")
    async def docs_history(ctx: Context, path: str) -> list[dict]:
        s = session(ctx)
        return [m.to_json() for m in s.docs.history(readable_path(s, path))]

    @server.tool(description="Write a new immutable version of a document. base_version must equal "
                             "the current latest version (omit for a new document). The content "
                             "must match the schema bound to the path (see docs_schema). Returns "
                             "the attestation file:<path>@<version> to cite.")
    async def docs_write(ctx: Context, path: str, content: str, base_version: int | None = None) -> dict:
        s = session(ctx)
        try:
            v = await s.write(s.resolve(path), content, base_version)
        except DocStoreError as e:
            raise ToolError(str(e)) from None
        return v.meta().to_json()

    @server.tool(description="The schema a document path is bound to (name, format, definition). "
                             "Every write is validated against it; check before writing.")
    async def docs_schema(ctx: Context, path: str) -> dict:
        s = session(ctx)
        full = s.resolve(path)
        try:
            sch = s.docs.schemas.get(s.docs.schemas.bound(full))
        except SchemaError as e:
            raise ToolError(str(e)) from None
        return {"path": full, "schema": sch.name, "format": sch.fmt, "sha": sch.sha,
                "definition": sch.body}

    @server.tool(description="A schema by name, e.g. a report schema.")
    async def schema_get(ctx: Context, name: str) -> dict:
        s = session(ctx)
        try:
            sch = s.docs.schemas.get(name)
        except SchemaError as e:
            raise ToolError(str(e)) from None
        return {"schema": sch.name, "format": sch.fmt, "sha": sch.sha, "definition": sch.body}
