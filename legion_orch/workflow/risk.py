"""Risk signals -> tier (mirrors legion-ascended DEFAULT_RISK_POLICY's thresholds and floors);
what a tier requires of the plan is its policy in assets/workflow.yaml."""
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

def rank(tier: str) -> int:
    return TIERS.index(tier)


def validate_signals(data: dict) -> list[str]:
    """Guard G-I2, beyond the risk schema's (twelve known signals scored 0–3, each with a
    rationale): every signal once, each rationale more than blank."""
    names = [s["name"] for s in data["signals"]]
    errs = [f"G-I2: signal {n} appears more than once"
            for n in sorted({n for n in names if names.count(n) > 1})]
    errs += [f"G-I2: signal {s['name']} needs a rationale"
             for s in data["signals"] if not s["rationale"].strip()]
    if missing := [n for n in SIGNALS if n not in names]:
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
