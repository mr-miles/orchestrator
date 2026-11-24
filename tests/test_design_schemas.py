"""Design-review documents: schema store checks (REQ-7) and the cross-document guards G-DR1..4."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from legion_orch.store import SchemaRegistry
from legion_orch.workflow import guards as g

ROOT = Path(__file__).resolve().parent.parent
REG = SchemaRegistry(ROOT / "assets" / "schemas")
EV1, EV2 = "git:app/a.py@abcdef1", "git:app/b.py@abcdef2"


def finding(i, ev=None, **kw):
    return {"id": f"F-{i}", "title": "t", "evidence": [EV1] if ev is None else ev, "problem": "p",
            "direction": "d", "severity": "high", "effort": "small"} | kw


def without(d, key):
    return {k: v for k, v in d.items() if k != key}


def accepted(name, doc):
    try:
        REG.check(f"changes/c1/tickets/x/{name}.json", json.dumps(doc))
    except Exception:
        return False
    return True


FINDINGS = {"findings": [finding(1), finding(2, [EV2])]}
REVIEW = {"reviews": [{"finding": "F-1", "verdict": "valid", "severity": "high", "note": "n"},
                      {"finding": "F-2", "verdict": "invalid", "severity": "low", "note": "n"}]}
SELECTION = {"selected": [{"id": "F-1", "reason": "r", "scope": "s", "evidence": [EV1]}],
             "dropped": [{"id": "F-2", "reason": "r"}]}
ATT = {EV1, EV2}


def test_findings_valid_accepted():
    assert accepted("findings", FINDINGS)


@pytest.mark.parametrize("bad", [
    {"findings": [without(finding(1), "id")]},
    {"findings": [without(finding(1), "evidence")]},
    {"findings": [finding(1, [])]},
    {"findings": [finding(1, ["app/a.py"])]},
    {"findings": [finding(1, severity="urgent")]},
    {"findings": []},
])
def test_findings_malformed_rejected(bad):
    assert not accepted("findings", bad)


def test_review_schema():
    assert accepted("findings-review", REVIEW)
    bad = {"reviews": [{"finding": "F-1", "verdict": "maybe", "severity": "high", "note": "n"}]}
    assert not accepted("findings-review", bad)
    assert not accepted("findings-review", {"reviews": []})


def test_selection_schema():
    assert accepted("selection", SELECTION)
    assert not accepted("selection", {"selected": [{"id": "F-1", "scope": "s", "evidence": [EV1]}],
                                      "dropped": []})
    assert not accepted("selection", {**SELECTION, "selected": []})
    assert not accepted("selection", without(SELECTION, "dropped"))


def test_g_dr1():
    assert g.findings_problems(FINDINGS, ATT) == []
    dup = g.findings_problems({"findings": [finding(1), finding(1)]}, ATT)
    assert dup and dup[0].startswith("G-DR1")
    off = g.findings_problems(FINDINGS, {EV1})
    assert off and off[0].startswith("G-DR1")


def test_g_dr2():
    assert g.review_problems(REVIEW, FINDINGS) == []
    extra = {"reviews": REVIEW["reviews"] + [{"finding": "F-9"}]}
    assert g.review_problems(extra, FINDINGS)[0].startswith("G-DR2")
    assert g.review_problems({"reviews": REVIEW["reviews"] * 2}, FINDINGS)
    assert g.review_problems({"reviews": REVIEW["reviews"][:1]}, FINDINGS)


def test_g_dr3_consistent_and_unknown():
    assert g.selection_problems(SELECTION, FINDINGS, REVIEW) == []
    unknown = {**SELECTION, "dropped": [{"id": "F-9", "reason": "r"}]}
    p = g.selection_problems(unknown, FINDINGS, REVIEW)
    assert all(x.startswith("G-DR3") for x in p) and len(p) == 2  # unknown + F-2 in neither


def test_g_dr3_duplicates_and_both_lists():
    dup = {**SELECTION, "selected": SELECTION["selected"] * 2}
    assert g.selection_problems(dup, FINDINGS, REVIEW)
    both = {**SELECTION, "dropped": [{"id": "F-1", "reason": "r"}, {"id": "F-2", "reason": "r"}]}
    assert g.selection_problems(both, FINDINGS, REVIEW)


def test_g_dr3_neither_list():
    p = g.selection_problems({**SELECTION, "dropped": []}, FINDINGS, REVIEW)
    assert any("F-2" in x for x in p)


def test_g_dr3_selected_invalid_or_unreviewed():
    sel = {"selected": SELECTION["selected"]
           + [{"id": "F-2", "reason": "r", "scope": "s", "evidence": [EV2]}], "dropped": []}
    assert any("F-2" in x for x in g.selection_problems(sel, FINDINGS, REVIEW))
    assert any("F-1" in x for x in g.selection_problems(SELECTION, FINDINGS, {"reviews": []}))


def test_g_dr3_evidence_mismatch():
    sel = {**SELECTION, "selected": [{**SELECTION["selected"][0], "evidence": [EV2]}]}
    assert any("evidence" in x for x in g.selection_problems(sel, FINDINGS, REVIEW))


def test_g_dr4():
    assert g.requirements_problems("- REQ-1: fix it (F-1)\n", SELECTION) == []
    assert g.requirements_problems("## REQ-1 x\nfixes F-1\n\n## REQ-2 y\nno ids\n", SELECTION)[0] \
        .startswith("G-DR4")
    assert any("F-2" in x for x in g.requirements_problems("- REQ-1: F-1 and F-2\n", SELECTION))
    assert any("F-1" in x for x in g.requirements_problems("- REQ-1: nothing\n", SELECTION))
    assert any("F-1" in x for x in g.requirements_problems("", SELECTION))


def test_guards_tolerate_garbage():
    for bad in ({}, {"findings": "x"}, {"findings": [1, None]}, None):
        g.findings_problems(bad, set())
        g.review_problems(bad, bad)
        g.selection_problems(bad, bad, bad)
        g.requirements_problems("REQ-1 F-1", bad)
