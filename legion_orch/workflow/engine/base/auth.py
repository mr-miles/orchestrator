"""Workload authentication: which sandbox may act for which run.

A sandbox proves its identity with a SPIFFE JWT-SVID (exchanged by the OpenShell gateway for a
task-scoped access token at ``/oauth/token``). This module decides what that identity is bound to:

* :meth:`WorkloadAuth.sandbox_binding` – the one task a live sandbox serves (token issuance),
* :meth:`WorkloadAuth.live_run` – the live run a bearer token acts for (MCP calls),
* :meth:`WorkloadAuth.git_grant` – what a bearer may do on the git endpoint.

Verifier sandboxes have no run record; the engine registers them for their lifetime.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import httpx

from ....config import Config
from ....git import GitGrant
from ....identity import (
    JWT_SPIFFE,
    AccessTokens,
    DevSpire,
    IdentityService,
    SvidVerifier,
    WorkloadBinding,
)
from ...model import LIVE_RUN, Run
from .common import log

# Every run of every change, as (change id, run).
RunSource = Callable[[], Iterable[tuple[str, Run]]]


@dataclass(frozen=True)
class VerifierGrant:
    """A live verifier sandbox: read-only git access for one task of one change."""
    id: str
    change: str
    task: str


class WorkloadAuth:
    def __init__(self, cfg: Config, runs: RunSource):
        self.cfg = cfg
        self._runs = runs
        self._verifiers: dict[str, VerifierGrant] = {}     # sandbox ref -> grant
        ic = cfg.identity
        self.dev_spire = DevSpire(ic.trust_prefix) if ic.dev_spire else None
        dev = self.dev_spire
        verifier = SvidVerifier(issuer=dev.issuer if dev else ic.issuer, audience=ic.svid_audience,
                                trust_prefix=ic.trust_prefix, jwks_uri=None if dev else ic.jwks_uri,
                                static_jwks=dev.jwks() if dev else None, ca_file=ic.jwks_ca_file)
        self.identity = IdentityService(verifier, AccessTokens(ic.token_audience, ic.access_ttl_s),
                                        ic.scope, dev)

    # ---- verifier sandboxes
    def register_verifier(self, sandbox_ref: str, grant: VerifierGrant) -> None:
        self._verifiers[sandbox_ref] = grant

    def unregister_verifier(self, sandbox_ref: str) -> None:
        self._verifiers.pop(sandbox_ref, None)

    # ---- bindings
    def sandbox_binding(self, ref: str) -> WorkloadBinding | None:
        """The one task (and role) a live, managed sandbox serves – None if unknown or deleted.
        A sandbox serves exactly one task for its whole life (an implementer sandbox is reused
        across that task's rework cycles; judges and verifiers always get their own)."""
        if ref in self._verifiers:
            v = self._verifiers[ref]
            return WorkloadBinding(sandbox=ref, change=v.change, task=v.task, role="verifier")
        for cid, r in self._runs():
            if r.sandbox_ref == ref and not r.sandbox_deleted:
                return WorkloadBinding(sandbox=ref, change=cid, task=r.node_id, role=r.role)
        return None

    def live_run(self, token: str | None) -> tuple[str, Run] | None:
        """Bind a gateway-injected, task-scoped token to the sandbox's live run, per request.
        The run must match the token's change, task and role exactly."""
        b = self.identity.tokens.verify(token) if token else None
        if b is None:
            return None
        for cid, r in self._runs():
            if r.sandbox_ref == b.sandbox and r.status in LIVE_RUN:
                if (cid, r.node_id, r.role) == (b.change, b.task, b.role):
                    return cid, r
                log.warning("token for %s does not match live run %s", b, r.id)
                return None
        return None

    def git_grant(self, token: str | None) -> GitGrant | None:
        b = self.identity.tokens.verify(token) if token else None
        if b is None:
            return None
        if b.role == "verifier":
            v = self._verifiers.get(b.sandbox)
            if v and (v.change, v.task) == (b.change, b.task):
                return GitGrant(v.id, None)          # read-only
            return None
        found = self.live_run(token)
        if found is None:
            return None
        run = found[1]
        return GitGrant(run.id, f"refs/heads/{run.branch}" if run.branch else None)

    # ---- local runner only
    async def dev_gateway(self, sandbox_ref: str) -> str:
        """Do what the OpenShell supervisor does: mint the sandbox's JWT-SVID (dev SPIRE) and
        exchange it at our own /oauth/token endpoint."""
        if self.dev_spire is None:
            raise RuntimeError("dev gateway requires identity.dev_spire")
        ic = self.cfg.identity
        form = {"grant_type": "client_credentials", "client_assertion_type": JWT_SPIFFE,
                "client_assertion": self.dev_spire.mint(sandbox_ref, ic.svid_audience),
                "audience": ic.token_audience, "scope": ic.scope}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(self.cfg.server.token_url, data=form)
            r.raise_for_status()
            return r.json()["access_token"]
