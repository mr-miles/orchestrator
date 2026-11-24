"""``controller:provision``: the environment built from its signed-off spec – the images, the
setup checked in them (G-PV1, G-PV3) – by the provisioner. A failure is the system's, not an
agent's: never an attempt. A failed build (the spec, a registry) escalates at once – today's
``provision_failed``, which a human retries once fixed; the provisioner's own errors (an outage)
are retried with backoff."""
from __future__ import annotations

from typing import Protocol

from .base import Backoff, Done, Fault, Outcome, Work, attempt


class ProvisionPort(Protocol):
    """The provisioner, for tickets: None when the environment is built, else what failed."""

    async def provision(self, work: Work) -> str | None: ...

    async def stop(self, ticket: str) -> None: ...


class ProvisionExecutor:
    def __init__(self, port: ProvisionPort, backoff: Backoff = Backoff()):
        self.port, self.backoff = port, backoff

    async def run(self, work: Work) -> Outcome:
        async def once() -> Outcome:
            failed = await self.port.provision(work)
            return Done() if failed is None else Fault(failed, transient=False)
        return await attempt(once, self.backoff)

    async def stop(self, ticket: str) -> None:
        await self.port.stop(ticket)
