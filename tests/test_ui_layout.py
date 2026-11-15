"""The task graph layout in the web UI (app.js layout()), run in an embedded JS engine: no boxes
overlap, no edge is drawn through a box, implied edges are not drawn, and branches run side by
side. Uses a real task graph that the old column-stacking layout drew as a tangle."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip("quickjs")

APP_JS = Path(__file__).resolve().parent.parent / "legion_orch/server/static/app.js"


def run_layout(nodes: list[dict]) -> dict:
    src = APP_JS.read_text()
    fn = re.search(r"\nfunction layout\(nodes\) \{.*?\n\}\n", src, re.S).group(0)
    ctx = quickjs.Context()
    ctx.eval(fn)
    ctx.eval("function run(s) { return JSON.stringify(layout(JSON.parse(s))); }")
    return json.loads(ctx.get("run")(json.dumps(nodes)))


def change_graph(tasks: dict[str, list[str]]) -> list[dict]:
    """The pipeline around a task graph, as the engine builds it (graph.materialize_builds)."""
    nodes = [{"id": "intake", "depends_on": []}, {"id": "H-1", "depends_on": ["intake"]},
             {"id": "plan", "depends_on": ["H-1"]}, {"id": "critique", "depends_on": ["plan"]},
             {"id": "oracle", "depends_on": ["critique"]},
             {"id": "H-2", "depends_on": ["critique", "oracle"]}]
    nodes += [{"id": f"build:{t}", "depends_on": ["H-2"] + [f"build:{d}" for d in deps]}
              for t, deps in tasks.items()]
    nodes += [{"id": "integration", "depends_on": [f"build:{t}" for t in tasks]},
              {"id": "H-3", "depends_on": ["integration"]}, {"id": "ship", "depends_on": ["H-3"]}]
    return nodes


def path_points(path: str) -> list[tuple[float, float]]:
    """Sample an SVG path made of M, L and C commands."""
    toks = re.findall(r"[MCL]|-?[\d.]+", path)
    num = lambda k: float(toks[k])
    i, cur, pts = 0, (0.0, 0.0), []
    while i < len(toks):
        cmd = toks[i]
        i += 1
        if cmd == "M":
            cur = (num(i), num(i + 1))
            i += 2
        elif cmd == "L":
            end = (num(i), num(i + 1))
            i += 2
            pts += [(cur[0] + (end[0] - cur[0]) * k / 20, cur[1] + (end[1] - cur[1]) * k / 20)
                    for k in range(21)]
            cur = end
        else:
            c1, c2, end = (num(i), num(i + 1)), (num(i + 2), num(i + 3)), (num(i + 4), num(i + 5))
            i += 6
            for k in range(41):
                u = k / 40
                pts.append(tuple((1 - u) ** 3 * cur[j] + 3 * (1 - u) ** 2 * u * c1[j]
                                 + 3 * (1 - u) * u * u * c2[j] + u ** 3 * end[j] for j in (0, 1)))
            cur = end
    return pts


def check_drawing(out: dict) -> None:
    pos, W, H = out["pos"], out["W"], out["H"]
    ids = list(pos)
    overlaps = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]
                if abs(pos[a]["x"] - pos[b]["x"]) < W and abs(pos[a]["y"] - pos[b]["y"]) < H]
    assert overlaps == []
    through = set()
    for e in out["edges"]:
        for x, y in path_points(e["path"]):
            for nid, p in pos.items():
                if nid not in (e["from"], e["to"]) and \
                        p["x"] + 1 < x < p["x"] + W - 1 and p["y"] + 1 < y < p["y"] + H - 1:
                    through.add((e["from"], e["to"], nid))
    assert through == set(), "edges drawn through boxes"
    assert all(0 <= p["x"] and p["x"] + W <= out["width"] and 0 <= p["y"]
               and p["y"] + H <= out["height"] for p in pos.values())


# T1..T7 of a real plan (a Connect 4 app): two branches off T2 rejoin at T5; T7 hangs off T1.
CONNECT4 = {"T1": [], "T2": ["T1"], "T3": ["T2"], "T4": ["T2"], "T5": ["T3", "T4"],
            "T6": ["T5"], "T7": ["T1"]}


def test_branching_task_graph_has_no_overlaps_and_no_edges_through_boxes():
    out = run_layout(change_graph(CONNECT4))
    check_drawing(out)
    drawn = {(e["from"], e["to"]) for e in out["edges"]}
    # implied edges are not drawn: H-2 reaches T2..T7 through T1; critique reaches H-2 via oracle
    assert ("H-2", "build:T1") in drawn and ("H-2", "build:T5") not in drawn
    assert ("critique", "H-2") not in drawn and ("oracle", "H-2") in drawn
    assert ("build:T7", "integration") in drawn and ("build:T1", "integration") not in drawn
    pos = out["pos"]
    # parallel branches share a column, side by side
    assert pos["build:T3"]["x"] == pos["build:T4"]["x"] != pos["build:T5"]["x"]
    assert pos["build:T3"]["y"] != pos["build:T4"]["y"]


@pytest.mark.parametrize("tasks", [
    {"T1": [], "T2": [], "T3": [], "T4": []},                                  # all independent
    {f"T{i}": [f"T{i - 1}"] if i > 1 else [] for i in range(1, 7)},           # a chain
    {"A": [], "B": ["A"], "C": ["A"], "D": ["A"], "E": ["B", "C", "D"], "F": []},  # fan out/in
    {"A": [], "B": ["A"], "C": ["B"], "D": ["C"], "E": ["A"], "F": ["E", "D"], "G": ["A"]},
])
def test_other_shapes_draw_cleanly(tasks):
    check_drawing(run_layout(change_graph(tasks)))


def test_a_single_node_and_unknown_dependencies():
    out = run_layout([{"id": "intake", "depends_on": ["gone"]}])
    assert out["edges"] == [] and out["pos"]["intake"]["x"] == 12
