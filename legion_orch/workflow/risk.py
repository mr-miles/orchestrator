"""Risk signals -> tier -> gates (mirrors legion-ascended DEFAULT_RISK_POLICY)."""
from __future__ import annotations

from .model import RiskAssessment, RiskFloor

SIGNALS = [
    "security", "authorization", "data_migration", "external_side_effect", "public_api", "ui",
    "performance", "infrastructure", "irreversible_action", "scope_breadth",
    "novelty_uncertainty", "verification_quality",
]
# Signals whose score of 2 forces at least R2 (all except novelty/verification quality).
R2_FLOOR_AT_2 = set(SIGNALS) - {"novelty_uncertainty", "verification_quality"}
TIERS = ["R0", "R1", "R2", "R3"]

GATES_BY_TIER: dict[str, list[str]] = {
    "R0": ["current_task_contract_or_small_change_record", "deterministic_verification",
           "evidence_note"],
    "R1": ["task_contract", "scoped_implementer_run", "deterministic_verification",
           "evidence_bundle_or_log", "lightweight_independent_review"],
    "R2": ["approved_delta_spec", "protected_oracle", "task_contract",
           "deterministic_verification", "task_level_independent_review",
           "integration_or_real_interface_checks", "whole_change_acceptance_evidence"],
    "R3": ["independent_baseline", "approved_spec_and_oracle", "protected_oracle",
           "deterministic_verification", "architecture_or_security_review",
           "protected_acceptance_tests", "security_or_e2e_evaluator", "explicit_human_approval",
           "release_observation_plan", "rollback_or_forward_fix_evidence"],
}
WAIVABLE_GATES = {"release_observation_plan"}


def rank(tier: str) -> int:
    return TIERS.index(tier)


def validate_signals(data: dict) -> list[str]:
    """Guard G-I2."""
    errs: list[str] = []
    sigs = data.get("signals") if isinstance(data, dict) else None
    if not isinstance(sigs, list):
        return ["G-I2: risk.json must contain a 'signals' list"]
    seen = {}
    for s in sigs:
        if not isinstance(s, dict) or s.get("name") not in SIGNALS:
            errs.append(f"G-I2: unknown or malformed signal {s!r:.80}")
            continue
        score = s.get("score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 3:
            errs.append(f"G-I2: signal {s['name']} score must be an integer 0..3")
        if not str(s.get("rationale") or "").strip():
            errs.append(f"G-I2: signal {s['name']} needs a rationale")
        seen[s["name"]] = s
    missing = [n for n in SIGNALS if n not in seen]
    if missing:
        errs.append(f"G-I2: missing signals {missing}")
    return errs


def derive_tier(data: dict) -> RiskAssessment:
    scores = {s["name"]: int(s["score"]) for s in data["signals"]}
    total = sum(scores.values())
    threshold = "R0" if total <= 2 else "R1" if total <= 5 else "R2" if total <= 8 else "R3"
    floors = []
    for name, score in scores.items():
        if score >= 3:
            floors.append(RiskFloor(signal=name, tier="R3"))
        elif score >= 2 and name in R2_FLOOR_AT_2:
            floors.append(RiskFloor(signal=name, tier="R2"))
    tier = max([threshold] + [f.tier for f in floors], key=rank)
    return RiskAssessment(total=total, threshold_tier=threshold, floors=floors, tier=tier,
                          scores=scores)


def review_panel(tier: str, scores: dict[str, int]) -> list[str]:
    if tier == "R0":
        return []
    if tier == "R1":
        return ["reviewer"]
    if tier == "R2":
        panel = ["reviewer"]
        if max(scores.get("security", 0), scores.get("authorization", 0)) >= 2:
            panel.append("security")
        if max(scores.get("scope_breadth", 0), scores.get("novelty_uncertainty", 0)) >= 2:
            panel.append("architecture")
        return panel
    return ["reviewer", "security", "architecture"]


def valid_reason(reason: str | None) -> bool:
    reason = (reason or "").strip()
    return len(reason) >= 24 and len(reason.split()) > 1
