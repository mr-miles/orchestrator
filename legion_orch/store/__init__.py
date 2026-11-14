"""Immutable, versioned, schema-validated document store and its MCP tools.

Self-contained: nothing here imports the orchestrator. The orchestrator plugs in a per-caller
:class:`~legion_orch.store.mcp.StoreSession` (namespace, read filter, write policy).
"""
from .docstore import (
    DocMeta,
    DocStore,
    DocStoreError,
    DocVersion,
    SchemaViolation,
    VersionConflict,
    now_iso,
)
from .schemas import Schema, SchemaError, SchemaRegistry

__all__ = ["DocMeta", "DocStore", "DocStoreError", "DocVersion", "SchemaViolation", "VersionConflict",
           "now_iso", "Schema", "SchemaError", "SchemaRegistry"]
