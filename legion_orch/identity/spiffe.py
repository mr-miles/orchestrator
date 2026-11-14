"""Workload identity: SPIFFE JWT-SVID → controller access token.

No credential is ever placed in a sandbox. The OpenShell gateway's sandbox supervisor holds the
sandbox's SPIFFE identity; a workspace-level provider profile (``legion-controller``) tells it to
exchange the sandbox's JWT-SVID at ``POST /oauth/token`` (OAuth2 client_credentials with a
``jwt-spiffe`` client assertion) and to inject the returned bearer into the agent's requests to
the controller (MCP and git). The controller:

1. verifies the JWT-SVID against the SPIRE JWKS (signature, issuer, audience, expiry, and the
   sandbox SPIFFE ID prefix),
2. maps the SPIFFE ID's sandbox id to a sandbox it created,
3. issues a short-lived access token naming that sandbox (HMAC-signed with a key that lives
   only in controller memory).

Each sandbox serves exactly one task (node) in one role for its whole life, so the access token is
task-specific: it names the sandbox, change, task and role. Every request must also match the
sandbox's current live run on all of those, so a token cached by the gateway is useless once the
run ends and can never act for another task.

For development without OpenShell, :class:`DevSpire` stands in for SPIRE + the gateway.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

JWT_SPIFFE = "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe"
SVID_ALGS = ["RS256", "RS384", "RS512", "ES256", "ES384", "PS256", "PS384", "PS512"]


class IdentityError(Exception):
    def __init__(self, error: str, description: str, status: int = 400):
        super().__init__(description)
        self.error, self.description, self.status = error, description, status


# ---------------------------------------------------------------------------- SVID verification
class SvidVerifier:
    """Verifies JWT-SVIDs against a JWKS (fetched from SPIRE, or static for development)."""

    def __init__(self, *, issuer: str | None, audience: str, trust_prefix: str,
                 jwks_uri: str | None = None, static_jwks: dict | None = None,
                 ca_file: str | None = None, cache_s: int = 60):
        if not (jwks_uri or static_jwks):
            raise ValueError("SvidVerifier needs jwks_uri or static_jwks")
        self.issuer, self.audience, self.trust_prefix = issuer, audience, trust_prefix
        self.jwks_uri, self.static, self.ca_file, self.cache_s = jwks_uri, static_jwks, ca_file, cache_s
        self._jwks: dict | None = None
        self._at = 0.0

    async def _keys(self, refresh: bool = False) -> dict:
        if self.static is not None:
            return self.static
        if refresh or self._jwks is None or time.time() - self._at > self.cache_s:
            async with httpx.AsyncClient(verify=self.ca_file or True, timeout=10) as c:
                r = await c.get(self.jwks_uri)
                r.raise_for_status()
                self._jwks, self._at = r.json(), time.time()
        return self._jwks

    async def verify(self, svid: str) -> str:
        """Return the sandbox reference (SPIFFE ID suffix after ``trust_prefix``)."""
        try:
            header = jwt.get_unverified_header(svid)
        except jwt.PyJWTError as e:
            raise IdentityError("invalid_client", f"malformed JWT-SVID: {e}", 401)
        if header.get("alg") not in SVID_ALGS:
            raise IdentityError("invalid_client", f"unsupported JWT-SVID alg {header.get('alg')}", 401)
        jwk = None
        for refresh in (False, True):            # one refresh on unknown kid (key rotation)
            keys = (await self._keys(refresh)).get("keys", [])
            jwk = next((k for k in keys if k.get("kid") == header.get("kid")), None)
            if jwk or self.static is not None:
                break
        if jwk is None:
            raise IdentityError("invalid_client", f"no JWKS key for kid {header.get('kid')}", 401)
        try:
            key = jwt.PyJWK(jwk).key
            claims = jwt.decode(svid, key, algorithms=[header["alg"]], audience=self.audience,
                                issuer=self.issuer, leeway=30,
                                options={"require": ["sub", "aud", "exp"],
                                         "verify_iss": self.issuer is not None})
        except jwt.PyJWTError as e:
            raise IdentityError("invalid_client", f"JWT-SVID rejected: {e}", 401)
        sub = str(claims.get("sub", ""))
        if not sub.startswith(self.trust_prefix) or len(sub) == len(self.trust_prefix):
            raise IdentityError("invalid_client", "JWT-SVID subject is not an OpenShell sandbox", 401)
        return sub[len(self.trust_prefix):]


# ---------------------------------------------------------------------------- dev SPIRE
@dataclass
class DevSpire:
    """In-process stand-in for SPIRE + the OpenShell supervisor (local runner only)."""

    trust_prefix: str
    issuer: str = "https://dev-spire.legion.local"
    kid: str = field(default_factory=lambda: secrets.token_hex(4))

    def __post_init__(self):
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def jwks(self) -> dict:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self._key.public_key()))
        return {"keys": [jwk | {"kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def mint(self, sandbox_ref: str, audience: str, ttl_s: int = 300, **overrides: Any) -> str:
        now = int(time.time())
        claims = {"sub": self.trust_prefix + sandbox_ref, "aud": [audience], "iss": self.issuer,
                  "iat": now, "exp": now + ttl_s} | overrides
        return jwt.encode(claims, self._key, algorithm="RS256", headers={"kid": self.kid})
