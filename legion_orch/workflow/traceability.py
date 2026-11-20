"""What the gate screens show (WORKFLOW.md §2.3): the traceability matrix – each accepted
requirement with its delta spec, the tasks that cover it and the acceptance criteria that test
it, and once built their verification, reviews and results – plus what H-3 accepts (the diff
since the base, per task, and the protected paths it changes).

Presentation only: it reads the change and never decides anything. The warnings are for the
human to weigh; the guards stay the authority."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from ..git import path_matches
from . import governance, risk
from .guards import requirements_ids
from .model import H_REQUIREMENTS, INTEGRATION, ApprovalState, Node
from .view import ChangeView

REQUIREMENTS = "intake/requirements.md"
TASKGRAPH = "plan/taskgraph.json"
ORACLE = "plan/oracle.json"

# a verification command that only lints, formats or type-checks: it cannot show behaviour
_LINT = re.compile(r"\b(lint|oxlint|eslint|ruff|flake8|pylint|prettier|black|isort|fmt|format|"
                   r"tsc|mypy|pyright|typecheck|type-check|vet|clippy|shellcheck)\b")
_TEST = re.compile(r"\b(test|tests|pytest|jest|vitest|spec|bats|e2e)\b")


class TaskCheck(BaseModel):
    ok: bool
    sha: str
    attestation: str | None = None


class TaskReview(BaseModel):
    role: str
    verdict: str


class TaskTrace(BaseModel):
    id: str
    title: str
    requirements: list[str]
    verification: list[str]
    scaffold: bool = False
    node: str | None = None             # its build node, once the plan is accepted
    status: str = "PLANNED"
    cycle: int = 1
    accepted_sha: str | None = None
    check: TaskCheck | None = None      # its latest deterministic verification
    reviews: list[TaskReview] = []      # the panel at its current cycle


class CriterionTrace(BaseModel):
    id: str
    text: str
    tests: list[str] = []
    commands: list[str] = []
    result: Literal["pass", "fail"] | None = None    # its commands at the integration head


class SpecTrace(BaseModel):
    path: str
    status: ApprovalState
    approved: int | None = None
    latest: int | None = None


class RequirementTrace(BaseModel):
    id: str
    text: str
    spec: SpecTrace | None = None
    tasks: list[str] = []
    criteria: list[CriterionTrace] = []


class TraceWarning(BaseModel):
    kind: str
    message: str
    requirement: str | None = None
    task: str | None = None
    criterion: str | None = None
    path: str | None = None             # the document involved (qualified)
    version: int | None = None          # stale: the version approved, to diff against


class CommandOutcome(BaseModel):
    command: str
    exit_code: int


class Results(BaseModel):
    """The integration head's evidence (H-3): the verifier's verdict and the controller's own
    oracle command results."""
    head: str | None = None
    verdict: str | None = None
    run: str | None = None
    ok: bool | None = None
    commands: list[CommandOutcome] = []
    attestation: str | None = None


class Traceability(BaseModel):
    built: bool                         # any task merged: verification and results columns apply
    requirements: list[RequirementTrace]
    tasks: list[TaskTrace]
    warnings: list[TraceWarning]
    results: Results | None = None
    summary: dict[str, int]


def _requirement_texts(e: ChangeView) -> dict[str, str]:
    """REQ id -> its first line, from requirements.md as accepted at H-1 (else the latest)."""
    if not e.latest(REQUIREMENTS):
        return {}
    h1 = e.change.approvals.get(H_REQUIREMENTS)
    text = e.doc(REQUIREMENTS, h1.pins.get(e.p(REQUIREMENTS)) if h1 else None).content
    out = {}
    for line in text.splitlines():
        ids = requirements_ids(line)
        if ids and ids[0] not in out:
            rest = line.split(ids[0], 1)[1]
            out[ids[0]] = rest.strip().lstrip("*:-–— ").strip().rstrip("*").strip()
    return out


def _json(e: ChangeView, rel: str) -> dict:
    if not e.latest(rel):
        return {}
    try:
        data = e.json_doc(rel)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def lint_only(commands: list[str]) -> bool:
    """Every command only lints, formats or type-checks (none runs tests)."""
    cmds = [c for c in commands if c.strip()]
    return bool(cmds) and all(_LINT.search(c) and not _TEST.search(c) for c in cmds)


def _task(t: dict, node: Node | None) -> TaskTrace:
    tr = TaskTrace(id=str(t.get("id")), title=str(t.get("title", "")),
                   requirements=list(t.get("requirements") or []),
                   verification=list(t.get("verification") or []),
                   scaffold=bool(t.get("scaffold")))
    if node is None:
        return tr
    tr.node, tr.status, tr.cycle, tr.accepted_sha = node.id, node.status, node.cycle, node.accepted_sha
    ver = node.current_verification() or max(node.verification.values(), key=lambda v: v.at,
                                             default=None)
    if ver:
        tr.check = TaskCheck(ok=ver.ok, sha=ver.sha, attestation=ver.attestation)
    prefix = f"{node.cycle}:"
    tr.reviews = [TaskReview(role=k[len(prefix):], verdict=r.verdict)
                  for k, r in sorted(node.reviews.items()) if k.startswith(prefix)]
    return tr


def _results(e: ChangeView) -> Results | None:
    node = e.current(INTEGRATION)
    if node is None:
        return None
    rev, ver = node.reviews.get("integration"), node.verification.get("integration")
    if rev is None and ver is None:
        return None
    return Results(head=ver.sha if ver else rev.sha, verdict=rev.verdict if rev else None,
                   run=rev.run if rev else None, ok=ver.ok if ver else None,
                   commands=[CommandOutcome(command=r.command, exit_code=r.exit_code)
                             for r in ver.results] if ver else [],
                   attestation=ver.attestation if ver else None)


def traceability(e: ChangeView) -> Traceability:
    """The matrix for the change's current plan: the latest task graph and oracle (what H-2 is
    asked to approve, or what was approved), with each task's build node once there is one."""
    ch = e.change
    req_ids = list(ch.requirements) or (requirements_ids(e.doc(REQUIREMENTS).content)
                                        if e.latest(REQUIREMENTS) else [])
    texts = _requirement_texts(e)
    graph, oracle = _json(e, TASKGRAPH), _json(e, ORACLE)
    tasks = [_task(t, e.nodes.get(f"build:{t.get('id')}"))
             for t in graph.get("tasks") or [] if isinstance(t, dict)]
    criteria = [c for c in oracle.get("criteria") or [] if isinstance(c, dict)]
    results = _results(e)
    exits = {c.command: c.exit_code for c in results.commands} if results else {}
    subjects = {s.subject: s for s in governance.h2_status(e)} if ch.requirements else {}
    r2 = risk.rank(ch.tier) >= 2 if ch.tier else False
    warnings: list[TraceWarning] = []

    rows = []
    for rid in req_ids:
        row = RequirementTrace(id=rid, text=texts.get(rid, ""),
                               tasks=[t.id for t in tasks if rid in t.requirements])
        s = subjects.get(rid)
        if s:
            path = s.paths[0]
            row.spec = SpecTrace(path=path, status=s.status, approved=s.approved.get(path),
                                 latest=s.latest.get(path))
        for c in criteria:
            if c.get("requirement") != rid:
                continue
            cr = CriterionTrace(id=str(c.get("id")), text=str(c.get("text", "")),
                                tests=list(c.get("tests") or []),
                                commands=list(c.get("commands") or []))
            if cr.commands and all(x in exits for x in cr.commands):
                cr.result = "pass" if all(exits[x] == 0 for x in cr.commands) else "fail"
            row.criteria.append(cr)
            if not cr.tests and not cr.commands:
                warnings.append(TraceWarning(
                    kind="unlinked_criterion", requirement=rid, criterion=cr.id, path=e.p(ORACLE),
                    message=f"{cr.id} names no test or command that checks it"))
        if not row.tasks:
            warnings.append(TraceWarning(kind="uncovered", requirement=rid,
                                         message=f"{rid} is not covered by any task"))
        elif all(t.scaffold for t in tasks if t.id in row.tasks):
            warnings.append(TraceWarning(kind="scaffold_only", requirement=rid,
                                         message=f"{rid} is covered only by the scaffold task"))
        if r2 and oracle and not row.criteria:
            warnings.append(TraceWarning(kind="no_criterion", requirement=rid, path=e.p(ORACLE),
                                         message=f"{rid} has no acceptance criterion"))
        rows.append(row)

    for t in tasks:
        if lint_only(t.verification):
            warnings.append(TraceWarning(
                kind="lint_only", task=t.id, path=e.p(TASKGRAPH),
                message=f"task {t.id} is verified only by lint or type checks: "
                        + ", ".join(f"`{c}`" for c in t.verification)))
    for s in subjects.values():
        if s.status != ApprovalState.STALE:
            continue
        for path, latest in s.latest.items():
            if s.approved.get(path) != latest:
                warnings.append(TraceWarning(
                    kind="stale", requirement=s.subject if s.subject in req_ids else None,
                    path=path, version=s.approved.get(path),
                    message=f"{path.split('/', 2)[-1]} changed since {s.approver} approved "
                            f"v{s.approved.get(path)} (now v{latest})"))

    order = {k: i for i, k in enumerate(("stale", "uncovered", "no_criterion", "scaffold_only",
                                         "lint_only", "unlinked_criterion"))}
    warnings.sort(key=lambda w: order.get(w.kind, len(order)))
    return Traceability(
        built=any(t.accepted_sha for t in tasks), requirements=rows, tasks=tasks,
        warnings=warnings, results=results,
        summary={"requirements": len(rows), "tasks": len(tasks), "criteria": len(criteria),
                 "warnings": len(warnings)})


# ---------------------------------------------------------------------------- H-3: the diff
class FileStat(BaseModel):
    path: str
    added: int | None = None            # None: binary
    deleted: int | None = None


class TaskDiff(BaseModel):
    id: str
    title: str
    files: list[FileStat]
    added: int
    deleted: int


class ProtectedChange(BaseModel):
    path: str
    approved_by: list[str] = []         # G-PP1 approvals that let a task change it
    tasks: list[str] = []


class DiffStat(BaseModel):
    base: str | None = None
    head: str | None = None
    files: list[FileStat] = []
    added: int = 0
    deleted: int = 0
    tasks: list[TaskDiff] = []
    protected: list[ProtectedChange] = []


def _stats(rows: list[tuple[str, int | None, int | None]]) -> tuple[list[FileStat], int, int]:
    files = [FileStat(path=p, added=a, deleted=d) for p, a, d in rows]
    return files, sum(f.added or 0 for f in files), sum(f.deleted or 0 for f in files)


async def diffstat(e: ChangeView) -> DiffStat:
    """What H-3 accepts: ``base..integration head`` as a whole and per merged task (each task's
    own commits, since it forked), and the protected paths changed after the oracle commit."""
    ch = e.change
    if not ch.base_sha or not ch.integration_head:
        return DiffStat(base=ch.base_sha, head=ch.integration_head)
    out = DiffStat(base=ch.base_sha, head=ch.integration_head)
    out.files, out.added, out.deleted = _stats(await e.git.numstat(ch.base_sha, ch.integration_head))
    for n in e.build_nodes():
        if n.accepted_sha and n.base_sha:
            files, a, d = _stats(await e.git.numstat(n.base_sha, n.accepted_sha, since_fork=True))
            out.tasks.append(TaskDiff(id=n.task.id, title=n.task.title, files=files,
                                      added=a, deleted=d))
    if ch.oracle_sha:
        protected = governance.protected_paths(e)
        changed = await e.git.changed_paths(ch.oracle_sha, ch.integration_head)
        for p in changed:
            if any(path_matches(p, x) for x in protected):
                apps = [a for a in ch.protected_approvals
                        if any(path_matches(p, x) or p == x for x in a.paths)]
                out.protected.append(ProtectedChange(
                    path=p, approved_by=sorted({a.approver for a in apps}),
                    tasks=sorted({a.task for a in apps})))
    return out


# ---------------------------------------------------------------------------- markdown
def _cell(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def to_markdown(t: dict, cid: str) -> str:
    """The matrix as markdown (``legion-orch trace``), from :class:`Traceability` JSON."""
    s = t["summary"]
    lines = [f"# Traceability: {cid}", "",
             f"{s['requirements']} requirements · {s['tasks']} tasks · {s['criteria']} criteria"
             f" · {s['warnings']} warnings", ""]
    if t["warnings"]:
        lines += ["## Warnings", ""] + [f"- {w['message']}" for w in t["warnings"]] + [""]
    tasks = {x["id"]: x for x in t["tasks"]}
    head = ["Requirement", "Spec", "Tasks", "Criteria"]
    lines += ["## Matrix", "", "| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in t["requirements"]:
        spec = r.get("spec") or {}
        task_cells = []
        for tid in r["tasks"]:
            x = tasks.get(tid, {})
            bits = [x.get("status", "")]
            if x.get("check"):
                bits.append("verified" if x["check"]["ok"] else "verification failed")
            task_cells.append(f"{tid} ({', '.join(b for b in bits if b)})")
        crit = []
        for c in r["criteria"]:
            links = ", ".join(c["tests"] + [f"`{x}`" for x in c["commands"]]) or "unlinked"
            res = f" **{c['result']}**" if c.get("result") else ""
            crit.append(f"{c['id']}: {c['text']} → {links}{res}")
        lines.append("| " + " | ".join(_cell(x) for x in (
            f"**{r['id']}** {r['text']}", spec.get("status", "—"),
            "<br>".join(task_cells) or "—", "<br>".join(crit) or "—")) + " |")
    res = t.get("results")
    if res:
        lines += ["", "## Results at the integration head", "",
                  f"Verifier verdict: {res.get('verdict') or '—'}; controller verification: "
                  f"{'ok' if res.get('ok') else 'failed' if res.get('ok') is False else '—'}", ""]
        lines += [f"- `{c['command']}` → exit {c['exit_code']}" for c in res.get("commands", [])]
    return "\n".join(lines) + "\n"
