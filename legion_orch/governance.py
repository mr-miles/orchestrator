"""Human governance records: spec/oracle approvals (H-2), attestations, protected-path approvals,
verification-surface pins and gate overrides. Mirrors legion-ascended `approve`, `attest` and the
risk-override model. Every record is a versioned document under changes/<id>/control/."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import attest, risk
from .util import now_iso
from .git import path_matches

if TYPE_CHECKING:
    from .engine import ChangeEngine

ATTESTATION_KINDS = {
    "independent-baseline", "security-evaluation", "e2e-evaluation", "architecture-review",
    "rollback-evidence", "forward-fix-evidence", "release-observation",
}
VERDICTS = {"pass", "fail", "unknown", "not_applicable"}
# Gates a human may never waive: the machine check or the human decision *is* the gate.
NON_WAIVABLE = {"deterministic_verification", "explicit_human_approval",
                "whole_change_acceptance_evidence"}
ALL_GATES = {g for gates in risk.GATES_BY_TIER.values() for g in gates}


class GovernanceError(Exception):
    pass


# ------------------------------------------------------------------ H-2 subjects
def h2_subjects(e: "ChangeEngine") -> dict[str, dict[str, int | None]]:
    """Subject -> {qualified path: current latest version}. One subject per delta spec."""
    subjects = {"plan": {e.p("plan/spec.md"): e._latest("plan/spec.md"),
                         e.p("plan/taskgraph.json"): e._latest("plan/taskgraph.json")}}
    for req in e.change.requirements:
        subjects[req] = {e.p(f"plan/specs/{req}.md"): e._latest(f"plan/specs/{req}.md")}
    subjects["oracle"] = {e.p("plan/oracle.json"): e._latest("plan/oracle.json")}
    return subjects


def approval_key(subject: str) -> str:
    return "oracle" if subject == "oracle" else f"spec:{subject}"


def h2_status(e: "ChangeEngine") -> list[dict]:
    out = []
    for subj, pins in h2_subjects(e).items():
        a = e.change.approvals.get(approval_key(subj))
        fresh = bool(a) and a["pins"] == pins and (
            subj != "oracle" or a.get("oracle_sha") == e.change.oracle_sha)
        out.append({"subject": subj, "paths": list(pins), "status":
                    "approved" if fresh else ("stale" if a else "pending"),
                    "approver": a["approver"] if a else None, "at": a["at"] if a else None})
    return out


def stale_approvals(e: "ChangeEngine") -> list[str]:
    """Guard G-B1: every H-2 subject approved against the current bytes."""
    return [f"G-B1: {s['subject']} {s['status']}" for s in h2_status(e) if s["status"] != "approved"]


async def approve_subjects(e: "ChangeEngine", subjects: list[str] | str, approver: str) -> list[str]:
    available = h2_subjects(e)
    if subjects == "all" or subjects == ["all"]:
        subjects = list(available)
    unknown = [s for s in subjects if s not in available]
    if unknown:
        raise GovernanceError(f"unknown approval subject(s) {unknown}; choose from {list(available)}")
    done = []
    for subj in subjects:
        pins = available[subj]
        missing = [p for p, v in pins.items() if v is None]
        if missing:
            raise GovernanceError(f"{subj}: nothing to approve, missing {missing}")
        rec: dict[str, Any] = {"gate": "H-2", "subject": subj, "approver": approver, "at": now_iso(),
                               "pins": pins}
        if subj == "oracle":
            rec["oracle_sha"] = e.change.oracle_sha
        rec["attestation"] = e._cwrite(f"control/approvals/{approval_key(subj).replace(':', '/')}.json",
                                       rec).attestation
        e.change.approvals[approval_key(subj)] = rec
        if subj == "oracle":
            e.change.oracle = e._json_doc("plan/oracle.json")
            await pin_surfaces(e, approver, None, reason="initial pin at oracle approval")
        done.append(subj)
    return done


def last_h2_approval_at(e: "ChangeEngine") -> str | None:
    ats = [e.change.approvals[approval_key(s)]["at"] for s in h2_subjects(e)
           if approval_key(s) in e.change.approvals]
    return max(ats) if ats else None


# ------------------------------------------------------------------ attestations
def coverage_ok(rec: dict, task_ids: set[str]) -> bool:
    return rec.get("covers") is None or task_ids <= set(rec["covers"])


async def record_attestation(e: "ChangeEngine", attested_by: str, payload: dict) -> dict:
    """Guard G-AT1."""
    kind, verdict = payload.get("kind"), payload.get("verdict")
    if kind not in ATTESTATION_KINDS:
        raise GovernanceError(f"G-AT1: unknown attestation kind {kind!r}; one of {sorted(ATTESTATION_KINDS)}")
    if verdict not in VERDICTS:
        raise GovernanceError(f"G-AT1: verdict must be one of {sorted(VERDICTS)}")
    if kind == "release-observation" and verdict == "pass":
        raise GovernanceError("G-AT1: release-observation cannot be attested as pass – the release "
                              "plan is the evidence; only not_applicable (a waiver) is accepted")
    reason = payload.get("waiver_reason")
    if verdict == "not_applicable" and not risk.valid_reason(reason):
        raise GovernanceError("G-AT1: not_applicable needs a waiver_reason of ≥24 chars and more than one word")
    if verdict != "not_applicable" and reason:
        raise GovernanceError("G-AT1: waiver_reason is only accepted with verdict not_applicable")
    sources = [attest.normalize(s, e.cid) for s in payload.get("sources") or [] if s]
    if not sources:
        raise GovernanceError("G-AT1: at least one source attestation is required (a waiver still "
                              "names the bytes it is about)")
    for s in sources:
        try:
            ok = await attest.resolve(attest.parse(s, [e.cfg.repo.name]), e.docs, e.git)
        except attest.AttestationError as ex:
            raise GovernanceError(f"G-AT1: {ex}")
        if not ok:
            raise GovernanceError(f"G-AT1: source does not resolve: {s}")
    covers = payload.get("covers") or None
    tasks = {b.task["id"] for b in e._build_nodes()}
    if covers is not None:
        unknown = set(covers) - tasks
        if unknown:
            raise GovernanceError(f"G-AT1: covers unknown task(s) {sorted(unknown)}")
    warnings = []
    prev = e.change.attestations.get(kind)
    if prev and prev["verdict"] != verdict:
        warnings.append(f"attestation_verdict_superseded: {prev['verdict']} -> {verdict}")
    if covers is not None and tasks - set(covers):
        warnings.append(f"attestation_partial_coverage: missing {sorted(tasks - set(covers))}")
    if kind == "independent-baseline" and e.change.first_build_at:
        warnings.append("attestation_after_execution: a task run already started, so "
                        "independent_baseline can no longer be satisfied for this change")
    rec = {"kind": kind, "verdict": verdict, "attested_by": attested_by, "at": now_iso(),
           "sources": sources, "covers": covers, "waiver_reason": reason,
           "supersedes": prev["attestation"] if prev else None, "warnings": warnings}
    rec["attestation"] = e._cwrite(f"control/attestations/{kind}.json", rec).attestation
    e.change.attestations[kind] = rec
    return {"ok": True, "attestation": rec["attestation"], "warnings": warnings}


# ------------------------------------------------------------------ protected paths
def record_protected_approval(e: "ChangeEngine", approver: str, payload: dict) -> dict:
    task, paths, reason = payload.get("task"), payload.get("paths") or [], payload.get("reason")
    builds = {b.task["id"]: b for b in e._build_nodes()}
    if task not in builds:
        raise GovernanceError(f"unknown task {task!r}")
    protected = protected_paths(e)
    bad = [p for p in paths if not any(path_matches(p, x) for x in protected)]
    if not paths or bad:
        raise GovernanceError(f"paths must be protected acceptance paths {protected}; got {bad or paths}")
    if not risk.valid_reason(reason):
        raise GovernanceError("a reason of ≥24 chars and more than one word is required")
    rec = {"task": task, "paths": paths, "approver": approver, "at": now_iso(), "reason": reason}
    n = len(e.change.protected_approvals) + 1
    rec["attestation"] = e._cwrite(f"control/approvals/protected-paths/{n}.json", rec).attestation
    e.change.protected_approvals.append(rec)
    return {"ok": True, "attestation": rec["attestation"],
            "note": "applies only to runs of this task that start after now"}


def protected_paths(e: "ChangeEngine") -> list[str]:
    return list((e.change.oracle or {}).get("acceptance_tests", [])) + list(e.cfg.protected_paths)


def approved_protected_for(e: "ChangeEngine", task_id: str, before: str) -> list[str]:
    """Guard G-PP1: protected paths this task may touch in a run created at ``before``."""
    return [p for a in e.change.protected_approvals if a["task"] == task_id and a["at"] < before
            for p in a["paths"]]


def all_approved_protected(e: "ChangeEngine") -> list[str]:
    return [p for a in e.change.protected_approvals for p in a["paths"]]


# ------------------------------------------------------------------ verification surfaces
def surfaces(e: "ChangeEngine") -> list[dict]:
    return list((e.change.oracle or {}).get("surfaces", []))


async def pin_surfaces(e: "ChangeEngine", approver: str, surface_id: str | None, reason: str) -> list[str]:
    """Pin (or re-affirm) the files a verification surface relies on at the integration head."""
    head = e.change.integration_head
    chosen = [s for s in surfaces(e) if surface_id in (None, s["id"])]
    if surface_id and not chosen:
        raise GovernanceError(f"unknown surface {surface_id!r}")
    for s in chosen:
        pins = {p: await e.git.blob(head, p) for p in s.get("pins", [])}
        rec = {"surface": s["id"], "pins": pins, "head": head, "approver": approver,
               "at": now_iso(), "reason": reason}
        rec["attestation"] = e._cwrite(f"control/surfaces/{s['id']}.json", rec).attestation
        e.change.surface_pins[s["id"]] = rec
    return [s["id"] for s in chosen]


async def surface_drift(e: "ChangeEngine", head: str) -> list[str]:
    """Guard G-SF1: pinned files unchanged at ``head`` (or re-affirmed)."""
    v = []
    for s in surfaces(e):
        rec = e.change.surface_pins.get(s["id"])
        if not rec:
            v.append(f"G-SF1: surface {s['id']} was never pinned")
            continue
        for path, blob in rec["pins"].items():
            if await e.git.blob(head, path) != blob:
                v.append(f"G-SF1: surface {s['id']} pinned file {path} changed since it was pinned; "
                         "a human must re-affirm the surface")
    return v


def surface_commands(e: "ChangeEngine") -> list[str]:
    return [c for s in surfaces(e) for c in s.get("commands", [])]


# ------------------------------------------------------------------ overrides
async def record_override(e: "ChangeEngine", approver: str, payload: dict) -> dict:
    """Guard G-OV1."""
    kind, gate, reason = payload.get("kind"), payload.get("gate"), payload.get("reason")
    if kind not in ("add_gate", "waive_gate"):
        raise GovernanceError("G-OV1: kind must be add_gate or waive_gate (tier changes happen at H-1)")
    if gate not in ALL_GATES:
        raise GovernanceError(f"G-OV1: unknown gate {gate!r}")
    if not risk.valid_reason(reason):
        raise GovernanceError("G-OV1: a reason of ≥24 chars and more than one word is required")
    rec: dict[str, Any] = {"kind": kind, "gate": gate, "reason": reason, "approver": approver,
                           "at": now_iso()}
    if kind == "waive_gate":
        if gate in NON_WAIVABLE:
            raise GovernanceError(f"G-OV1: {gate} cannot be waived")
        evidence = attest.normalize(payload.get("evidence") or "", e.cid)
        try:
            ok = await attest.resolve(attest.parse(evidence, [e.cfg.repo.name]), e.docs, e.git)
        except attest.AttestationError as ex:
            raise GovernanceError(f"G-OV1: replacement evidence: {ex}")
        if not ok:
            raise GovernanceError(f"G-OV1: replacement evidence does not resolve: {evidence}")
        rec["evidence"] = evidence
    n = len(e.change.overrides) + 1
    rec["attestation"] = e._cwrite(f"control/overrides/{n}.json", rec).attestation
    e.change.overrides.append(rec)
    return {"ok": True, "attestation": rec["attestation"]}


def gate_set(e: "ChangeEngine") -> list[str]:
    gates = list(risk.GATES_BY_TIER[e.change.tier])
    for o in e.change.overrides:
        if o["kind"] == "add_gate" and o["gate"] not in gates:
            gates.append(o["gate"])
    return gates


def waiver_for(e: "ChangeEngine", gate: str) -> dict | None:
    ws = [o for o in e.change.overrides if o["kind"] == "waive_gate" and o["gate"] == gate]
    return ws[-1] if ws else None
