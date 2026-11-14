"""Workflow guards. Every guard id documented in WORKFLOW.md is registered here and used by
the engine / MCP layer. Guards return lists of violation strings prefixed with the guard id."""
from __future__ import annotations

import json
import re
from typing import Any

from ..git import path_matches, paths_overlap
from ..store import DocStore, SchemaRegistry
from . import risk
from .attest import Attestation

GUARDS: dict[str, str] = {
    # universal
    "G-D1": "dispatch: all dependencies ACCEPTED",
    "G-D2": "dispatch: assets resolve, variables bound, prompt pin recorded",
    "G-D3": "dispatch: parallel limit and no files_modified overlap with running build nodes",
    "G-D4": "dispatch: judges run in fresh sandboxes distinct from the judged run",
    "G-R1": "report: status is DONE or BLOCKED",
    "G-R2": "report: BLOCKED needs blocked_reason and missing_decision",
    "G-R3": "report: every attestation parses and resolves",
    "G-R4": "report: required docs attested at latest version, authored by this run",
    "G-R5": "report: required git attestation present",
    "G-R6": "report: too many rejected reports fails the run",
    "G-W1": "docs_write: inside write scope, never control/",
    "G-W2": "docs_write: base_version equals latest",
    "G-W3": "store write: content matches the schema bound to the path",
    "G-R7": "report: data matches the role's report schema",
    "G-R8": "report: size limits (summary, reasons, attestations, data) and check time limit",
    # intake
    "G-I1": "requirements contain unique REQ ids",
    "G-I2": "risk.json has all 12 signals with scores and rationale",
    "G-I3": "open questions answered or run BLOCKED",
    "H-1": "human accepts requirements",
    # plan / critique / oracle / baseline
    "G-P1": "taskgraph/oracle schema",
    "G-P2": "unique ids, known deps, acyclic",
    "G-P3": "requirement traceability",
    "G-P4": "verification + acceptance per task",
    "G-P5": "implementer souls only",
    "G-P6": "valid paths, parallel tasks disjoint",
    "G-P7": "no protected paths in files_modified",
    "G-P8": "R2+ oracle completeness",
    "G-P9": "a delta spec per accepted requirement",
    "G-P10": "R2+ oracle declares verification surfaces",
    "G-C1": "critique verdict and findings",
    "G-C2": "critique cycle budget",
    "G-O1": "oracle diff only touches acceptance tests",
    "G-O2": "acceptance tests exist at oracle commit",
    "G-BL1": "baseline results cover oracle commands at head",
    "H-2": "human approves spec and oracle (pinned)",
    # build
    "G-B1": "build dispatch requires fresh spec/oracle approval",
    "G-B2": "commit is the pushed tip of the run branch and descends from base",
    "G-B3": "scoped diff",
    "G-B4": "evidence lists verification commands",
    "G-PP1": "protected paths changed only after a prior human approval for that task",
    "G-V1": "deterministic verification passes",
    "G-RV1": "review attests candidate sha",
    "G-RV2": "finding format",
    "G-RV3": "verdict consistent with findings",
    "G-RV4": "first-cycle PASS lists what was checked",
    "G-RV5": "review cycle budget and stale-loop abort",
    "G-A1": "verification + full panel PASS at same sha",
    "G-A2": "clean merge into integration",
    # integration / release / acceptance
    "G-INT1": "integration verdict at head covering all commands",
    "G-INT2": "protected acceptance tests unchanged since oracle commit",
    "G-REL1": "release plan and rollback documents complete",
    "H-3": "human accepts the change at the integration head",
    "G-AT1": "attestation shape: kind, verdict, sources, waiver rules, coverage",
    "G-SF1": "verification surface pins unchanged (or re-affirmed)",
    "G-OV1": "gate overrides: add or waive with reason and replacement evidence",
}

REQ_RE = re.compile(r"\bREQ-([0-9]+)\b")
MAX_REQUIREMENTS = 100
SEVERITIES = {"BLOCKER", "WARNING", "SUGGESTION"}
CONFIDENCES = {"HIGH", "MEDIUM", "LOW"}


def load_json(docs: DocStore, att: Attestation | None, gid: str) -> tuple[Any, list[str]]:
    if att is None:
        return None, [f"{gid}: document not attested"]
    try:
        return json.loads(docs.read(att.path, att.version).content), []
    except json.JSONDecodeError as e:
        return None, [f"{gid}: {att} is not valid JSON: {e}"]


# ---------------------------------------------------------------- universal
MAX_TEXT = 2000
MAX_ATTESTATIONS = 100
MAX_DATA_BYTES = 256 * 1024


def report_limits(summary: Any, attestations: Any, data: Any, blocked_reason: Any,
                  missing_decision: Any) -> list[str]:
    """Guard G-R8: bound everything a report can make the controller store or process."""
    v = []
    for name, val in (("summary", summary), ("blocked_reason", blocked_reason),
                      ("missing_decision", missing_decision)):
        if val is not None and (not isinstance(val, str) or len(val) > MAX_TEXT):
            v.append(f"G-R8: {name} must be a string of at most {MAX_TEXT} characters")
    if not isinstance(attestations, list) or len(attestations) > MAX_ATTESTATIONS \
            or not all(isinstance(a, str) and len(a) <= 600 for a in attestations):
        v.append(f"G-R8: attestations must be a list of at most {MAX_ATTESTATIONS} strings")
    try:
        size = len(json.dumps(data))
    except (TypeError, ValueError):
        return v + ["G-R8: data must be JSON"]
    if size > MAX_DATA_BYTES:
        v.append(f"G-R8: data is {size} bytes; at most {MAX_DATA_BYTES}")
    return v


def report_shape(status: str, blocked_reason: str | None, missing_decision: str | None) -> list[str]:
    v = []
    if status not in ("DONE", "BLOCKED"):
        v.append(f"G-R1: status must be DONE or BLOCKED (got {status!r}); deferral is not allowed")
    if status == "BLOCKED" and not ((blocked_reason or "").strip() and (missing_decision or "").strip()):
        v.append("G-R2: BLOCKED requires blocked_reason and missing_decision")
    return v


def required_docs(run_id: str, required: list[str], atts: list[Attestation],
                  docs: DocStore) -> tuple[dict[str, Attestation], list[str]]:
    found: dict[str, Attestation] = {}
    v = []
    for path in required:
        cands = [a for a in atts if a.kind == "file" and a.path == path]
        if not cands:
            v.append(f"G-R4: required document {path} not attested")
            continue
        att = max(cands, key=lambda a: a.version)
        latest = docs.latest_version(path)
        if att.version != latest:
            v.append(f"G-R4: {att} is not the latest version (latest {latest})")
            continue
        if docs.read(path, att.version).author != run_id:
            v.append(f"G-R4: {att} was not written by this run")
            continue
        found[path] = att
    return found, v


def required_git(mode: str, atts: list[Attestation], repo: str,
                 judged_sha: str | None) -> tuple[str | None, list[str]]:
    if mode == "none":
        return None, []
    commits = [a.sha for a in atts if a.kind == "git" and a.path is None and a.repo == repo]
    if mode == "commit":
        if len(set(commits)) != 1:
            return None, [f"G-R5: attest exactly one commit as git:{repo}@<sha> (got {len(set(commits))})"]
        return commits[0], []
    # candidate / head: must attest exactly the sha under judgement
    if judged_sha not in commits:
        gid = "G-RV1" if mode == "candidate" else "G-R5"
        return None, [f"{gid}: must attest git:{repo}@{judged_sha} (the {mode} under judgement)"]
    return judged_sha, []


# ---------------------------------------------------------------- intake
def requirements_ids(text: str) -> list[str]:
    ids = []
    for line in text.splitlines():
        m = re.match(r"^\s*(?:[-*]|#+)?\s*\**(REQ-[0-9]+)\b", line)
        if m:
            ids.append(m.group(1))
    return ids


def intake(req_text: str, risk_json: Any, questions_text: str | None, status: str) -> list[str]:
    v = []
    ids = requirements_ids(req_text)
    if not ids:
        v.append("G-I1: requirements.md must define at least one REQ-<n> (one per line/heading)")
    dups = sorted({i for i in ids if ids.count(i) > 1})
    if dups:
        v.append(f"G-I1: duplicate requirement ids {dups}")
    if len(set(ids)) > MAX_REQUIREMENTS:
        v.append(f"G-I1: {len(set(ids))} requirements; at most {MAX_REQUIREMENTS} per change – "
                 "split the goal into several changes")
    v += risk.validate_signals(risk_json)
    if questions_text and status == "DONE":
        open_q = [ln.strip() for ln in questions_text.splitlines() if re.match(r"^\s*[-*]\s*\[ \]", ln)]
        if open_q:
            v.append(f"G-I3: {len(open_q)} unanswered question(s) in intake/questions.md – "
                     "answer them or report BLOCKED with the question as missing_decision")
    return v


# ---------------------------------------------------------------- plan
def _reachable(graph: dict[str, list[str]], start: str) -> set[str]:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        for d in graph.get(n, []):
            if d not in seen:
                seen.add(d)
                stack.append(d)
    return seen


def plan(taskgraph: Any, oracle: Any, requirements: list[str], tier: str,
         protected_cfg: list[str], implementer_souls: list[str], schemas: SchemaRegistry) -> list[str]:
    """Cross-document plan rules. Shape (G-P1) is the ``taskgraph`` / ``oracle`` JSON Schemas –
    the same ones the store enforces on write – so they are defined in exactly one place."""
    v = [f"G-P1: taskgraph.json {e}" for e in schemas.validate_data("taskgraph", taskgraph)]
    v += [f"G-P1: oracle.json {e}" for e in schemas.validate_data("oracle", oracle)]
    if v:
        return v
    tasks = taskgraph["tasks"]
    ids = [t["id"] for t in tasks]
    # G-P2
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        v.append(f"G-P2: duplicate task ids {dup}")
    graph = {t["id"]: list(t["depends_on"]) for t in tasks}
    for tid, deps in graph.items():
        for d in deps:
            if d not in graph:
                v.append(f"G-P2: task {tid} depends on unknown task {d}")
    if not v:
        for tid in graph:
            if tid in _reachable(graph, tid):
                v.append(f"G-P2: dependency cycle through {tid}")
                break
    # G-P3
    req_set = set(requirements)
    covered = set()
    for t in tasks:
        if not t["requirements"]:
            v.append(f"G-P3: task {t['id']} cites no requirement")
        for r in t["requirements"]:
            if r not in req_set:
                v.append(f"G-P3: task {t['id']} cites unknown requirement {r}")
            covered.add(r)
    for r in sorted(req_set - covered):
        v.append(f"G-P3: requirement {r} is not covered by any task")
    # G-P4 / G-P5
    for t in tasks:
        if not [c for c in t["verification"] if isinstance(c, str) and c.strip()]:
            v.append(f"G-P4: task {t['id']} has no verification command")
        if not t["acceptance"]:
            v.append(f"G-P4: task {t['id']} has no acceptance criterion")
        if t["soul"] not in implementer_souls:
            v.append(f"G-P5: task {t['id']} soul {t['soul']!r} not in {implementer_souls}")
    # G-P6
    bad_path = re.compile(r"(^/|\\|(^|/)\.\.?(/|$)|^$)")
    for t in tasks:
        if not t["files_modified"]:
            v.append(f"G-P6: task {t['id']} has empty files_modified")
        for p in t["files_modified"] + t.get("files_forbidden", []):
            if not isinstance(p, str) or bad_path.search(p):
                v.append(f"G-P6: task {t['id']} has invalid path {p!r}")
    if not v:
        reach = {tid: _reachable(graph, tid) for tid in graph}
        index = {t["id"]: _file_index(t["files_modified"]) for t in tasks}
        for i, a in enumerate(tasks):
            for b in tasks[i + 1:]:
                related = b["id"] in reach[a["id"]] or a["id"] in reach[b["id"]]
                if related:
                    continue
                clash = _files_clash(index[a["id"]], index[b["id"]])
                if clash:
                    v.append(f"G-P6: parallel tasks {a['id']} and {b['id']} both modify "
                             f"{clash}; add a dependency")
    # G-P7
    protected = list(oracle.get("acceptance_tests", [])) + list(protected_cfg)
    for t in tasks:
        for p in t["files_modified"]:
            for prot in protected:
                if paths_overlap(p, prot) or path_matches(p, prot):
                    v.append(f"G-P7: task {t['id']} modifies protected path {prot}")
    # G-P8
    if risk.rank(tier) >= 2:
        if not oracle.get("acceptance_tests"):
            v.append("G-P8: oracle.acceptance_tests must list at least one test path")
        if not oracle.get("acceptance_commands"):
            v.append("G-P8: oracle.acceptance_commands must list at least one command")
        crit = oracle.get("criteria") or []
        if not crit:
            v.append("G-P8: oracle.criteria must map acceptance criteria to requirements")
        for c in crit:
            if not isinstance(c, dict) or c.get("requirement") not in req_set:
                v.append(f"G-P8: criterion {c!r:.60} must reference an accepted requirement")
        surfaces = oracle.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces:
            v.append("G-P10: oracle.surfaces must declare at least one verification surface")
        else:
            ids = []
            for sf in surfaces:
                if not isinstance(sf, dict) or not str(sf.get("id") or "").strip():
                    v.append("G-P10: every surface needs an id")
                    continue
                ids.append(sf["id"])
                if not [c for c in sf.get("commands") or [] if isinstance(c, str) and c.strip()]:
                    v.append(f"G-P10: surface {sf['id']} needs at least one command")
                if not sf.get("pins") or not all(isinstance(p, str) and not bad_path.search(p)
                                                 for p in sf["pins"]):
                    v.append(f"G-P10: surface {sf['id']} must pin at least one valid repository path")
            if len(ids) != len(set(ids)) or any(not re.match(r"^[a-z0-9][a-z0-9-]*$", i) for i in ids):
                v.append("G-P10: surface ids must be unique lowercase-dashed names")
    return v


def waves(tasks: list[dict]) -> dict[str, int]:
    """Wave number per task (1 + deepest dependency). Iterative: no recursion limit on long
    chains. Assumes the graph is acyclic (G-P2 runs first)."""
    graph = {t["id"]: t["depends_on"] for t in tasks}
    level: dict[str, int] = {}
    for start in graph:
        stack = [start]
        while stack:
            tid = stack[-1]
            if tid in level:
                stack.pop()
                continue
            pending = [d for d in graph[tid] if d not in level]
            if pending:
                stack.extend(pending)
            else:
                level[tid] = 1 + max((level[d] for d in graph[tid]), default=0)
                stack.pop()
    return level


def _file_index(paths: list[str]) -> tuple[set[str], list[str]]:
    """Exact paths (set) and patterns (directory prefixes ending in '/', globs)."""
    patterns = [p for p in paths if p.endswith("/") or any(c in p for c in "*?[")]
    return set(paths) - set(patterns), patterns


def _files_clash(a: tuple[set[str], list[str]], b: tuple[set[str], list[str]]) -> str | None:
    common = a[0] & b[0]
    if common:
        return min(common)
    for pa in a[1]:
        for pb in list(b[0]) + b[1]:
            if paths_overlap(pa, pb):
                return f"{pa} / {pb}"
    for pb in b[1]:
        for pa in a[0]:
            if paths_overlap(pa, pb):
                return f"{pa} / {pb}"
    return None


# ---------------------------------------------------------------- findings / verdicts
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


def fingerprint(f: dict) -> str:
    first = (f.get("attestations") or [""])[0]
    path = first.split("@")[0]
    return f"{f.get('severity')}|{str(f.get('title', '')).strip().lower()}|{path}"


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


# ---------------------------------------------------------------- git-scoped work
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
