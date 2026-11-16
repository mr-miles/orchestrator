"""environment: the sandbox environment spec, composed from the library (G-E1..G-E4)."""
from __future__ import annotations

from .. import guards
from ..engine.common import ENV_SPEC as SPEC
from ..environment import LibraryError
from ..model import CRITIQUE, PENDING
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class EnvironmentEngineer(RoleHandler):
    roles = ("environment",)
    sees_draft_plan = True          # the plan it equips (verification commands, task files)
    sees_oracle_commit = False

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        spec, err = guards.load_json(e.docs, r.docs.get(e.p(SPEC)), "G-E1")
        if err:
            return err
        try:
            lib = e.environment_library()
        except LibraryError as x:
            return [f"G-E1: the environment library is invalid (an operator must fix it): {x}"]
        _, v = guards.environment(spec, lib, e.cfg, set(e.assets.roles))
        return v

    async def on_done(self, e: RoleContext, r: Report) -> None:
        await super().on_done(e, r)
        e.nodes[CRITIQUE].status = PENDING      # (re-)critique the plan with its environment
