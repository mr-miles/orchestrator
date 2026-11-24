"""The design-review roles (REQ-6): assets, bindings and the handlers' guards (G-DR1..G-DR4)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from legion_orch.workflow import roles
from legion_orch.workflow.assets import TEMPLATE_VARS, Assets
from legion_orch.workflow.environment import PRE_PROVISION_ROLES
from legion_orch.workflow.risk import SIGNALS
from legion_orch.workflow.roles.base import Report

ROOT = Path(__file__).resolve().parent.parent
NEW = ("design-reviewer", "findings-reviewer", "selector")
BASE = ["harness-contract", "a2a-protocol", "a2a-cli", "docstore-protocol", "attestation-format"]
EV = "git:app/a.py@abcdef1"
FINDINGS = {"findings": [{"id": "F-1", "evidence": [EV]}, {"id": "F-2", "evidence": [EV]}]}
REVIEW = {"reviews": [{"finding": "F-1", "verdict": "valid"},
                      {"finding": "F-2", "verdict": "valid"}]}


@pytest.fixture(scope="module")
def assets():
    return Assets(ROOT / "assets")


def test_assets_load(assets):
    for role in NEW:
        assert role in assets.roles


def test_skills_are_exact(assets):
    s = {r: list(assets.roles[r].skills) for r in NEW}
    assert s["design-reviewer"] == [*BASE, "codebase-design", "blocked-protocol"]
    assert s["findings-reviewer"] == [*BASE, "finding-format", "blocked-protocol"]
    assert s["selector"] == [*BASE, "blocked-protocol"]


def test_codebase_design_only_for_design_reviewer(assets):
    users = [n for n, r in assets.roles.items() if "codebase-design" in r.skills]
    assert users == ["design-reviewer"]


def test_every_role_has_a_handler(assets):
    assert set(assets.roles) <= set(roles.REGISTRY)


def test_pre_provision_roles():
    assert set(NEW) <= PRE_PROVISION_ROLES


@pytest.mark.parametrize("role", NEW)
def test_prompt_renders(assets, role):
    out = assets.render_role(role, {v: "x" for v in TEMPLATE_VARS})
    assert out is not None


def test_findings_reviewer_is_a_critic():
    assert isinstance(roles.REGISTRY["findings-reviewer"], roles.REGISTRY["critic"].__class__)


# ---------------------------------------------------------------- handler guards
def ctx(docs: dict[str, object], outputs: dict[str, str] | None = None, pointers=None,
        tickets=None):
    def read(path, version):
        return NS(content=json.dumps(docs[path]) if not isinstance(docs[path], str)
                  else docs[path])
    t = tickets or {}
    return NS(docs=NS(read=read), tickets=NS(t=t, root=NS(pointers=pointers or {})))


def report(outputs=None, atts=()):
    parsed = [NS(__str__=None) for _ in ()]
    r = Report(run=None, ticket=NS(parent=None), parsed=parsed, data=NS(), outputs=outputs or {})
    r.__class__ = type("R", (Report,), {"repo_atts": property(lambda self: set(atts))})
    return r


def run(coro):
    return asyncio.run(coro)


def test_design_reviewer_reports_unattested_evidence():
    e = ctx({"d/findings.json": FINDINGS})
    r = report({"findings": "file:d/findings.json@1"})
    out = run(roles.REGISTRY["design-reviewer"].guards(e, r))
    assert out and all(x.startswith("G-DR1") for x in out)
    assert not run(roles.REGISTRY["design-reviewer"].guards(
        e, report({"findings": "file:d/findings.json@1"}, {EV})))


def test_design_reviewer_not_json():
    e = ctx({"d/findings.json": "nope"})
    out = run(roles.REGISTRY["design-reviewer"].guards(
        e, report({"findings": "file:d/findings.json@1"})))
    assert out[0].startswith("G-DR1: findings is not JSON")


def _selector_ctx(selection):
    live = {"T1": NS(status="passed", type="agent:design-reviewer",
                     outputs={"findings": "file:d/findings.json@1"}),
            "T2": NS(status="passed", type="agent:findings-reviewer",
                     outputs={"findings-review": "file:d/review.json@1"})}
    return ctx({"d/findings.json": FINDINGS, "d/review.json": REVIEW, "d/sel.json": selection},
               tickets=live)


def test_selector_reports_unknown_id():
    sel = {"selected": [{"id": "F-9", "reason": "r", "scope": "s", "evidence": [EV]}],
           "dropped": [{"id": "F-1", "reason": "r"}, {"id": "F-2", "reason": "r"}]}
    out = run(roles.REGISTRY["selector"].guards(
        _selector_ctx(sel), report({"selection": "file:d/sel.json@1"})))
    assert any(x.startswith("G-DR3: unknown finding F-9") for x in out)


def test_selector_context_is_latest_refs():
    e = _selector_ctx({})
    assert roles.REGISTRY["selector"].extra_context(e, None) == [
        "file:d/findings.json@1", "file:d/review.json@1"]


SEL = {"selected": [{"id": "F-1", "reason": "r", "scope": "s", "evidence": [EV]}], "dropped": []}


def _intake(pointers, reqs):
    risk = {"signals": [{"name": n, "score": 0, "rationale": "r"} for n in SIGNALS]}
    docs = {"d/req.md": reqs, "d/risk.json": risk, "d/sel.json": SEL}
    return ctx(docs, pointers=pointers), report({"requirements": "file:d/req.md@1",
                                                 "risk": "file:d/risk.json@1"})


def test_intake_selection_uncovered():
    e, r = _intake({"selection": "file:d/sel.json@1"}, "- REQ-1: build something\n")
    out = run(roles.REGISTRY["intake"].guards(e, r))
    assert any(x.startswith("G-DR4") for x in out)


def test_intake_without_selection_has_no_dr4():
    e, r = _intake({}, "- REQ-1: build something\n")
    out = run(roles.REGISTRY["intake"].guards(e, r))
    assert not any("G-DR4" in x for x in out)


def test_intake_selection_not_json():
    e, r = _intake({"selection": "file:d/bad.json@1"}, "- REQ-1: x F-1\n")
    orig = e.docs.read
    e.docs.read = lambda p, v: NS(content="nope") if "bad" in p else orig(p, v)
    out = run(roles.REGISTRY["intake"].guards(e, r))
    assert any("G-DR4: selection is not JSON" in x for x in out)
