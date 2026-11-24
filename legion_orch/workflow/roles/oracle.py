"""oracle: the protected acceptance tests, committed on its branch (G-O1, G-O2) – merged, like
any ticket's commit, when it passes (the runner's)."""
from __future__ import annotations

import json

from .. import guards
from ..model import Oracle
from ..view import RoleContext
from .base import Report, RoleHandler, pointer, register


@register
class OracleAuthor(RoleHandler):
    roles = ("oracle",)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        orc = Oracle.model_validate(json.loads(pointer(e, "oracle") or "{}"))
        changed = await e.git.changed_paths(r.run.base_sha, r.sha)
        allowed = orc.acceptance_tests + orc.protected_support
        v = guards.scoped_diff(changed, allowed, [], e.cfg.protected_paths, "G-O1")
        for p in orc.acceptance_tests:
            if not await e.git.has_object(r.sha, p):
                v.append(f"G-O2: acceptance test {p} missing at {r.sha[:12]}")
        return v
