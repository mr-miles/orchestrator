"""Immutable, versioned document store backed by SQLite.

Documents are append-only: a write creates version N+1 of a path and nothing is ever updated or
deleted (enforced by SQLite triggers as well as by the API). Every version is addressable as
``file:<path>@<version>``.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime

from .schemas import SchemaError, SchemaRegistry

PATH_RE = re.compile(r"^(?!/)(?!.*//)(?!.*(?:^|/)\.\.?(?:/|$))"
                     r"[A-Za-z0-9._@+=:,~-]+(?:/[A-Za-z0-9._@+=:,~-]+)*$")
MAX_DOC_BYTES = 2 * 1024 * 1024
META_COLS = "path, version, sha256, author, role, created_at, schema, schema_sha"
COLS = META_COLS + ", content"
DMETA_COLS = ", ".join("d." + c.strip() for c in META_COLS.split(","))

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  path TEXT NOT NULL,
  version INTEGER NOT NULL,
  content TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  author TEXT NOT NULL,          -- run id, 'controller' or 'human:<id>'
  role TEXT NOT NULL,
  created_at TEXT NOT NULL,
  schema TEXT NOT NULL,          -- schema name bound to the path at write time
  schema_sha TEXT NOT NULL,      -- sha256 of the schema file used to validate
  PRIMARY KEY (path, version)
);
CREATE TRIGGER IF NOT EXISTS documents_no_update BEFORE UPDATE ON documents
BEGIN SELECT RAISE(ABORT, 'documents are immutable'); END;
CREATE TRIGGER IF NOT EXISTS documents_no_delete BEFORE DELETE ON documents
BEGIN SELECT RAISE(ABORT, 'documents are immutable'); END;
"""


class DocStoreError(Exception):
    pass


class VersionConflict(DocStoreError):
    pass


class SchemaViolation(DocStoreError):
    pass


@dataclass(frozen=True)
class DocMeta:
    """One immutable document version, without its content."""
    path: str
    version: int
    sha256: str
    author: str
    role: str
    created_at: str
    schema: str
    schema_sha: str

    @property
    def attestation(self) -> str:
        return f"file:{self.path}@{self.version}"

    def to_json(self) -> dict:
        return asdict(self) | {"attestation": self.attestation}


@dataclass(frozen=True)
class DocVersion(DocMeta):
    """One immutable document version with its content."""
    content: str

    def meta(self) -> DocMeta:
        return DocMeta(**{f.name: getattr(self, f.name) for f in fields(DocMeta)})


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class DocStore:
    """Every write is validated against the schema bound to its path (guard G-W3); the schema
    name and sha are stored with the version."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock, schemas: SchemaRegistry):
        self.conn = conn
        self.lock = lock
        self.schemas = schemas
        with self.lock:
            self.conn.executescript(SCHEMA)
            cols = {r[1] for r in self.conn.execute("PRAGMA table_info(documents)")}
            for col in ("schema", "schema_sha"):   # stores created before schemas existed
                if col not in cols:
                    self.conn.execute(f"ALTER TABLE documents ADD COLUMN {col} TEXT NOT NULL "
                                      f"DEFAULT '{'legacy' if col == 'schema' else ''}'")
            self.conn.commit()

    def write(self, path: str, content: str, *, author: str, role: str,
              base_version: int | None = None, schema: str | None = None) -> DocVersion:
        """Create a new version. ``base_version`` must equal the current latest version
        (``None`` only for a new path) – optimistic concurrency (guard G-W2). The content must
        match the schema bound to ``path``; ``schema``, if given, must name that schema (G-W3)."""
        if not PATH_RE.match(path) or len(path) > 512:
            raise DocStoreError(f"invalid document path {path!r}")
        data = content.encode()
        if len(data) > MAX_DOC_BYTES:
            raise DocStoreError("document too large")
        try:
            sch = self.schemas.check(path, content, schema)
        except SchemaError as e:
            raise SchemaViolation(str(e)) from None
        with self.lock:
            latest = self._latest_version(path)
            if latest != base_version:
                raise VersionConflict(
                    f"G-W2: base_version {base_version} != latest {latest} for {path}")
            v = DocVersion(path=path, version=(latest or 0) + 1, content=content,
                           sha256="sha256:" + hashlib.sha256(data).hexdigest(),
                           author=author, role=role, created_at=now_iso(),
                           schema=sch.name, schema_sha=sch.sha)
            self.conn.execute(
                "INSERT INTO documents (path, version, content, sha256, author, role, created_at, "
                "schema, schema_sha) VALUES (?,?,?,?,?,?,?,?,?)",
                (v.path, v.version, v.content, v.sha256, v.author, v.role, v.created_at,
                 v.schema, v.schema_sha))
            self.conn.commit()
            return v

    def read(self, path: str, version: int | None = None) -> DocVersion:
        with self.lock:
            if version is None:
                row = self.conn.execute(
                    f"SELECT {COLS} FROM documents WHERE path=? ORDER BY version DESC LIMIT 1",
                    (path,)).fetchone()
            else:
                row = self.conn.execute(
                    f"SELECT {COLS} FROM documents WHERE path=? AND version=?", (path, version)).fetchone()
        if row is None:
            raise DocStoreError(f"no such document {path}@{version or 'latest'}")
        return DocVersion(*row)

    def exists(self, path: str, version: int) -> bool:
        with self.lock:
            return self.conn.execute(
                "SELECT 1 FROM documents WHERE path=? AND version=?", (path, version)
            ).fetchone() is not None

    def latest_version(self, path: str) -> int | None:
        with self.lock:
            return self._latest_version(path)

    def _latest_version(self, path: str) -> int | None:
        row = self.conn.execute("SELECT MAX(version) FROM documents WHERE path=?", (path,)).fetchone()
        return row[0]

    def history(self, path: str) -> list[DocMeta]:
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {META_COLS} FROM documents WHERE path=? ORDER BY version", (path,)).fetchall()
        return [DocMeta(*r) for r in rows]

    def list(self, prefix: str = "") -> list[DocMeta]:
        """The latest version of every document under ``prefix``."""
        with self.lock:
            rows = self.conn.execute(
                f"SELECT {DMETA_COLS} FROM documents d JOIN (SELECT path, MAX(version) v FROM documents "
                "WHERE path LIKE ? ESCAPE '\\' GROUP BY path) m ON d.path=m.path AND d.version=m.v "
                "ORDER BY d.path",
                (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
            ).fetchall()
        return [DocMeta(*r) for r in rows]
