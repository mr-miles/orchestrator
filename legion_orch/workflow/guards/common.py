"""Helpers shared by the guard modules."""
from __future__ import annotations

import json
from typing import Any

from ...store import DocStore
from ..attest import Attestation


def load_json(docs: DocStore, att: Attestation | None, gid: str) -> tuple[Any, list[str]]:
    if att is None:
        return None, [f"{gid}: document not attested"]
    try:
        return json.loads(docs.read(att.path, att.version).content), []
    except json.JSONDecodeError as e:
        return None, [f"{gid}: {att} is not valid JSON: {e}"]
