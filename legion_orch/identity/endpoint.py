"""Starlette handler for ``POST /oauth/token`` (called by the OpenShell gateway supervisor)."""
from __future__ import annotations

import logging
from collections.abc import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from .service import IdentityService
from .spiffe import IdentityError
from .tokens import WorkloadBinding

log = logging.getLogger("legion.identity")


def make_token_endpoint(identity: IdentityService, binding_for: Callable[[str], WorkloadBinding | None]):
    async def oauth_token(request: Request):
        """JWT-SVID client assertion -> task-scoped access token."""
        form = dict(await request.form())
        try:
            body = await identity.token_request(form, binding_for)
        except IdentityError as e:
            # the reason only (never the assertion): why the gateway's token grant failed
            log.warning("token request refused (%s): %s", e.error, e.description)
            return JSONResponse({"error": e.error, "error_description": e.description},
                                status_code=e.status)
        except Exception as e:   # e.g. SPIRE JWKS unreachable
            log.warning("token request failed: %r", e)
            return JSONResponse({"error": "temporarily_unavailable", "error_description": str(e)},
                                status_code=503)
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return oauth_token
