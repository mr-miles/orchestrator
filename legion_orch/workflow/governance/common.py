"""Shared by the governance modules: the error type and evidence resolution."""
from __future__ import annotations

from .. import attest
from ..view import ChangeView


class GovernanceError(Exception):
    """A human governance action was refused (the message names the guard)."""


async def resolve_or_raise(e: ChangeView, ref: str, what: str) -> None:
    """Raise :class:`GovernanceError` unless ``ref`` parses and resolves (``what`` prefixes the
    message, for example ``"G-AT1: source"``)."""
    try:
        ok = await attest.resolve(attest.parse(ref, [e.cfg.repo.name]), e.docs, e.git)
    except attest.AttestationError as ex:
        raise GovernanceError(f"{what}: {ex}") from None
    if not ok:
        raise GovernanceError(f"{what} does not resolve: {ref}")
