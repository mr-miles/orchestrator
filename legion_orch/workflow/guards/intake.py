"""Intake guards: requirements, risk signals, open questions (G-I1..G-I3)."""
from __future__ import annotations

import re
from typing import Any

from .. import risk
from ..model import ReportStatus

MAX_REQUIREMENTS = 100


def requirements_ids(text: str) -> list[str]:
    ids = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*]|#+)?\s*\**(REQ-[0-9]+)\b", line)
        if m:
            ids.append(m.group(1))
    return ids


def intake(req_text: str, risk_json: Any, questions_text: str | None, status: str) -> list[str]:
    v = []
    ids = requirements_ids(req_text)
    if not ids:
        v.append("G-I1: requirements.md must define at least one REQ-<n> (one per line/heading)")
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        v.append(f"G-I1: duplicate requirement ids {dups}")
    if len(set(ids)) > MAX_REQUIREMENTS:
        v.append(f"G-I1: {len(set(ids))} requirements; at most {MAX_REQUIREMENTS} per change – "
                 "split the goal into several changes")
    v += risk.validate_signals(risk_json)
    if questions_text and status == ReportStatus.DONE:
        open_q = [ln.strip() for ln in questions_text.splitlines() if re.match(r"^\s*[-*]\s*\[ \]", ln)]
        if open_q:
            v.append(f"G-I3: {len(open_q)} unanswered question(s) in intake/questions.md – "
                     "answer them or report BLOCKED with the question as missing_decision")
    return v
