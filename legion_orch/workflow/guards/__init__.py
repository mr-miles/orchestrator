"""Workflow guards. Every guard id documented in WORKFLOW.md is listed in :data:`GUARDS`
(catalog.py) and implemented by one of the modules here, grouped by workflow stage:

* report.py – universal report checks (G-R*),
* intake.py – requirements and risk (G-I*),
* plan.py   – task graph and oracle (G-P*), waves,
* review.py – findings and verdicts (G-C1, G-RV*, G-INT1),
* work.py   – scoped diffs, evidence, result coverage, release documents.

Guards are pure functions over documents and data; they return violation strings prefixed with
the guard id. Callers use the names re-exported here.
"""
from .catalog import GUARDS
from .common import load_json
from .intake import intake, requirements_ids
from .plan import plan, waves
from .report import report_limits, report_shape, required_docs, required_git
from .review import critique, findings, high_findings, verdict
from .work import evidence_mentions, release_docs, results_cover, scoped_diff

__all__ = [
    "GUARDS", "critique", "evidence_mentions", "findings", "high_findings", "intake", "load_json",
    "plan", "release_docs", "report_limits", "report_shape", "required_docs", "required_git",
    "requirements_ids", "results_cover", "scoped_diff", "verdict", "waves",
]
