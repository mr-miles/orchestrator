"""resolver: diagnoses escalations and proposes a decision a human may apply (A2A only).

The resolver is the change's, not a step's: it is started on its first message (an escalation
the orchestrator routes to it, or an agent's question) and kept, stopped between messages, for
the whole change. It answers in its final message – never a report – and changes nothing.
"""
from __future__ import annotations

from .base import RoleHandler, register


@register
class Resolver(RoleHandler):
    roles = ("resolver",)
