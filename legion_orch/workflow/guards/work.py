"""Guards over git-scoped work: scoped diffs, evidence, result coverage, release documents."""
from __future__ import annotations

from ...git import path_matches


def scoped_diff(changed: list[str], allowed: list[str], forbidden: list[str],
                protected: list[str], gid: str) -> list[str]:
    v = []
    if not changed:
        v.append(f"{gid}: commit changes no files")
    for p in changed:
        if not any(path_matches(p, a) for a in allowed):
            v.append(f"{gid}: {p} is outside the allowed paths")
        if any(path_matches(p, f) for f in forbidden):
            v.append(f"{gid}: {p} is forbidden for this task")
        if any(path_matches(p, f) for f in protected):
            v.append(f"{gid}: {p} is a protected path")
    return v


def evidence_mentions(text: str, commands: list[str]) -> list[str]:
    return [f"G-B4: evidence document does not list verification command `{c}`"
            for c in commands if c not in text]


def results_cover(data: dict, commands: list[str], gid: str) -> list[str]:
    res = data.get("results")
    if not isinstance(res, list):
        return [f"{gid}: data.results must be a list of {{command, exit_code}}"]
    got = {r.get("command") for r in res if isinstance(r, dict) and isinstance(r.get("exit_code"), int)}
    return [f"{gid}: no result for command `{c}`" for c in commands if c not in got]


def release_docs(plan_text: str, rollback_text: str, base_sha: str) -> list[str]:
    v = []
    low = plan_text.lower()
    if "## health criteria" not in low or "## rollback criteria" not in low:
        v.append("G-REL1: release/plan.md needs '## Health criteria' and '## Rollback criteria'")
    if base_sha not in rollback_text:
        v.append(f"G-REL1: release/rollback.md must name the revert target {base_sha}")
    return v
