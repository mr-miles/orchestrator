"""Findings and verdicts of critique, review and integration (G-C1, G-RV2..G-RV4, G-INT1)."""
from __future__ import annotations

SEVERITIES = {"BLOCKER", "WARNING", "SUGGESTION"}
CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}


def findings(data: dict, atts_ok: set[str]) -> list[str]:
    v = []
    fs = data.get("findings", [])
    if not isinstance(fs, list):
        return ["G-RV2: data.findings must be a list"]
    for i, f in enumerate(fs):
        if not isinstance(f, dict):
            v.append(f"G-RV2: finding {i} must be an object")
            continue
        if f.get("severity") not in SEVERITIES:
            v.append(f"G-RV2: finding {i} severity must be one of {sorted(SEVERITIES)}")
        if f.get("confidence") not in CONFIDENCES:
            v.append(f"G-RV2: finding {i} confidence must be one of {sorted(CONFIDENCES)}")
        if not str(f.get("title") or "").strip() or not str(f.get("fix") or "").strip():
            v.append(f"G-RV2: finding {i} needs title and fix")
        refs = f.get("attestations") or []
        if not refs:
            v.append(f"G-RV2: finding {i} needs at least one attestation")
        for r in refs:
            if r not in atts_ok:
                v.append(f"G-RV2: finding {i} attestation {r} must also be listed (and resolve) "
                         "in the report attestations")
    return v


def high_findings(data: dict) -> list[dict]:
    return [f for f in data.get("findings", []) if isinstance(f, dict)
            and f.get("confidence") == "HIGH" and f.get("severity") in ("BLOCKER", "WARNING")]


def verdict(data: dict, *, pass_word: str, fail_word: str, gid: str, cycle: int,
            require_checked: bool) -> list[str]:
    v = []
    verdict_ = data.get("verdict")
    if verdict_ not in (pass_word, fail_word):
        return [f"{gid}: data.verdict must be {pass_word} or {fail_word}"]
    high = high_findings(data)
    if verdict_ == pass_word and high:
        v.append(f"G-RV3: {pass_word} is not allowed with {len(high)} HIGH BLOCKER/WARNING finding(s)")
    if verdict_ == fail_word and not high:
        v.append(f"G-RV3: {fail_word} requires at least one HIGH BLOCKER/WARNING finding")
    if require_checked and verdict_ == pass_word and cycle == 1 and not data.get("checked"):
        v.append("G-RV4: a first-cycle PASS must list what was checked in data.checked")
    return v


def critique(data: dict, plan_atts: dict[str, int], latest: dict[str, int | None],
             atts_ok: set[str]) -> list[str]:
    v = []
    if data.get("verdict") not in ("PASS", "REWORK"):
        return ["G-C1: data.verdict must be PASS or REWORK"]
    if data["verdict"] == "REWORK" and not data.get("findings"):
        v.append("G-C1: REWORK requires at least one finding")
    v += [x.replace("G-RV2", "G-C1") for x in findings(data, atts_ok)]
    for path, ver in latest.items():
        if plan_atts.get(path) != ver:
            v.append(f"G-C1: critique must attest file:{path}@{ver} (latest plan version)")
    return v
