"""Workload identity: SPIFFE JWT-SVID (via the OpenShell gateway) -> task-scoped access token.

Self-contained: depends on nothing else in the orchestrator. See spiffe.py for the flow.
"""
from .endpoint import make_token_endpoint
from .service import IdentityService
from .spiffe import JWT_SPIFFE, DevSpire, IdentityError, SvidVerifier
from .tokens import BINDING_CLAIMS, AccessTokens, WorkloadBinding

__all__ = ["make_token_endpoint", "IdentityService", "JWT_SPIFFE", "DevSpire", "IdentityError",
           "SvidVerifier", "BINDING_CLAIMS", "AccessTokens", "WorkloadBinding"]
