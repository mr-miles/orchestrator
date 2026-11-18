"""Human governance records. Mirrors legion-ascended `approve`, `attest` and the risk-override
model; every record is a versioned document under changes/<id>/control/.

* approvals.py    – H-2 per-subject approvals (plan, each delta spec, oracle) and freshness (G-B1),
  and the environment approval's freshness (G-E5),
* attestations.py – human attestations (G-AT1),
* protected.py    – protected paths and per-task approvals to change them (G-PP1),
* surfaces.py     – verification-surface pins and drift (G-SF1),
* overrides.py    – added and waived gates (G-OV1), the change's gate set.

Everything here works against :class:`~legion_orch.workflow.view.ChangeView` only.
"""
from .approvals import (
    SubjectStatus,
    approval_key,
    approve_subjects,
    baseline_warnings,
    h2_status,
    h2_subjects,
    last_h2_approval_at,
    stale_approvals,
    stale_environment,
)
from .attestations import ATTESTATION_KINDS, coverage_ok, record_attestation
from .common import GovernanceError
from .overrides import gate_set, record_override, waiver_for
from .protected import (
    all_approved_protected,
    approved_protected_for,
    protected_paths,
    protected_unchanged,
    record_protected_approval,
)
from .surfaces import pin_surfaces, surface_commands, surface_drift, surfaces

__all__ = [
    "ATTESTATION_KINDS", "GovernanceError", "SubjectStatus", "all_approved_protected",
    "approval_key", "approve_subjects", "approved_protected_for", "baseline_warnings",
    "coverage_ok", "gate_set",
    "h2_status", "h2_subjects", "last_h2_approval_at", "pin_surfaces", "protected_paths",
    "protected_unchanged",
    "record_attestation", "record_override", "record_protected_approval", "stale_approvals",
    "stale_environment",
    "surface_commands", "surface_drift", "surfaces", "waiver_for",
]
