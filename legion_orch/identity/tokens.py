"""Task-scoped controller access tokens. The signing key never leaves controller memory."""
from __future__ import annotations

import secrets
import time

import jwt

BINDING_CLAIMS = ("sandbox", "change", "task", "role")


class AccessTokens:
    """Short-lived, task-scoped controller tokens. The key never leaves controller memory."""

    def __init__(self, audience: str, ttl_s: int):
        self.key = secrets.token_bytes(32)
        self.audience, self.ttl_s = audience, ttl_s

    def issue(self, binding: dict[str, str], spiffe_id: str) -> str:
        """``binding`` = {sandbox, change, task, role} of the sandbox's assignment."""
        now = int(time.time())
        claims = {k: binding[k] for k in BINDING_CLAIMS}
        return jwt.encode(claims | {"sub": binding["sandbox"], "spiffe_id": spiffe_id,
                                    "aud": self.audience, "iat": now, "exp": now + self.ttl_s,
                                    "jti": secrets.token_hex(8)},
                          self.key, algorithm="HS256")

    def verify(self, token: str) -> dict[str, str] | None:
        """Return the binding claims, or None if the token is invalid or expired."""
        try:
            c = jwt.decode(token, self.key, algorithms=["HS256"], audience=self.audience,
                           options={"require": ["exp", *BINDING_CLAIMS]})
        except jwt.PyJWTError:
            return None
        return {k: c[k] for k in BINDING_CLAIMS}
