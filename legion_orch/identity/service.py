"""Token endpoint logic: OAuth2 client_credentials with a jwt-spiffe client assertion."""
from __future__ import annotations

from collections.abc import Callable

from .spiffe import JWT_SPIFFE, DevSpire, IdentityError, SvidVerifier
from .tokens import AccessTokens, WorkloadBinding


class IdentityService:
    def __init__(self, verifier: SvidVerifier, tokens: AccessTokens, scope: str,
                 dev: DevSpire | None = None):
        self.verifier, self.tokens, self.scope, self.dev = verifier, tokens, scope, dev

    async def token_request(self, form: dict[str, str],
                            binding_for: Callable[[str], WorkloadBinding | None]) -> dict:
        """OAuth2 client_credentials with a jwt-spiffe client assertion (RFC 7523 style).
        ``binding_for(sandbox_ref)`` is the sandbox's binding, or None for a sandbox this
        controller does not manage (or has deleted). Returns the OAuth token response."""
        if form.get("grant_type") != "client_credentials":
            raise IdentityError("unsupported_grant_type", "only client_credentials is supported")
        if form.get("client_assertion_type") != JWT_SPIFFE:
            raise IdentityError("invalid_client", "client_assertion_type must be jwt-spiffe")
        assertion = form.get("client_assertion")
        if not assertion:
            raise IdentityError("invalid_client", "missing client_assertion")
        if form.get("audience") and form["audience"] != self.tokens.audience:
            raise IdentityError("invalid_target", f"audience must be {self.tokens.audience}")
        scopes = (form.get("scope") or self.scope).split()
        if self.scope not in scopes:
            raise IdentityError("invalid_scope", f"scope must include {self.scope}")
        ref = await self.verifier.verify(assertion)
        binding = binding_for(ref)
        if binding is None:
            raise IdentityError("invalid_client", "sandbox is not managed by this controller", 401)
        return {"access_token": self.tokens.issue(binding, self.verifier.trust_prefix + ref),
                "token_type": "Bearer", "expires_in": self.tokens.ttl_s, "scope": self.scope}
