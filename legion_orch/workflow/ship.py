"""Deterministic ship readiness: evaluate every gate of the change's tier (plus added gates).

Each gate is ``satisfied``, ``unsatisfied`` or ``unevaluable`` (no evidence to evaluate).
Every attestation relied upon is re-resolved and git facts are recomputed live. A waived gate
is satisfied only while its replacement evidence resolves, and is reported as human judgement.
"""
from __future__ import annotations

from typing import Any

from ..git import path_matches
from . import attest, governance
from .model import ACCEPTED, BASELINE, H_CHANGE, INTEGRATION, RELEASE, _parse_ts_safe
from .view import ChangeView

SAT, UNSAT, UNEVAL = "satisfied", "unsatisfied", "unevaluable"


def _g(status: str, detail: str, human: bool = False) -> dict:
    return {"status": status, "detail": detail, "human_judgement": human}


async def evaluate(e: ChangeView) -> dict[str, Any]:
    ch = e.change
    builds = e.build_nodes()
    task_ids = {b.task["id"] for b in builds}
    head = await e.git.integration_head(ch.id)
    repo = e.cfg.repo.name

    async def resolves(ref: str | None) -> bool:
        if not ref:
            return False
        try:
            return await attest.resolve(attest.parse(ref, [repo]), e.docs, e.git)
        except attest.AttestationError:
            return False

    h2 = {s["subject"]: s for s in governance.h2_status(e)}
    h2_records = [ch.approvals.get(governance.approval_key(s)) for s in h2]

    def all_accepted() -> bool:
        return bool(builds) and all(b.status == ACCEPTED for b in builds)

    async def attested(kinds: list[str], *, need_before_build: bool = False) -> dict | None:
        """Evaluate human attestations of ``kinds`` (any pass suffices; any fail blocks)."""
        recs = [ch.attestations[k] for k in kinds if k in ch.attestations]
        if not recs:
            return None
        for r in recs:
            if not await resolves(r["attestation"]) or not all([await resolves(s) for s in r["sources"]]):
                return _g(UNSAT, f"{r['kind']}: attestation or a source no longer resolves")
            if r["verdict"] == "fail":
                return _g(UNSAT, f"{r['kind']} attested fail by {r['attested_by']}")
        for r in recs:
            if r["verdict"] in ("pass", "not_applicable"):
                if not governance.coverage_ok(r, task_ids):
                    return _g(UNSAT, f"{r['kind']}: covers {r['covers']}, not every task")
                if need_before_build and ch.first_build_at and \
                        _parse_ts_safe(r["at"]) >= _parse_ts_safe(ch.first_build_at):
                    return _g(UNSAT, f"{r['kind']} recorded after execution started")
                how = "waived (not_applicable)" if r["verdict"] == "not_applicable" else "pass"
                return _g(SAT, f"{r['kind']} {how} by {r['attested_by']}: {', '.join(r['sources'])}", True)
        return _g(UNSAT, "attested verdict is unknown")

    # ---- contracts / specs
    async def contract() -> dict:
        plan = h2.get("plan")
        if not plan or plan["status"] == "pending":
            return _g(UNEVAL, "task graph not approved (H-2 plan)")
        if plan["status"] != "approved":
            return _g(UNSAT, "approved task graph is stale")
        rec = ch.approvals["spec:plan"]
        tg = e.json_doc("plan/taskgraph.json", rec["pins"][e.p("plan/taskgraph.json")])
        ids = {t["id"] for t in tg["tasks"]}
        if ids != task_ids:
            return _g(UNSAT, f"build tasks {sorted(task_ids)} != contract {sorted(ids)}")
        return _g(SAT, f"{len(ids)} task contract(s) at {rec['attestation']}")

    async def delta_specs() -> dict:
        reqs = [s for s in h2 if s not in ("plan", "oracle")]
        if not reqs:
            return _g(UNEVAL, "no delta specs")
        bad = [f"{s}: {h2[s]['status']}" for s in reqs if h2[s]["status"] != "approved"]
        if bad:
            return _g(UNSAT if any("stale" in b for b in bad) else UNEVAL, "; ".join(bad))
        for s in reqs:
            if not await resolves(ch.approvals[governance.approval_key(s)]["attestation"]):
                return _g(UNSAT, f"{s}: approval record does not resolve")
        return _g(SAT, f"{len(reqs)} delta spec(s) approved against current bytes")

    async def spec_and_oracle_before() -> dict:
        if any(r is None for r in h2_records):
            return _g(UNEVAL, "spec/oracle not fully approved")
        if any(s["status"] != "approved" for s in h2.values()):
            return _g(UNSAT, "a spec/oracle approval is stale")
        last = governance.last_h2_approval_at(e)
        if ch.first_build_at and _parse_ts_safe(last) >= _parse_ts_safe(ch.first_build_at):
            return _g(UNSAT, "last approval recorded after build started")
        return _g(SAT, f"last approval {last} before first build {ch.first_build_at}")

    # ---- execution evidence
    async def deterministic() -> dict:
        if not all_accepted():
            return _g(UNEVAL if not builds else UNSAT, "not every build task is accepted")
        for b in builds:
            ver = b.verification.get(str(b.cycle))
            if not ver or not ver["ok"] or ver["sha"] != b.accepted_sha \
                    or not await resolves(ver.get("attestation")):
                return _g(UNSAT, f"{b.id}: no passing verification at accepted sha")
        iv = e.nodes.get(INTEGRATION)
        if iv is not None:
            ver = iv.verification.get("integration")
            if not ver or not ver["ok"] or ver["sha"] != head:
                return _g(UNSAT, "no passing oracle verification at the integration head")
        return _g(SAT, "all verification commands passed at accepted commits")

    async def evidence() -> dict:
        if not all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        for b in builds:
            run = e.runs.get(b.current_run)
            atts = (run.report or {}).get("attestations", []) if run else []
            ev = [a for a in atts if a.startswith(f"file:{e.p('tasks/' + b.task['id'] + '/evidence')}/")]
            if not ev or not all([await resolves(a) for a in ev]):
                return _g(UNSAT, f"{b.id}: no resolvable evidence document")
        return _g(SAT, "every task has an attested evidence document")

    async def scoped() -> dict:
        if not all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        for b in builds:
            run = e.runs.get(b.current_run)
            approved = governance.approved_protected_for(e, b.task["id"], run.created_at if run else "")
            prot = [p for p in governance.protected_paths(e) if p not in approved]
            allowed = b.task["files_modified"] + approved
            changed = await e.git.changed_paths(b.base_sha, b.accepted_sha)
            bad = [p for p in changed if not any(path_matches(p, a) for a in allowed)
                   or any(path_matches(p, x) for x in prot)]
            if bad:
                return _g(UNSAT, f"{b.id} changed out-of-scope paths {bad[:5]}")
        return _g(SAT, "every accepted diff stays inside files_modified (+ approved protected paths)")

    async def review(roles: list[str], label: str) -> dict:
        if not all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        human = False
        for b in builds:
            if (b.human or {}).get("override"):
                human = True
                continue
            ok = False
            for role in roles:
                r = b.reviews.get(f"{b.cycle}:{role}")
                if r and r["verdict"] == "PASS" and r["sha"] == b.accepted_sha and await resolves(r["doc"]):
                    ok = True
            if not ok:
                return _g(UNSAT, f"{b.id}: no independent {label} PASS at accepted sha")
        return _g(SAT, f"independent {label} PASS on every task" +
                  (" (some by human override)" if human else ""), human)

    # ---- oracle / surfaces
    async def protected() -> dict:
        tests = (ch.oracle or {}).get("acceptance_tests", [])
        support = (ch.oracle or {}).get("protected_support", [])
        if not ch.oracle_sha or not tests:
            return _g(UNEVAL, "no oracle commit")
        approved = governance.all_approved_protected(e)
        changed = [p for p in tests + support
                   if await e.git.blob(head, p) != await e.git.blob(ch.oracle_sha, p)]
        unapproved = [p for p in changed if p not in approved]
        if unapproved:
            return _g(UNSAT, f"{unapproved} changed since oracle commit without approval")
        if changed:
            return _g(SAT, f"changed only with prior human approval: {changed}", True)
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
        return _g(SAT, "protected acceptance tests pass at head", g["human_judgement"])

    async def integration() -> dict:
        iv = e.nodes.get(INTEGRATION)
        if not iv:
            return _g(UNEVAL, "no integration node")
        r = iv.reviews.get("integration")
        ver = iv.verification.get("integration")
        if iv.status != ACCEPTED or not r or r["verdict"] != "PASS" or r["sha"] != head \
                or not ver or not ver["ok"] or ver["sha"] != head:
            return _g(UNSAT, "integration verification not passed at current head")
        if not governance.surfaces(e):
            return _g(UNEVAL, "no verification surface declared")
        drift = await governance.surface_drift(e, head)
        if drift:
            return _g(UNSAT, "; ".join(drift))
        ran = {x["command"]: x["exit_code"] for x in ver["results"]}
        missing = [c for c in governance.surface_commands(e) if ran.get(c) != 0]
        if missing:
            return _g(UNSAT, f"surface commands not passing at head: {missing}")
        return _g(SAT, f"integration PASS; {len(governance.surfaces(e))} surface(s) pinned and exercised")

    # ---- human decisions
    async def human_change() -> dict:
        a = ch.approvals.get(H_CHANGE)
        if not a:
            return _g(UNEVAL, "change not accepted by a human (H-3)")
        if a["approver"] not in e.cfg.decision_owners or a["head"] != head \
                or not await resolves(a["attestation"]):
            return _g(UNSAT, "acceptance stale or approver not a decision owner")
        return _g(SAT, f"accepted by {a['approver']} at {head[:12]}", True)

    async def baseline() -> dict:
        g = await attested(["independent-baseline"], need_before_build=True)
        if g:
            return g
        n = e.nodes.get(BASELINE)
        return _g(UNEVAL, "no independent-baseline attestation" +
                  (" (baseline report exists; a human must attest it)" if n and n.status == ACCEPTED else ""))

    async def security_or_e2e() -> dict:
        return await attested(["security-evaluation", "e2e-evaluation"]) or \
            _g(UNEVAL, "no security-evaluation or e2e-evaluation attestation")

    async def arch_or_security() -> dict:
        a = await attested(["architecture-review"])
        if a and a["status"] == UNSAT:
            return a
        r = await review(["architecture", "security"], "architecture/security review")
        if r["status"] == SAT:
            return r
        return a or r

    async def release_plan() -> dict:
        w = ch.attestations.get("release-observation")
        if w and w["verdict"] == "not_applicable":
            return await attested(["release-observation"])
        n = e.nodes.get(RELEASE)
        if not n or n.status != ACCEPTED:
            return _g(UNEVAL, "no release plan")
        return _g(SAT, "release/plan.md attested") if await resolves(e.latest_att("release/plan.md")) \
            else _g(UNSAT, "release plan missing")

    async def rollback() -> dict:
        return await attested(["rollback-evidence", "forward-fix-evidence"]) or \
            _g(UNEVAL, "no rollback-evidence or forward-fix-evidence attestation")

    checks = {
        "current_task_contract_or_small_change_record": contract,
        "task_contract": contract,
        "deterministic_verification": deterministic,
        "evidence_note": evidence,
        "evidence_bundle_or_log": evidence,
        "scoped_implementer_run": scoped,
        "lightweight_independent_review": lambda: review(["reviewer"], "code review"),
        "task_level_independent_review": lambda: review(["reviewer"], "code review"),
        "approved_delta_spec": delta_specs,
        "protected_oracle": protected,
        "integration_or_real_interface_checks": integration,
        "whole_change_acceptance_evidence": human_change,
        "independent_baseline": baseline,
        "approved_spec_and_oracle": spec_and_oracle_before,
        "architecture_or_security_review": arch_or_security,
        "protected_acceptance_tests": protected_tests_ran,
        "security_or_e2e_evaluator": security_or_e2e,
        "explicit_human_approval": human_change,
        "release_observation_plan": release_plan,
        "rollback_or_forward_fix_evidence": rollback,
    }
    gates: dict[str, dict] = {}
    waived = []
    for gate in governance.gate_set(e):
        w = governance.waiver_for(e, gate)
        if w:
            ok = await resolves(w["evidence"]) and await resolves(w["attestation"])
            gates[gate] = _g(SAT if ok else UNSAT,
                             f"waived by {w['approver']}: {w['reason']} (evidence {w['evidence']})", True)
            waived.append(gate)
        else:
            gates[gate] = await checks[gate]()
        gates[gate]["added"] = any(o["kind"] == "add_gate" and o["gate"] == gate for o in ch.overrides)
    waived += [g for g, r in gates.items() if "not_applicable" in r["detail"] and g not in waived]
    ready = all(g["status"] == SAT for g in gates.values())
    return {
        "verdict": "READY" if ready else "NOT_READY",
        "tier": ch.tier, "integration_head": head,
        "integration_branch": e.git.integration_branch(ch.id),
        "gates": gates,
        "waived_gates": sorted(waived),
        "human_judgement_gates": sorted(k for k, g in gates.items() if g["human_judgement"]),
        "note": "Readiness verdict only. Nothing was published, deployed or released.",
    }
