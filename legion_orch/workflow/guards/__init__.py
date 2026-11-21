"""Workflow guards. Every guard id documented in WORKFLOW.md is listed in :data:`GUARDS`
(catalog.py) and implemented by one of the modules here, grouped by workflow stage:

* report.py – universal report checks (G-R*),
* intake.py – requirements and risk (G-I*),
* plan.py   – task graph and oracle (G-P*), fix rounds (G-FR*), waves,
* environment.py – the sandbox environment spec (G-E1..G-E4),
* review.py – findings and verdicts (G-C1, G-RV*, G-INT1),
* work.py   – scoped diffs, evidence, result coverage, release documents,
* pr.py     – the pull request description and its review (G-PR*).

Guards are pure functions over documents and data; they return violation strings prefixed with
the guard id. Callers use the names re-exported here.
"""
from .catalog import GUARDS
from .common import load_json
from .environment import environment
from .intake import intake, requirements_ids
from .plan import fix_round, plan, waves
from .pr import PR_DOC, pr_body, pr_description, pr_review, pr_title
from .report import report_limits, report_shape, required_docs, required_git
from .review import critique, findings, verdict
from .work import evidence_mentions, recovery_strategy, release_docs, results_cover, scoped_diff

__all__ = [
    "GUARDS", "PR_DOC", "critique", "environment", "evidence_mentions", "findings", "fix_round",
    "intake", "load_json", "plan", "pr_body", "pr_description", "pr_review", "pr_title",
    "release_docs", "report_limits", "report_shape", "required_docs", "required_git",
    "recovery_strategy", "requirements_ids", "results_cover", "scoped_diff", "verdict", "waves",
]
