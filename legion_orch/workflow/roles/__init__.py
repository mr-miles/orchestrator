"""Per-role report handling; importing the package registers every handler."""
from . import (  # noqa: F401  (registration side effects)
    baseline,
    critic,
    describer,
    implementer,
    intake,
    integration,
    oracle,
    panel,
    planner,
    release,
    retrospective,
)
from .base import REGISTRY, Report, RoleHandler, handler_for, register, roles_where

__all__ = ["REGISTRY", "Report", "RoleHandler", "handler_for", "register", "roles_where"]
