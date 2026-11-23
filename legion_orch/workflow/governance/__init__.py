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
    BASELINE_SUBJECT,
    ENV_FILE,
    ENVIRONMENT_SUBJECT,
    PLAN_FILES,
    PLAN_RECORD,
    BaselineSubject,
    SubjectStatus,
    approval_key,
    approve_subjects,
    baseline_subject,
    baseline_warnings,
    draft,
    env_spec,
    h2_status,
    h2_subjects,
    last_h2_approval_at,
    oracle_subjects,
    pinned,
    plan_docs,
    plan_subjects,
    spec_doc,
    stale_approvals,
    stale_environment,
    subject_status,
    work_to,
)
from .attestations import (
    ATTESTATION_KINDS,
    H3_GATES,
    GateAttestation,
    coverage_ok,
    h3_attestations,
    record_attestation,
)
from .common import GovernanceError
from .overrides import gate_set, record_override, waiver_for
from .protected import (
    all_approved_protected,
    approved_protected_for,
    protected_paths,
    protected_unchanged,
    record_protected_approval,
    setup_paths,
)
from .surfaces import pin_surfaces, surface_commands, surface_drift, surfaces

__all__ = [
    "BASELINE_SUBJECT", "BaselineSubject", "baseline_subject",
    "ENV_FILE", "PLAN_FILES", "draft", "env_spec", "pinned", "plan_docs", "spec_doc",
    "work_to", "PLAN_RECORD", "ENVIRONMENT_SUBJECT",
    "oracle_subjects", "plan_subjects", "subject_status",
    "H3_GATES", "GateAttestation", "h3_attestations",
    "ATTESTATION_KINDS", "GovernanceError", "SubjectStatus", "all_approved_protected",
    "approval_key", "approve_subjects", "approved_protected_for", "baseline_warnings",
    "coverage_ok", "gate_set",
    "h2_status", "h2_subjects", "last_h2_approval_at", "pin_surfaces", "protected_paths",
    "protected_unchanged",
    "record_attestation", "record_override", "record_protected_approval", "setup_paths", "stale_approvals",
    "stale_environment",
    "surface_commands", "surface_drift", "surfaces", "waiver_for",
]
