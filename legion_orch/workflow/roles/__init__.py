"""Per-role report handling; importing the package registers every handler."""
from . import (  # noqa: F401  (registration side effects)
    critic,
    describer,
    design,
    implementer,
    intake,
    integration,
    oracle,
    panel,
    planner,
    release,
    resolver,
    retrospective,
)
from .base import REGISTRY, Report, RoleHandler, Said, handler_for, register

__all__ = ["REGISTRY", "Report", "RoleHandler", "Said", "handler_for", "register"]
