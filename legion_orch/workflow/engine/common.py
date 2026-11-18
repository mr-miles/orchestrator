"""Shared engine constants and helpers."""
from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ..governance.approvals import ENV_SPEC  # noqa: F401  (re-exported: the environment spec)

log = logging.getLogger("legion")
PLAN_DOCS = ["plan/spec.md", "plan/oracle.json", "plan/taskgraph.json"]
SKILLS_DIR = "skills"      # payload/skills/<name>/SKILL.md, installed by run-agent.sh


def write_skills(root: Path, skills: dict[str, str]) -> None:
    """A run's rendered skills, one ``<name>/SKILL.md`` each (replacing any earlier set)."""
    shutil.rmtree(root, ignore_errors=True)
    for name, text in skills.items():
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(text)


class WorkflowError(Exception):
    """A refused human/API action (shown to the operator)."""


def _slug(node_id: str) -> str:
    return node_id.replace(":", "-").lower()


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
