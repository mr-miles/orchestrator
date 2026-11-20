"""Deterministic ship readiness: evaluate every gate of the change's tier (plus added gates).

Each gate is ``satisfied``, ``unsatisfied`` or ``unevaluable`` (no evidence to evaluate).
Every attestation relied upon is re-resolved and git facts are recomputed live. A waived gate
is satisfied only while its replacement evidence resolves, and is reported as human judgement.
"""
from __future__ import annotations

from ..git import path_matches
from . import attest, governance
from .model import (
    ACCEPTED,
    BASELINE,
    H_CHANGE,
    INTEGRATION,
    RELEASE,
    ApprovalState,
    AttestationVerdict,
    GateResult,
    GateStatus,
    Oracle,
    OverrideKind,
    Readiness,
    ReadinessVerdict,
    Verdict,
    _parse_ts_safe,
)
from .view import ChangeView

SAT, UNSAT, UNEVAL = GateStatus.SATISFIED, GateStatus.UNSATISFIED, GateStatus.UNEVALUABLE


# gate name -> GateEvaluator method that evaluates it (filled by @checks)
GATE_CHECKS: dict[str, str] = {}


def checks(*gates: str):
    """Register the decorated GateEvaluator method as the check for ``gates``."""
    def deco(fn):
        for g in gates:
            if g in GATE_CHECKS:
                raise ValueError(f"gate {g!r} has two checks")
            GATE_CHECKS[g] = fn.__name__
        return fn
    return deco


def _g(status: str, detail: str, human: bool = False) -> GateResult:
    return GateResult(status=status, detail=detail, human_judgement=human)


class GateEvaluator:
    """Evaluates every gate of one change against live evidence at the integration head.

    One instance per evaluation: the facts every check shares (build nodes, H-2 status, head)
    are gathered once in the constructor. Each check is a method registered with ``@checks``.
    """

    def __init__(self, e: ChangeView, head: str):
        self.e, self.ch, self.head = e, e.change, head
        self.builds = e.build_nodes()
        self.task_ids = {b.task.id for b in self.builds}
        self.h2 = {s.subject: s for s in governance.h2_status(e)}
        self.h2_records = [self.ch.approvals.get(governance.approval_key(s)) for s in self.h2]

    # ---- shared helpers
    async def resolves(self, ref: str | None) -> bool:
        if not ref:
            return False
        try:
            return await attest.resolve(attest.parse(ref, [self.e.cfg.repo.name]),
                                        self.e.docs, self.e.git)
        except attest.AttestationError:
            return False

    def all_accepted(self) -> bool:
        return bool(self.builds) and all(b.status == ACCEPTED for b in self.builds)

    async def attested(self, kinds: list[str], *, need_before_build: bool = False) -> GateResult | None:
        """Evaluate human attestations of ``kinds`` (any pass suffices; any fail blocks)."""
        recs = [self.ch.attestations[k] for k in kinds if k in self.ch.attestations]
        if not recs:
            return None
        for r in recs:
            if not await self.resolves(r.attestation) or not all([await self.resolves(s) for s in r.sources]):
                return _g(UNSAT, f"{r.kind}: attestation or a source no longer resolves")
            if r.verdict == AttestationVerdict.FAIL:
                return _g(UNSAT, f"{r.kind} attested fail by {r.attested_by}")
        for r in recs:
            if r.verdict in (AttestationVerdict.PASS, AttestationVerdict.NOT_APPLICABLE):
                if not governance.coverage_ok(r, self.task_ids):
                    return _g(UNSAT, f"{r.kind}: covers {r.covers}, not every task")
                started = self.ch.round_first_build_at()
                if need_before_build and started and _parse_ts_safe(r.at) >= _parse_ts_safe(started):
                    return _g(UNSAT, f"{r.kind} recorded after execution started")
                how = "waived (not_applicable)" if r.verdict == AttestationVerdict.NOT_APPLICABLE else "pass"
                return _g(SAT, f"{r.kind} {how} by {r.attested_by}: {', '.join(r.sources)}", True)
        return _g(UNSAT, "attested verdict is unknown")

    # ---- contracts / specs
    @checks("current_task_contract_or_small_change_record", "task_contract")
    async def contract(self) -> GateResult:
        plan = self.h2.get("plan")
        if not plan or plan.status == ApprovalState.PENDING:
            return _g(UNEVAL, "task graph not approved (H-2 plan)")
        if plan.status != ApprovalState.APPROVED:
            return _g(UNSAT, "approved task graph is stale")
        rec = self.ch.approvals["spec:plan"]
        tg = self.e.json_doc("plan/taskgraph.json", rec.pins[self.e.p("plan/taskgraph.json")])
        ids = {t["id"] for t in tg["tasks"]}     # every round's tasks (a round appends its own)
        if ids != self.task_ids:
            return _g(UNSAT, f"build tasks {sorted(self.task_ids)} != contract {sorted(ids)}")
        return _g(SAT, f"{len(ids)} task contract(s) at {rec.attestation}")

    @checks("approved_delta_spec")
    async def delta_specs(self) -> GateResult:
        reqs = [s for s in self.h2 if s not in ("plan", "oracle")]
        if not reqs:
            return _g(UNEVAL, "no delta specs")
        bad = [f"{s}: {self.h2[s].status}" for s in reqs if self.h2[s].status != ApprovalState.APPROVED]
        if bad:
            return _g(UNSAT if any("stale" in b for b in bad) else UNEVAL, "; ".join(bad))
        for s in reqs:
            if not await self.resolves(self.ch.approvals[governance.approval_key(s)].attestation):
                return _g(UNSAT, f"{s}: approval record does not resolve")
        return _g(SAT, f"{len(reqs)} delta spec(s) approved against current bytes")

    @checks("approved_spec_and_oracle")
    async def spec_and_oracle_before(self) -> GateResult:
        if any(r is None for r in self.h2_records):
            return _g(UNEVAL, "spec/oracle not fully approved")
        if any(s.status != ApprovalState.APPROVED for s in self.h2.values()):
            return _g(UNSAT, "a spec/oracle approval is stale")
        # the current round's builds ran under these approvals; earlier rounds' builds were
        # dispatched only while every approval of their time was fresh (G-B1)
        last, started = governance.last_h2_approval_at(self.e), self.ch.round_first_build_at()
        if started and _parse_ts_safe(last) >= _parse_ts_safe(started):
            return _g(UNSAT, "last approval recorded after build started")
        return _g(SAT, f"last approval {last} before first build {started}")

    # ---- execution evidence
    @checks("deterministic_verification")
    async def deterministic(self) -> GateResult:
        if not self.all_accepted():
            return _g(UNEVAL if not self.builds else UNSAT, "not every build task is accepted")
        for b in self.builds:
            ver = b.current_verification()
            if not ver or not ver.ok or ver.sha != b.accepted_sha \
                    or not await self.resolves(ver.attestation):
                return _g(UNSAT, f"{b.id}: no passing verification at accepted sha")
        iv = self.e.current(INTEGRATION)
        if iv is not None:
            ver = iv.verification.get("integration")
            if not ver or not ver.ok or ver.sha != self.head:
                return _g(UNSAT, "no passing oracle verification at the integration head")
        return _g(SAT, "all verification commands passed at accepted commits")

    @checks("evidence_note", "evidence_bundle_or_log")
    async def evidence(self) -> GateResult:
        if not self.all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        for b in self.builds:
            run = self.e.runs.get(b.current_run)
            atts = run.report.attestations if run and run.report else []
            ev = [a for a in atts if a.startswith(f"file:{self.e.p('tasks/' + b.task.id + '/evidence')}/")]
            if not ev or not all([await self.resolves(a) for a in ev]):
                return _g(UNSAT, f"{b.id}: no resolvable evidence document")
        return _g(SAT, "every task has an attested evidence document")

    @checks("scoped_implementer_run")
    async def scoped(self) -> GateResult:
        if not self.all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        for b in self.builds:
            run = self.e.runs.get(b.current_run)
            approved = governance.approved_protected_for(self.e, b.task.id, run.created_at if run else "")
            prot = [p for p in governance.protected_paths(self.e) if p not in approved]
            allowed = b.task.files_modified + approved
            changed = await self.e.git.changed_paths(b.base_sha, b.accepted_sha)
            bad = [p for p in changed if not any(path_matches(p, a) for a in allowed)
                   or any(path_matches(p, x) for x in prot)]
            if bad:
                return _g(UNSAT, f"{b.id} changed out-of-scope paths {bad[:5]}")
        return _g(SAT, "every accepted diff stays inside files_modified (+ approved protected paths)")

    async def review(self, roles: list[str], label: str) -> GateResult:
        if not self.all_accepted():
            return _g(UNEVAL, "not every build task is accepted")
        human = False
        for b in self.builds:
            if b.override:
                human = True
                continue
            ok = False
            for role in roles:
                r = b.review(b.cycle, role)
                if r and r.verdict == Verdict.PASS and r.sha == b.accepted_sha and await self.resolves(r.doc):
                    ok = True
            if not ok:
                return _g(UNSAT, f"{b.id}: no independent {label} PASS at accepted sha")
        return _g(SAT, f"independent {label} PASS on every task" +
                  (" (some by human override)" if human else ""), human)

    @checks("lightweight_independent_review", "task_level_independent_review")
    async def code_review(self) -> GateResult:
        return await self.review(["reviewer"], "code review")

    # ---- oracle / surfaces
    @checks("protected_oracle")
    async def protected(self) -> GateResult:
        tests = (self.ch.oracle or Oracle()).acceptance_tests
        support = (self.ch.oracle or Oracle()).protected_support
        if not self.ch.oracle_sha or not tests:
            return _g(UNEVAL, "no oracle commit")
        approved = governance.all_approved_protected(self.e)
        changed = [p for p in tests + support
                   if await self.e.git.blob(self.head, p) != await self.e.git.blob(self.ch.oracle_sha, p)]
        unapproved = [p for p in changed if p not in approved]
        if unapproved:
            return _g(UNSAT, f"{unapproved} changed since oracle commit without approval")
        if changed:
            return _g(SAT, f"changed only with prior human approval: {changed}", True)
        return _g(SAT, f"{len(tests)} acceptance test(s) unchanged since {self.ch.oracle_sha[:12]}")

    @checks("protected_acceptance_tests")
    async def protected_tests_ran(self) -> GateResult:
        g = await self.protected()
        if g.status != SAT:
            return g
        iv = self.e.current(INTEGRATION)
        ver = iv.verification.get("integration") if iv else None
        cmds = (self.ch.oracle or Oracle()).acceptance_commands
        if not ver or ver.sha != self.head:
            return _g(UNEVAL, "acceptance commands not run at head")
        ran = {r.command: r.exit_code for r in ver.results}
        if any(ran.get(c) != 0 for c in cmds):
            return _g(UNSAT, "acceptance commands did not all pass at head")
        return _g(SAT, "protected acceptance tests pass at head", g.human_judgement)

    @checks("integration_or_real_interface_checks")
    async def integration(self) -> GateResult:
        iv = self.e.current(INTEGRATION)
        if not iv:
            return _g(UNEVAL, "no integration node")
        r = iv.reviews.get("integration")
        ver = iv.verification.get("integration")
        if iv.status != ACCEPTED or not r or r.verdict != Verdict.PASS or r.sha != self.head \
                or not ver or not ver.ok or ver.sha != self.head:
            return _g(UNSAT, "integration verification not passed at current head")
        if not governance.surfaces(self.e):
            return _g(UNEVAL, "no verification surface declared")
        drift = await governance.surface_drift(self.e, self.head)
        if drift:
            return _g(UNSAT, "; ".join(drift))
        ran = {x.command: x.exit_code for x in ver.results}
        missing = [c for c in governance.surface_commands(self.e) if ran.get(c) != 0]
        if missing:
            return _g(UNSAT, f"surface commands not passing at head: {missing}")
        n = len(governance.surfaces(self.e))
        return _g(SAT, f"integration PASS; {n} surface(s) pinned and exercised")

    # ---- human decisions
    @checks("whole_change_acceptance_evidence", "explicit_human_approval")
    async def human_change(self) -> GateResult:
        a = self.ch.approvals.get(H_CHANGE)
        if not a:
            return _g(UNEVAL, "change not accepted by a human (H-3)")
        if a.approver not in self.e.cfg.decision_owners or a.head != self.head \
                or not await self.resolves(a.attestation):
            return _g(UNSAT, "acceptance stale or approver not a decision owner")
        return _g(SAT, f"accepted by {a.approver} at {self.head[:12]}", True)

    @checks("independent_baseline")
    async def baseline(self) -> GateResult:
        g = await self.attested(["independent-baseline"], need_before_build=True)
        if g:
            return g
        n = self.e.current(BASELINE)
        return _g(UNEVAL, "no independent-baseline attestation" +
                  (" (baseline report exists; a human must attest it)" if n and n.status == ACCEPTED else ""))

    @checks("security_or_e2e_evaluator")
    async def security_or_e2e(self) -> GateResult:
        return await self.attested(["security-evaluation", "e2e-evaluation"]) or \
            _g(UNEVAL, "no security-evaluation or e2e-evaluation attestation")

    @checks("architecture_or_security_review")
    async def arch_or_security(self) -> GateResult:
        a = await self.attested(["architecture-review"])
        if a and a.status == UNSAT:
            return a
        r = await self.review(["architecture", "security"], "architecture/security review")
        if r.status == SAT:
            return r
        return a or r

    @checks("release_observation_plan")
    async def release_plan(self) -> GateResult:
        w = self.ch.attestations.get("release-observation")
        if w and w.verdict == AttestationVerdict.NOT_APPLICABLE:
            return await self.attested(["release-observation"])
        n = self.e.current(RELEASE)
        if not n or n.status != ACCEPTED:
            return _g(UNEVAL, "no release plan")
        if not await self.resolves(self.e.latest_att("release/plan.md")):
            return _g(UNSAT, "release plan missing")
        return _g(SAT, "release/plan.md attested")

    @checks("rollback_or_forward_fix_evidence")
    async def rollback(self) -> GateResult:
        return await self.attested(["rollback-evidence", "forward-fix-evidence"]) or \
            _g(UNEVAL, "no rollback-evidence or forward-fix-evidence attestation")

    # ---- the verdict
    async def gate(self, gate: str) -> GateResult:
        """One gate: its check, or – if a human waived it – whether the waiver still holds."""
        w = governance.waiver_for(self.e, gate)
        if w:
            ok = await self.resolves(w.evidence) and await self.resolves(w.attestation)
            res = _g(SAT if ok else UNSAT,
                     f"waived by {w.approver}: {w.reason} (evidence {w.evidence})", True)
        else:
            res = await getattr(self, GATE_CHECKS[gate])()
        res.added = any(o.kind == OverrideKind.ADD_GATE and o.gate == gate for o in self.ch.overrides)
        return res

    async def readiness(self) -> Readiness:
        gates = {g: await self.gate(g) for g in governance.gate_set(self.e)}
        waived = sorted(g for g, r in gates.items()
                        if r.detail.startswith("waived by ") or "not_applicable" in r.detail)
        return Readiness(
            verdict=ReadinessVerdict.READY if all(g.status == SAT for g in gates.values())
            else ReadinessVerdict.NOT_READY,
            tier=self.ch.tier, integration_head=self.head,
            integration_branch=self.e.git.integration_branch(self.ch.id), gates=gates,
            waived_gates=waived,
            human_judgement_gates=sorted(k for k, g in gates.items() if g.human_judgement))


async def evaluate(e: ChangeView) -> Readiness:
    return await GateEvaluator(e, await e.git.integration_head(e.change.id)).readiness()
