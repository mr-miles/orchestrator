"""Deterministic ship readiness: evaluate every gate of the change's tier.

Each gate is ``satisfied``, ``unsatisfied`` or ``unevaluable`` (no evidence to evaluate).
Every attestation relied upon is re-resolved and git facts are recomputed live.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import attest, risk
from .gitops import path_matches
from .model import ACCEPTED, BASELINE, H_CHANGE, H_SPEC, INTEGRATION, RELEASE, _parse_ts_safe

if TYPE_CHECKING:
    from .engine import Engine

SAT, UNSAT, UNEVAL = "satisfied", "unsatisfied", "unevaluable"


def _g(status: str, detail: str, human: bool = False) -> dict:
    return {"status": status, "detail": detail, "human_judgement": human}


async def evaluate(e: "Engine") -> dict[str, Any]:
    ch = e.change
    builds = e._build_nodes()
    head = await e.git.integration_head(ch.id)
    repo = e.cfg.repo.name
    gates: dict[str, dict] = {}

    async def resolves(ref: str | None) -> bool:
        if not ref:
            return False
        try:
            return await attest.resolve(attest.parse(ref, [repo]), e.docs, e.git)
        except attest.AttestationError:
            return False

    approval = ch.approvals.get(H_SPEC)
    approval_fresh = bool(approval) and all(
        e.docs.latest_version(p) == v for p, v in approval["pins"].items()) \
        and await resolves(approval.get("attestation"))

    def all_accepted() -> bool:
        return bool(builds) and all(b.status == ACCEPTED for b in builds)

    # ---- contracts
    async def contract() -> dict:
        if not approval:
            return _g(UNEVAL, "no approved task graph (H-2)")
        if not approval_fresh:
            return _g(UNSAT, "approved task graph is stale")
        tg = e._json_doc("plan/taskgraph.json", approval["pins"]["plan/taskgraph.json"])
        ids = {t["id"] for t in tg["tasks"]}
        mine = {b.task["id"] for b in builds}
        if ids != mine:
            return _g(UNSAT, f"build tasks {sorted(mine)} != contract {sorted(ids)}")
        return _g(SAT, f"{len(ids)} task contract(s) at {approval['attestation']}")

    async def deterministic() -> dict:
        if not all_accepted():
            return _g(UNEVAL if not builds else UNSAT, "not every build task is accepted")
        for b in builds:
            ver = b.verification.get(str(b.cycle))
            if not ver or not ver["ok"] or ver["sha"] != b.accepted_sha or not await resolves(ver.get("attestation")):
                return _g(UNSAT, f"{b.id}: no passing verification at accepted sha")
        if risk.rank(ch.tier) >= 2:
            iv = e.nodes.get(INTEGRATION)
            ver = iv.verification.get("integration") if iv else None
            if not ver or not ver["ok"] or ver["sha"] != head:
                return _g(UNSAT, "no passing oracle verification at the integration head")
        return _g(SAT, "all verification commands passed at accepted commits")

    async def evidence() -> dict:
        if not all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        for b in builds:
            run = e.runs.get(b.current_run)
            atts = (run.report or {}).get("attestations", []) if run else []
            ev = [a for a in atts if a.startswith(f"file:evidence/{b.task['id']}/")]
            if not ev or not all([await resolves(a) for a in ev]):
                return _g(UNSAT, f"{b.id}: no resolvable evidence document")
        return _g(SAT, "every task has an attested evidence document")

    async def scoped() -> dict:
        if not all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        prot = list((ch.oracle or {}).get("acceptance_tests", [])) + e.cfg.protected_paths
        for b in builds:
            changed = await e.git.changed_paths(b.base_sha, b.accepted_sha)
            bad = [p for p in changed if not any(path_matches(p, a) for a in b.task["files_modified"])
                   or any(path_matches(p, x) for x in prot)]
            if bad:
                return _g(UNSAT, f"{b.id} changed out-of-scope paths {bad[:5]}")
        return _g(SAT, "every accepted diff stays inside files_modified")

    async def review(role: str) -> dict:
        if not all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        human = False
        for b in builds:
            r = b.reviews.get(f"{b.cycle}:{role}")
            if (b.human or {}).get("override"):
                human = True
                continue
            if not r or r["verdict"] != "PASS" or r["sha"] != b.accepted_sha or not await resolves(r["doc"]):
                return _g(UNSAT, f"{b.id}: no independent {role} PASS at accepted sha")
        return _g(SAT, f"independent {role} PASS on every task" +
                  (" (some by human override)" if human else ""), human)

    async def spec_approved() -> dict:
        if not approval:
            return _g(UNEVAL, "spec not approved")
        return _g(SAT, f"approved by {approval['approver']}") if approval_fresh else \
            _g(UNSAT, "spec changed after approval")

    async def spec_and_oracle_before() -> dict:
        if not approval:
            return _g(UNEVAL, "spec/oracle not approved")
        if not approval_fresh or not approval.get("oracle_sha"):
            return _g(UNSAT, "approval stale or oracle not pinned")
        if ch.first_build_at and _parse_ts_safe(approval["at"]) >= _parse_ts_safe(ch.first_build_at):
            return _g(UNSAT, "approval recorded after build started")
        return _g(SAT, f"approved {approval['at']} before first build {ch.first_build_at}")

    async def protected() -> dict:
        tests = (ch.oracle or {}).get("acceptance_tests", [])
        if not ch.oracle_sha or not tests:
            return _g(UNEVAL, "no oracle commit")
        for p in tests:
            if await e.git.blob(head, p) != await e.git.blob(ch.oracle_sha, p):
                return _g(UNSAT, f"{p} changed since oracle commit")
        return _g(SAT, f"{len(tests)} acceptance test(s) unchanged since {ch.oracle_sha[:12]}")

    async def protected_tests_ran() -> dict:
        g = await protected()
        if g["status"] != SAT:
            return g
        iv = e.nodes.get(INTEGRATION)
        ver = iv.verification.get("integration") if iv else None
        cmds = (ch.oracle or {}).get("acceptance_commands", [])
        if not ver or ver["sha"] != head:
            return _g(UNEVAL, "acceptance commands not run at head")
        ran = {r["command"]: r["exit_code"] for r in ver["results"]}
        if any(ran.get(c) != 0 for c in cmds):
            return _g(UNSAT, "acceptance commands did not all pass at head")
        return _g(SAT, "protected acceptance tests pass at head")

    async def integration() -> dict:
        iv = e.nodes.get(INTEGRATION)
        if not iv:
            return _g(UNEVAL, "no integration node")
        r = iv.reviews.get("integration")
        ver = iv.verification.get("integration")
        if iv.status != ACCEPTED or not r or r["verdict"] != "PASS" or r["sha"] != head \
                or not ver or not ver["ok"] or ver["sha"] != head:
            return _g(UNSAT, "integration verification not passed at current head")
        return _g(SAT, "integration verifier PASS and oracle commands pass at head")

    async def human_change() -> dict:
        a = ch.approvals.get(H_CHANGE)
        if not a:
            return _g(UNEVAL, "change not accepted by a human (H-3)")
        if a["approver"] not in e.cfg.decision_owners or a["head"] != head \
                or not await resolves(a["attestation"]):
            return _g(UNSAT, "acceptance stale or approver not a decision owner")
        return _g(SAT, f"accepted by {a['approver']} at {head[:12]}", True)

    async def baseline() -> dict:
        n = e.nodes.get(BASELINE)
        if not n or n.status != ACCEPTED:
            return _g(UNEVAL, "no independent baseline")
        return _g(SAT, "baseline recorded") if await resolves(e._latest_att("baseline/report.md")) \
            else _g(UNSAT, "baseline report missing")

    async def security_or_e2e() -> dict:
        s = await review("security")
        if s["status"] == SAT:
            return s
        return await integration()

    async def release_plan() -> dict:
        w = ch.waivers.get("release_observation_plan")
        if w:
            ok = await resolves(w["source"]) and await resolves(w["attestation"])
            return _g(SAT if ok else UNSAT, f"waived by {w['approver']}: {w['reason']}", True)
        n = e.nodes.get(RELEASE)
        if not n or n.status != ACCEPTED:
            return _g(UNEVAL, "no release plan")
        return _g(SAT, "release/plan.md attested") if await resolves(e._latest_att("release/plan.md")) \
            else _g(UNSAT, "release plan missing")

    async def rollback() -> dict:
        att = e._latest_att("release/rollback.md")
        if not att:
            return _g(UNEVAL, "no rollback evidence")
        return _g(SAT, f"{att}") if await resolves(att) else _g(UNSAT, "rollback doc missing")

    checks = {
        "current_task_contract_or_small_change_record": contract,
        "task_contract": contract,
        "deterministic_verification": deterministic,
        "evidence_note": evidence,
        "evidence_bundle_or_log": evidence,
        "scoped_implementer_run": scoped,
        "lightweight_independent_review": lambda: review("reviewer"),
        "task_level_independent_review": lambda: review("reviewer"),
        "approved_delta_spec": spec_approved,
        "protected_oracle": protected,
        "integration_or_real_interface_checks": integration,
        "whole_change_acceptance_evidence": human_change,
        "independent_baseline": baseline,
        "approved_spec_and_oracle": spec_and_oracle_before,
        "architecture_or_security_review": lambda: review("security"),
        "protected_acceptance_tests": protected_tests_ran,
        "security_or_e2e_evaluator": security_or_e2e,
        "explicit_human_approval": human_change,
        "release_observation_plan": release_plan,
        "rollback_or_forward_fix_evidence": rollback,
    }
    for gate in risk.GATES_BY_TIER[ch.tier]:
        gates[gate] = await checks[gate]()
    ready = all(g["status"] == SAT for g in gates.values())
    return {
        "verdict": "READY" if ready else "NOT_READY",
        "tier": ch.tier, "integration_head": head,
        "integration_branch": e.git.integration_branch(ch.id),
        "gates": gates,
        "waived_gates": sorted(ch.waivers),
        "human_judgement_gates": sorted(k for k, g in gates.items() if g["human_judgement"]),
        "note": "Readiness verdict only. Nothing was published, deployed or released.",
    }
