"""Verification surfaces: the files each surface relies on are pinned at oracle approval and
must be unchanged at ship time unless a human re-affirms them (G-SF1)."""
from __future__ import annotations

from ..model import (
    Oracle,
    Surface,
    SurfacePin,
)
from ..view import ChangeView
from .common import GovernanceError


def surfaces(e: ChangeView) -> list[Surface]:
    return list((e.change.oracle or Oracle()).surfaces)


async def pin_surfaces(e: ChangeView, approver: str, surface_id: str | None, reason: str) -> list[str]:
    """Pin (or re-affirm) the files a verification surface relies on at the integration head."""
    head = e.change.integration_head
    chosen = [s for s in surfaces(e) if surface_id in (None, s.id)]
    if surface_id and not chosen:
        raise GovernanceError(f"unknown surface {surface_id!r}")
    for s in chosen:
        pins = {p: await e.git.blob(head, p) for p in s.pins}
        rec = SurfacePin(surface=s.id, pins=pins, head=head, approver=approver, reason=reason)
        e.write_record(f"control/surfaces/{s.id}.json", rec)     # projected: surface_pins
    return [s.id for s in chosen]


async def surface_drift(e: ChangeView, head: str) -> list[str]:
    """Guard G-SF1: pinned files unchanged at ``head`` (or re-affirmed)."""
    v = []
    for s in surfaces(e):
        rec = e.change.surface_pins.get(s.id)
        if not rec:
            v.append(f"G-SF1: surface {s.id} was never pinned")
            continue
        for path, blob in rec.pins.items():
            if await e.git.blob(head, path) != blob:
                v.append(f"G-SF1: surface {s.id} pinned file {path} changed since it was pinned; "
                         "a human must re-affirm the surface")
    return v


def surface_commands(e: ChangeView) -> list[str]:
    return [c for s in surfaces(e) for c in s.commands]
