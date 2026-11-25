"""Design-review documents checked against each other (G-DR1..G-DR4).

The documents have already passed their JSON Schemas at write time (shape), so these functions
check only what a schema cannot: ids and evidence across findings, review, selection and the
requirements text. They are pure, tolerate missing or mistyped keys (read as empty) and never
raise; each problem string starts with its guard id and ``[]`` means consistent.
"""
from __future__ import annotations

import re

_BLOCK = re.compile(r"^\s*(?:[-*]|#+)?\s*\**REQ-[0-9]+\b")
_FID = re.compile(r"\bF-[0-9]+\b")


def _items(doc: object, key: str) -> list[dict]:
    items = doc.get(key) if isinstance(doc, dict) else None
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def _ids(items: list[dict], key: str = "id") -> list[str]:
    return [i[key] for i in items if isinstance(i.get(key), str)]


def _evidence(item: dict) -> set[str]:
    ev = item.get("evidence")
    return {e for e in ev if isinstance(e, str)} if isinstance(ev, list) else set()


def _dups(ids: list[str]) -> list[str]:
    return sorted({i for i in ids if ids.count(i) > 1})


def findings_problems(doc: dict, attested: set[str]) -> list[str]:
    """G-DR1: finding ids are unique and every evidence ref is attested by the report."""
    items = _items(doc, "findings")
    v = [f"G-DR1: duplicate finding id {i}" for i in _dups(_ids(items))]
    for f in items:
        for ref in sorted(_evidence(f) - set(attested)):
            v.append(f"G-DR1: finding {f.get('id')} evidence {ref} must also be listed (and "
                     "resolve) in the report attestations")
    return v


def review_problems(review: dict, findings: dict) -> list[str]:
    """G-DR2: every finding is reviewed exactly once, and only findings are reviewed."""
    known = set(_ids(_items(findings, "findings")))
    reviewed = _ids(_items(review, "reviews"), "finding")
    v = [f"G-DR2: review of unknown finding {i}" for i in sorted(set(reviewed) - known)]
    v += [f"G-DR2: finding {i} reviewed more than once" for i in _dups(reviewed)]
    v += [f"G-DR2: finding {i} has no review" for i in sorted(known - set(reviewed))]
    return v


def selection_problems(selection: dict, findings: dict, review: dict) -> list[str]:
    """G-DR3: each finding is selected or dropped exactly once; a selected finding is reviewed
    ``valid`` and keeps the finding's evidence."""
    by_id = {f["id"]: f for f in _items(findings, "findings") if isinstance(f.get("id"), str)}
    verdicts = {r["finding"]: r.get("verdict") for r in _items(review, "reviews")
                if isinstance(r.get("finding"), str)}
    selected = _items(selection, "selected")
    sel, drp = _ids(selected), _ids(_items(selection, "dropped"))
    listed = sel + drp
    v = [f"G-DR3: unknown finding {i}" for i in sorted(set(listed) - set(by_id))]
    v += [f"G-DR3: finding {i} listed more than once" for i in _dups(listed)]
    v += [f"G-DR3: finding {i} is neither selected nor dropped" for i in sorted(set(by_id) - set(listed))]
    for item in selected:
        i = item.get("id")
        if i not in by_id:
            continue
        if verdicts.get(i) != "valid":
            v.append(f"G-DR3: selected finding {i} is not reviewed valid "
                     f"({verdicts.get(i, 'unreviewed')})")
        if _evidence(item) != _evidence(by_id[i]):
            v.append(f"G-DR3: selected finding {i} evidence differs from the finding's")
    return v


def requirements_problems(requirements: str, selection: dict) -> list[str]:
    """G-DR4: every REQ block cites a selected finding id, and every selected id is cited."""
    selected = set(_ids(_items(selection, "selected")))
    blocks: list[list[str]] = []
    for line in (requirements if isinstance(requirements, str) else "").splitlines():
        if _BLOCK.match(line):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
    v, cited = [], set()
    for lines in blocks:
        name = re.search(r"REQ-[0-9]+", lines[0]).group(0)
        ids = set(_FID.findall("\n".join(lines)))
        cited |= ids
        if not ids:
            v.append(f"G-DR4: {name} cites no finding id")
        v += [f"G-DR4: {name} cites {i}, which is not selected" for i in sorted(ids - selected)]
    v += [f"G-DR4: selected finding {i} is cited by no requirement" for i in sorted(selected - cited)]
    return v
