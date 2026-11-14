"""Document schemas and path bindings (``assets/schemas/``).

* ``bindings.yaml`` – ordered list of ``{pattern, schema}``; patterns are relative to a change
  (``changes/<id>/`` is stripped first) and use ``fnmatch`` syntax. First match wins; the last
  binding should be a catch-all so every document has a schema.
* ``<name>.json`` – either a JSON Schema (draft 2020-12) for JSON documents, or a rule document
  with ``"x-format": "markdown"`` (``required_headings``, ``required_patterns``) or
  ``"x-format": "text"`` for free text.

The document store calls :meth:`SchemaRegistry.check` on every write and records the schema name
and sha with the version.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
CHANGE_PREFIX = re.compile(r"^changes/[^/]+/")
MAX_ERRORS = 8


class SchemaError(Exception):
    """Invalid schema definitions (raised at load) or invalid content (raised at write)."""


@dataclass
class Schema:
    name: str
    body: dict
    sha: str
    fmt: str  # json | markdown | text
    validator: Draft202012Validator | None = None


class SchemaRegistry:
    def __init__(self, root: Path):
        self.root = Path(root)
        if not (self.root / "bindings.yaml").is_file():
            raise SchemaError(f"{self.root}/bindings.yaml not found")
        self.schemas: dict[str, Schema] = {}
        for p in sorted(self.root.glob("*.json")):
            if not NAME_RE.match(p.stem):
                raise SchemaError(f"{p}: schema files must be named <lowercase-name>.json")
            raw = p.read_bytes()
            try:
                body = json.loads(raw)
            except json.JSONDecodeError as e:
                raise SchemaError(f"{p}: not valid JSON: {e}") from None
            fmt = body.get("x-format", "json")
            if fmt not in ("json", "markdown", "text"):
                raise SchemaError(f"{p}: x-format must be json, markdown or text")
            validator = None
            if fmt == "json":
                Draft202012Validator.check_schema(body)
                validator = Draft202012Validator(body)
            else:
                for rx in body.get("required_patterns", []):
                    re.compile(rx)
            self.schemas[p.stem] = Schema(p.stem, body, "sha256:" + hashlib.sha256(raw).hexdigest(),
                                          fmt, validator)
        self.bindings: list[tuple[str, str]] = []
        for b in yaml.safe_load((self.root / "bindings.yaml").read_text()) or []:
            if set(b) != {"pattern", "schema"}:
                raise SchemaError(f"bindings.yaml: each entry needs exactly pattern and schema: {b}")
            if b["schema"] not in self.schemas:
                raise SchemaError(f"bindings.yaml: unknown schema {b['schema']!r}")
            self.bindings.append((b["pattern"], b["schema"]))

    # ------------------------------------------------------------------ lookup
    def bound(self, path: str) -> str:
        rel = CHANGE_PREFIX.sub("", path)
        for pattern, name in self.bindings:
            if fnmatch.fnmatchcase(rel, pattern):
                return name
        raise SchemaError(f"G-W3: no schema is bound to {path}")

    def get(self, name: str) -> Schema:
        try:
            return self.schemas[name]
        except KeyError:
            raise SchemaError(f"unknown schema {name!r}") from None

    # ------------------------------------------------------------------ validation
    def validate(self, name: str, content: str) -> list[str]:
        s = self.get(name)
        if s.fmt == "text":
            return []
        if s.fmt == "markdown":
            errs = []
            headings = {m.group(1).strip().lower()
                        for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", content, re.M)}
            for h in s.body.get("required_headings", []):
                if h.lower() not in headings:
                    errs.append(f"missing heading '{h}'")
            patterns = s.body.get("required_patterns", [])
            descriptions = s.body.get("pattern_descriptions", [])
            descriptions = (descriptions + [None] * len(patterns))[:len(patterns)]
            for rx, why in zip(patterns, descriptions, strict=True):
                if not re.search(rx, content, re.M):
                    errs.append(f"must contain {why or 'a match for /' + rx + '/'}")
            return errs
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            return [f"not valid JSON: {e}"]
        return self.validate_data(name, data)

    def validate_data(self, name: str, data) -> list[str]:
        s = self.get(name)
        errs = sorted(s.validator.iter_errors(data), key=lambda e: list(e.absolute_path))
        out = []
        for e in errs[:MAX_ERRORS]:
            where = "/".join(str(x) for x in e.absolute_path) or "(root)"
            out.append(f"{where}: {e.message}")
        if len(errs) > MAX_ERRORS:
            out.append(f"... and {len(errs) - MAX_ERRORS} more")
        return out

    def check(self, path: str, content: str, schema: str | None = None) -> Schema:
        """Guard G-W3: resolve the bound schema, refuse a mismatched name, validate content."""
        name = self.bound(path)
        if schema is not None and schema != name:
            raise SchemaError(f"G-W3: {path} is bound to schema {name!r}, not {schema!r}")
        errs = self.validate(name, content)
        if errs:
            raise SchemaError(f"G-W3: {path} does not match schema {name!r}: " + "; ".join(errs))
        return self.schemas[name]
