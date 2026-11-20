"""The nodes outside the workflow's tree, and the overlap query the scheduler asks.

Which nodes a change has follows from its workflow (:mod:`.workflow_tree`, made into nodes by
:class:`.FlowRuntime`); the retrospective of an ended change is the one node added here."""
from __future__ import annotations

from typing import TYPE_CHECKING

from ...git import paths_overlap
from ..model import ACTIVE_NODE, READY, RETRO, REWORK, Node
from .core import ChangeCore

if TYPE_CHECKING:
    from .steps import StepRunner


class StageGraph:
    def __init__(self, core: ChangeCore):
        self.c = core
        self.runner: StepRunner = None  # type: ignore[assignment]   # set once it exists

    def add_retrospective(self) -> Node:
        """Queue the retrospective of an ended change: a new node, or a new cycle of it."""
        c = self.c
        node = c.nodes.get(RETRO)
        if node is None:
            node = Node(id=RETRO, kind=RETRO, status=READY,
                        title="Retrospective: lessons for the assets and process")
            c.add_node(node)
        else:
            node.cycle += 1
            self.runner.again(node)
        return node

    def overlaps_running(self, node: Node) -> bool:
        """Guard G-D3 (file overlap part)."""
        mine = node.task.files_modified
        for other in self.c.build_nodes():
            if other.id == node.id or other.status not in ACTIVE_NODE | {REWORK}:
                continue
            if any(paths_overlap(a, b) for a in mine for b in other.task.files_modified):
                return True
        return False
