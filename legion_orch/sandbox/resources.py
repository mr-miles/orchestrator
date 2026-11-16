"""Sandbox resources, passed to the compute driver as ``--driver-config-json`` rendered from
``policies/sandbox-resources.template.yaml`` (the driver's own field names live there)."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..config import Config
from .templates import base_values, render


class Resources(BaseModel):
    """Requested resources; unset fields keep the driver's defaults."""
    model_config = ConfigDict(frozen=True, extra="forbid")
    memory_mib: int | None = None
    vcpus: int | None = None
    disk_mib: int | None = None

    def over(self, caps: Resources) -> list[tuple[str, int, int]]:
        """(field, requested, cap) for every request above its cap."""
        return [(k, v, getattr(caps, k)) for k, v in self.model_dump(exclude_none=True).items()
                if getattr(caps, k) is not None and v > getattr(caps, k)]


def _drop_nulls(v: Any) -> Any:
    if isinstance(v, dict):
        out = {k: _drop_nulls(x) for k, x in v.items() if x is not None}
        return {k: x for k, x in out.items() if x not in ({}, [])}
    return v


def driver_config(cfg: Config, res: Resources) -> str | None:
    """``--driver-config-json`` for a sandbox's resources (None: driver defaults)."""
    if res == Resources():
        return None
    doc = _drop_nulls(render(cfg, "sandbox_resources", base_values(cfg) | {
        "VCPUS": res.vcpus, "MEMORY_MIB": res.memory_mib, "DISK_MIB": res.disk_mib}))
    return json.dumps(doc, sort_keys=True) if doc else None
