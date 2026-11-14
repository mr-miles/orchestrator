"""Plan guards: task graph, oracle, traceability, waves and file overlap (G-P1..G-P10)."""
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
    protected = list(oracle.get("acceptance_tests", [])) + list(oracle.get("protected_support", [])) \
        + list(protected_cfg)
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
