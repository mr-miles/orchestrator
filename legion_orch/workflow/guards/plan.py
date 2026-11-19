"""Plan guards: task graph, oracle, traceability, waves, file overlap and the blank
repository's scaffold (G-P1..G-P12)."""
from __future__ import annotations

import re
from typing import Any

from ...git import path_matches, paths_overlap
from ...store import SchemaRegistry
from .. import risk


def _reachable(graph: dict[str, list[str]], start: str) -> set[str]:
    seen, stack = set(), [start]
    while stack:
        n = stack.pop()
        for d in graph.get(n, []):
            if d not in seen:
                seen.add(d)
                stack.append(d)
    return seen


_BAD_PATH = re.compile(r"(^/|\\|(^|/)\.\.?(/|$)|^$)")
_SURFACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
Graph = dict[str, list[str]]


def plan(taskgraph: Any, oracle: Any, requirements: list[str], tier: str,
         protected_cfg: list[str], implementer_souls: list[str], schemas: SchemaRegistry,
         blank_repo: bool = False) -> list[str]:
    """Cross-document plan rules. Shape (G-P1) is the ``taskgraph`` / ``oracle`` JSON Schemas –
    the same ones the store enforces on write – so they are defined in exactly one place. The
    remaining guards assume the shape; the file-overlap check also needs every earlier one."""
    v = shape(taskgraph, oracle, schemas)
    if v:
        return v
    tasks = taskgraph["tasks"]
    graph = {t["id"]: list(t["depends_on"]) for t in tasks}
    v = dependencies(tasks, graph)
    v += traceability(tasks, requirements)
    v += task_fields(tasks, implementer_souls)
    v += paths_valid(tasks)
    if not v:      # needs an acyclic graph and valid paths
        v += parallel_overlap(tasks, graph)
        v += scaffold(tasks, graph, blank_repo)
    v += protected_untouched(tasks, oracle, protected_cfg)
    if risk.rank(tier) >= 2:
        v += oracle_strength(oracle, requirements)
        v += surfaces(oracle)
        v += criterion_links(oracle)
        v += setup_steps(oracle)
    return v


def fix_round(taskgraph: Any, oracle: Any, requirements: list[str], tier: str,
              protected_cfg: list[str], implementer_souls: list[str], schemas: SchemaRegistry,
              built: dict[str, dict]) -> list[str]:
    """A fix round's revision of the task graph (§5.10a). ``built``: the tasks that already have
    build nodes (id -> contract). G-FR2: the revision keeps every one of them exactly as it is
    and appends at least one task with a new id; the whole graph meets G-P1 and G-P2, the new
    tasks G-P4–G-P7 (they may depend on, and modify the files of, accepted tasks). G-FR1: every
    new task cites accepted requirements (requirements are unchanged and already covered)."""
    v = shape(taskgraph, oracle, schemas)
    if v:
        return v
    tasks = taskgraph["tasks"]
    by_id = {t["id"]: t for t in tasks}
    for tid, contract in built.items():
        if tid not in by_id:
            v.append(f"G-FR2: task {tid} was removed; a fix round only appends tasks")
        elif _contract(by_id[tid]) != _contract(contract):
            v.append(f"G-FR2: task {tid} was changed; tasks of earlier rounds stay exactly as "
                     "they were built – add a new task instead")
    new = [t for t in tasks if t["id"] not in built]
    if not new:
        v.append("G-FR2: the fix round adds no task")
    graph = {t["id"]: list(t["depends_on"]) for t in tasks}
    v += dependencies(tasks, graph)
    known = set(requirements)
    for t in new:
        if not t["requirements"]:
            v.append(f"G-FR1: task {t['id']} cites no requirement")
        v += [f"G-FR1: task {t['id']} cites unknown requirement {r}"
              for r in t["requirements"] if r not in known]
    v += task_fields(new, implementer_souls)
    v += paths_valid(new)
    v += [f"G-P11: task {t['id']} is marked scaffold; only the first round of a blank "
          "repository has a scaffold task" for t in new if t.get("scaffold")]
    if not v:      # accepted tasks have finished: only the new ones may run in parallel
        new_ids = {t["id"] for t in new}
        v += parallel_overlap(new, {tid: [d for d in deps if d in new_ids]
                                    for tid, deps in graph.items() if tid in new_ids})
    v += protected_untouched(new, oracle, protected_cfg)
    if risk.rank(tier) >= 2:              # the oracle may have been revised in the round
        v += oracle_strength(oracle, requirements)
        v += surfaces(oracle)
        v += criterion_links(oracle)
        v += setup_steps(oracle)
    return v


def _contract(task: dict) -> dict:
    """A task contract with its schema defaults filled in, for comparing two versions."""
    return {"depends_on": [], "files_forbidden": [], "context": [], "scaffold": False} | dict(task)


def shape(taskgraph: Any, oracle: Any, schemas: SchemaRegistry) -> list[str]:
    """G-P1: both documents match their schemas."""
    return ([f"G-P1: taskgraph.json {e}" for e in schemas.validate_data("taskgraph", taskgraph)]
            + [f"G-P1: oracle.json {e}" for e in schemas.validate_data("oracle", oracle)])


def dependencies(tasks: list[dict], graph: Graph) -> list[str]:
    """G-P2: unique ids, known dependencies, no cycles."""
    ids = [t["id"] for t in tasks]
    v = []
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        v.append(f"G-P2: duplicate task ids {dup}")
    v += [f"G-P2: task {tid} depends on unknown task {d}"
          for tid, deps in graph.items() for d in deps if d not in graph]
    if not v:
        cyclic = next((tid for tid in graph if tid in _reachable(graph, tid)), None)
        if cyclic:
            v.append(f"G-P2: dependency cycle through {cyclic}")
    return v


def traceability(tasks: list[dict], requirements: list[str]) -> list[str]:
    """G-P3: every task cites known requirements; every requirement is covered."""
    req_set, covered, v = set(requirements), set(), []
    for t in tasks:
        if not t["requirements"]:
            v.append(f"G-P3: task {t['id']} cites no requirement")
        for r in t["requirements"]:
            if r not in req_set:
                v.append(f"G-P3: task {t['id']} cites unknown requirement {r}")
            covered.add(r)
    v += [f"G-P3: requirement {r} is not covered by any task" for r in sorted(req_set - covered)]
    return v


def task_fields(tasks: list[dict], implementer_souls: list[str]) -> list[str]:
    """G-P4: a verification command and an acceptance criterion; G-P5: a known soul."""
    v = []
    for t in tasks:
        if not _commands(t["verification"]):
            v.append(f"G-P4: task {t['id']} has no verification command")
        if not t["acceptance"]:
            v.append(f"G-P4: task {t['id']} has no acceptance criterion")
        if t["soul"] not in implementer_souls:
            v.append(f"G-P5: task {t['id']} soul {t['soul']!r} not in {implementer_souls}")
    return v


def paths_valid(tasks: list[dict]) -> list[str]:
    """G-P6 (paths): files_modified is non-empty and every path is relative and normalised."""
    v = []
    for t in tasks:
        if not t["files_modified"]:
            v.append(f"G-P6: task {t['id']} has empty files_modified")
        for p in t["files_modified"] + t.get("files_forbidden", []):
            if not _valid_path(p):
                v.append(f"G-P6: task {t['id']} has invalid path {p!r}")
    return v


def parallel_overlap(tasks: list[dict], graph: Graph) -> list[str]:
    """G-P6 (overlap): tasks that may run in parallel (neither depends on the other) modify
    disjoint files."""
    reach = {tid: _reachable(graph, tid) for tid in graph}
    index = {t["id"]: _file_index(t["files_modified"]) for t in tasks}
    v = []
    for i, a in enumerate(tasks):
        for b in tasks[i + 1:]:
            if b["id"] in reach[a["id"]] or a["id"] in reach[b["id"]]:
                continue
            clash = _files_clash(index[a["id"]], index[b["id"]])
            if clash:
                v.append(f"G-P6: parallel tasks {a['id']} and {b['id']} both modify "
                         f"{clash}; add a dependency")
    return v


def protected_untouched(tasks: list[dict], oracle: dict, protected_cfg: list[str]) -> list[str]:
    """G-P7: no task modifies the oracle's tests and support files or a configured protected path."""
    protected = [*oracle.get("acceptance_tests", []), *oracle.get("protected_support", []),
                 *protected_cfg]
    return [f"G-P7: task {t['id']} modifies protected path {prot}"
            for t in tasks for p in t["files_modified"] for prot in protected
            if paths_overlap(p, prot) or path_matches(p, prot)]


def scaffold(tasks: list[dict], graph: Graph, blank_repo: bool) -> list[str]:
    """G-P11: a blank repository's plan starts with exactly one scaffold task – no dependencies,
    and every other task depends on it (directly or not); otherwise no task is a scaffold."""
    marked = [t["id"] for t in tasks if t.get("scaffold")]
    if not blank_repo:
        return [f"G-P11: task {tid} is marked scaffold, but the repository is not blank"
                for tid in marked]
    if len(marked) != 1:
        return [f"G-P11: the repository is blank: mark exactly one task `scaffold: true` (the "
                f"build tooling the oracle and every other task build on); found {marked or 'none'}"]
    sid = marked[0]
    v = [f"G-P11: scaffold task {sid} must not depend on other tasks"] if graph[sid] else []
    v += [f"G-P11: task {tid} must depend on scaffold task {sid}" for tid in graph
          if tid != sid and sid not in _reachable(graph, tid)]
    return v


def oracle_strength(oracle: dict, requirements: list[str]) -> list[str]:
    """G-P8 (tier >= 2): the oracle has tests, commands and criteria tied to requirements."""
    v = []
    if not oracle.get("acceptance_tests"):
        v.append("G-P8: oracle.acceptance_tests must list at least one test path")
    if not oracle.get("acceptance_commands"):
        v.append("G-P8: oracle.acceptance_commands must list at least one command")
    crit = oracle.get("criteria") or []
    if not crit:
        v.append("G-P8: oracle.criteria must map acceptance criteria to requirements")
    v += [f"G-P8: criterion {c!r:.60} must reference an accepted requirement" for c in crit
          if not isinstance(c, dict) or c.get("requirement") not in set(requirements)]
    return v


def criterion_links(oracle: dict) -> list[str]:
    """G-P12 (tier >= 2): a criterion's ``tests`` are among the oracle's acceptance tests and its
    ``commands`` among its acceptance commands (both optional: an unlinked criterion is a warning
    on the gate screens, not a violation)."""
    tests, cmds = set(oracle.get("acceptance_tests") or []), set(oracle.get("acceptance_commands") or [])
    v = []
    for c in oracle.get("criteria") or []:
        if not isinstance(c, dict):
            continue
        v += [f"G-P12: criterion {c.get('id')} names test {t!r}, which is not in "
              "oracle.acceptance_tests" for t in c.get("tests") or [] if t not in tests]
        v += [f"G-P12: criterion {c.get('id')} names command {x!r}, which is not in "
              "oracle.acceptance_commands" for x in c.get("commands") or [] if x not in cmds]
    return v


# a command that only installs dependencies (or fetches them): the verifier must run it first
_INSTALL = re.compile(
    r"^\s*(?:(?:bun|npm|pnpm|yarn)\s+(?:install|i|ci)|uv\s+sync|pip3?\s+install|"
    r"poetry\s+install|pipenv\s+install|bundle\s+install|composer\s+install|"
    r"go\s+mod\s+download|cargo\s+fetch|mvn\s+dependency:\S+|dotnet\s+restore)\b[^;&|]*$")


def setup_steps(oracle: dict) -> list[str]:
    """G-P13 (tier >= 2): a command that only installs dependencies belongs in the repository's
    setup (mise.toml's ``setup`` task, §5.3c), which the verifier runs first; in the oracle's
    lists it would run after the acceptance commands that need it (acceptance, then integration)."""
    return [f"G-P13: `{c}` only installs dependencies: it belongs in mise.toml's setup task "
            "(run first after every checkout; propose it in the environment spec)"
            for key in ("acceptance_commands", "integration_commands")
            for c in oracle.get(key) or [] if isinstance(c, str) and _INSTALL.match(c)]


def surfaces(oracle: dict) -> list[str]:
    """G-P10 (tier >= 2): at least one verification surface; each has a unique id, commands
    and valid pinned paths."""
    found = oracle.get("surfaces")
    if not isinstance(found, list) or not found:
        return ["G-P10: oracle.surfaces must declare at least one verification surface"]
    v, ids = [], []
    for sf in found:
        if not isinstance(sf, dict) or not str(sf.get("id") or "").strip():
            v.append("G-P10: every surface needs an id")
            continue
        ids.append(sf["id"])
        if not _commands(sf.get("commands") or []):
            v.append(f"G-P10: surface {sf['id']} needs at least one command")
        if not sf.get("pins") or not all(_valid_path(p) for p in sf["pins"]):
            v.append(f"G-P10: surface {sf['id']} must pin at least one valid repository path")
    if len(ids) != len(set(ids)) or not all(_SURFACE_ID.match(i) for i in ids):
        v.append("G-P10: surface ids must be unique lowercase-dashed names")
    return v


def _commands(cmds: list) -> list[str]:
    return [c for c in cmds if isinstance(c, str) and c.strip()]


def _valid_path(p: Any) -> bool:
    return isinstance(p, str) and not _BAD_PATH.search(p)


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
