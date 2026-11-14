"""Shared engine constants and helpers."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

log = logging.getLogger("legion")


PUSH_ROLES = {"implementer", "oracle"}


PANEL_ROLES = ("reviewer", "security", "architecture")


JUDGE_ROLES = {"critic", "integration", "baseline", *PANEL_ROLES}


PLAN_DOCS = ["plan/spec.md", "plan/oracle.json", "plan/taskgraph.json"]


class WorkflowError(Exception):
    """A refused human/API action (shown to the operator)."""


def _slug(node_id: str) -> str:
    return node_id.replace(":", "-").lower()


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
