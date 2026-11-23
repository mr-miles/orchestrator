"""Shared engine constants and helpers."""
from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ...governance.approvals import ENV_SPEC, PLAN_DOCS  # noqa: F401  (re-exported)

log = logging.getLogger("legion")
SKILLS_DIR = "skills"      # payload/skills/<name>/SKILL.md, installed by run-agent.sh


def write_skills(root: Path, skills: dict[str, str]) -> None:
    """A run's rendered skills, one ``<name>/SKILL.md`` each (replacing any earlier set)."""
    shutil.rmtree(root, ignore_errors=True)
    for name, text in skills.items():
        (root / name).mkdir(parents=True)
        (root / name / "SKILL.md").write_text(text)


class WorkflowError(Exception):
    """A refused human/API action (shown to the operator)."""


class AgentBusy(WorkflowError):
    """An A2A answer was asked of a step whose agent is running (or about to): its inbox takes
    the message instead."""


class NoAgent(WorkflowError):
    """A step has no agent session to answer with any more (its sandbox was deleted, or is
    being retired)."""


def _slug(node_id: str) -> str:
    """A node id as a git branch component (``build:T1`` -> ``build-t1``: no ``:`` in a ref)."""
    return node_id.replace(":", "-").lower()


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
