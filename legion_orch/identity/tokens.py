"""Task-scoped controller access tokens. The signing key never leaves controller memory."""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass, fields

import jwt


@dataclass(frozen=True)
class WorkloadBinding:
    """What a sandbox is bound to for its whole life: one task of one change, in one role.
    Carried as claims in its access token."""
    sandbox: str
    change: str
    task: str
    role: str


BINDING_CLAIMS = tuple(f.name for f in fields(WorkloadBinding))


class AccessTokens:
    """Short-lived, task-scoped controller tokens. The key never leaves controller memory."""

    def __init__(self, audience: str, ttl_s: int):
        self.key = secrets.token_bytes(32)
        self.audience, self.ttl_s = audience, ttl_s

    def issue(self, binding: WorkloadBinding, spiffe_id: str) -> str:
        now = int(time.time())
        return jwt.encode(asdict(binding) | {"sub": binding.sandbox, "spiffe_id": spiffe_id,
                                    "aud": self.audience, "iat": now, "exp": now + self.ttl_s,
                                    "jti": secrets.token_hex(8)},
                          self.key, algorithm="HS256")

    def verify(self, token: str) -> WorkloadBinding | None:
        """The token's binding, or None if the token is invalid or expired."""
        try:
            c = jwt.decode(token, self.key, algorithms=["HS256"], audience=self.audience,
                           options={"require": ["exp", *BINDING_CLAIMS]})
        except jwt.PyJWTError:
            return None
        return WorkloadBinding(**{k: c[k] for k in BINDING_CLAIMS})
