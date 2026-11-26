"""oracle: the protected acceptance tests, committed on its branch (G-O1, G-O2) – merged, like
any ticket's commit, when it passes (the runner's)."""
from __future__ import annotations

import json

from ...git import PushScope, Rule
from .. import guards
from ..model import Oracle, Run
from ..view import RoleContext
from .base import Report, RoleHandler, pointer, register


def _oracle(e: RoleContext) -> Oracle:
    return Oracle.model_validate(json.loads(pointer(e, "oracle") or "{}"))


@register
class OracleAuthor(RoleHandler):
    roles = ("oracle",)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = _oracle(e)
        changed = await e.git.changed_paths(r.run.base_sha, r.sha)
        allowed = orc.acceptance_tests + orc.protected_support
        v = guards.scoped_diff(changed, allowed, [], e.cfg.protected_paths, "G-O1")
        for p in orc.acceptance_tests:
            if not await e.git.has_object(r.sha, p):
                v.append(f"G-O2: acceptance test {p} missing at {r.sha[:12]}")
        return v

    def push_scope(self, e: RoleContext, run: Run) -> PushScope:
        """G-O1 at push: its tests and support files, never a protected path."""
        orc = _oracle(e)
        return PushScope("G-O1", run.base_sha, (
            Rule("protected", tuple(e.cfg.protected_paths), why="{path} is a protected path"),
            Rule("tests", tuple(orc.acceptance_tests + orc.protected_support), allow=True)))
