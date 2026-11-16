"""environment: the sandbox environment spec, composed from the library (G-E1..G-E4)."""
from __future__ import annotations

from .. import guards
from ..engine.common import ENV_SPEC
from ..environment import EnvironmentSpec, Library, LibraryError
from ..model import CRITIQUE, PENDING, Node
from ..view import RoleContext
from .base import Report, RoleHandler, register


@register
class EnvironmentEngineer(RoleHandler):
    roles = ("environment",)
    sees_draft_plan = True          # the plan it equips (verification commands, task files)
    sees_oracle_commit = False

    def extra_variables(self, e: RoleContext, node: Node) -> dict[str, str]:
        """The library catalogue for the prompt (an invalid library is reported, not raised:
        the agent's spec is then rejected by G-E1 until an operator fixes it)."""
        try:
            return {"environment_library": Library.for_config(e.cfg, e.assets.schemas).catalogue()}
        except LibraryError as x:
            return {"environment_library": f"(the library is invalid; an operator must fix it: {x})"}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        raw, err = guards.load_json(e.docs, r.docs.get(e.p(ENV_SPEC)), "G-E1")
        if err:
            return err
        try:
            lib = Library.for_config(e.cfg, e.assets.schemas)
        except LibraryError as x:
            return [f"G-E1: the environment library is invalid (an operator must fix it): {x}"]
        _, v = guards.environment(EnvironmentSpec.model_validate(raw), lib, e.cfg, set(e.assets.roles))
        return v

    async def on_done(self, e: RoleContext, r: Report) -> None:
        await super().on_done(e, r)
        e.nodes[CRITIQUE].status = PENDING      # (re-)critique the plan with its environment
