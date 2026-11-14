"""Typed ``data`` of an agent's DONE report, one model per report schema (assets/schemas/report-*).

The JSON Schema bound to the role (``roles.yaml`` ``report_schema``) is checked first (G-R7);
the data is then parsed into the matching model here, so guards and role handlers work on typed
fields instead of dict lookups.
"""
from __future__ import annotations

from typing import Any

from .model import CommandResult, Finding, Record, Verdict


class NoData(Record):
    """Roles whose report carries no structured data."""


class ResultsData(Record):
    """Commands the agent ran, with their exit codes (baseline)."""
    results: list[CommandResult]

    def missing(self, commands: list[str]) -> list[str]:
        """Commands without a result."""
        got = {r.command for r in self.results}
        return [c for c in commands if c not in got]


class ReviewData(Record):
    """A judge's verdict with findings (reviewer, security, architecture; base of the others)."""
    verdict: Verdict
    findings: list[Finding] = []
    checked: list[str] = []

    @property
    def high(self) -> list[Finding]:
        """HIGH-confidence BLOCKER/WARNING findings: the ones that drive rework."""
        return [f for f in self.findings if f.high]


class CritiqueData(ReviewData):
    """The plan critic's verdict (PASS or REWORK)."""


class IntegrationData(ReviewData, ResultsData):
    """The integration verifier's verdict at the integration head, with command results."""


MODEL_FOR_SCHEMA: dict[str, type[Record]] = {
    "report-none": NoData,
    "report-baseline": ResultsData,
    "report-review": ReviewData,
    "report-critique": CritiqueData,
    "report-integration": IntegrationData,
}


def parse(schema: str, data: dict[str, Any]) -> Record:
    """Parse schema-valid report data into its model."""
    return MODEL_FOR_SCHEMA[schema].model_validate(data)
