"""Guards over git-scoped work: scoped diffs, evidence, result coverage, release documents."""
from __future__ import annotations

import re

from ...git import path_matches
from ..report_data import ResultsData

SCOPE, FORBIDDEN, GUARDED = "outside files_modified", "forbidden", "protected"


def beyond_scope(changed: list[str], allowed: list[str], forbidden: list[str],
                 guarded: list[str] = ()) -> dict[str, str]:
    """The changed paths that need a reason, and why each does: a guarded (protected) path,
    one in ``forbidden``, one outside ``allowed``."""
    out: dict[str, str] = {}
    for p in changed:
        if any(path_matches(p, g) for g in guarded):
            out[p] = GUARDED
        elif any(path_matches(p, f) for f in forbidden):
            out[p] = FORBIDDEN
        elif not any(path_matches(p, a) for a in allowed):
            out[p] = SCOPE
    return out


def scoped_diff(changed: list[str], allowed: list[str], forbidden: list[str],
                protected: list[str], gid: str, reasons: dict[str, str] | None = None,
                guarded: list[str] = (), why_protected: str = "") -> list[str]:
    """G-B3: the commit changes something; never a ``protected`` path (the oracle's tests: only
    the oracle changes them); and every path outside ``allowed``, in ``forbidden`` or
    ``guarded`` comes with a reason – ``reasons``, path -> why – which a reviewer judges (a
    guarded one strictly). ``reasons`` None: nothing may be beyond (a role without them)."""
    v = []
    if not changed:
        v.append(f"{gid}: commit changes no files")
    out = beyond_scope(changed, allowed, forbidden, guarded)
    for p in changed:
        if any(path_matches(p, f) for f in protected):
            v.append(f"{gid}: {p} is a protected path" + (f": {why_protected}" if why_protected
                                                            else ""))
        elif p in out and reasons is None:
            v.append(f"{gid}: {p} is outside the allowed paths")
        elif p in out and not (reasons or {}).get(p, "").strip():
            v.append(f"{gid}: {p} is {out[p] if out[p] != SCOPE else 'outside your files_modified'}"
                     ": revert it, or list it in data.beyond_scope with the reason your contract "
                     "needs it (a reviewer judges the reason"
                     + (", strictly: a protected path)" if out[p] == GUARDED else ")"))
    for p in sorted(set(reasons or {}) - set(out)):
        v.append(f"{gid}: data.beyond_scope lists {p}, which "
                 + ("the commit does not change" if p not in changed else "is inside your scope"))
    return v


def evidence_mentions(text: str, commands: list[str]) -> list[str]:
    return [f"G-B4: evidence document does not list verification command `{c}`"
            for c in commands if c not in text]


def results_cover(data: ResultsData, commands: list[str], gid: str) -> list[str]:
    return [f"{gid}: no result for command `{c}`" for c in data.missing(commands)]


_STRATEGY = re.compile(r"^[\s*_-]*strategy[\s*_]*:[\s*_]*(rollback|forward[- ]fix)\b",
                       re.IGNORECASE | re.MULTILINE)
_RATIONALE = re.compile(r"^[\s*_-]*rationale[\s*_]*:[\s*_]*(.+)$", re.IGNORECASE | re.MULTILINE)


def recovery_strategy(rollback_text: str) -> tuple[str | None, str]:
    """The release planner's recommended recovery – ``rollback`` or ``forward-fix`` – and its
    rationale, from ``Strategy:`` / ``Rationale:`` lines in release/rollback.md."""
    m = _STRATEGY.search(rollback_text)
    r = _RATIONALE.search(rollback_text)
    strategy = m.group(1).lower().replace(" ", "-") if m else None
    return strategy, (r.group(1).strip().strip("*_ ") if r else "")


def release_docs(plan_text: str, rollback_text: str, base_sha: str) -> list[str]:
    v = []
    low = plan_text.lower()
    if "## health criteria" not in low or "## rollback criteria" not in low:
        v.append("G-REL1: release/plan.md needs '## Health criteria' and '## Rollback criteria'")
    if base_sha not in rollback_text:
        v.append(f"G-REL1: release/rollback.md must name the revert target {base_sha}")
    if recovery_strategy(rollback_text)[0] is None:
        v.append("G-REL1: release/rollback.md must recommend a recovery strategy on its own line: "
                 "`Strategy: rollback` or `Strategy: forward-fix` (then `Rationale: …`)")
    return v
