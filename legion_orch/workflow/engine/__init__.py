"""The workflow engine: :class:`Controller` (shared services, one engine per change) and
:class:`ChangeEngine` (one change, composed in compose.py from its layers – runtime, steps,
services, sandboxes, base; docs/architecture.md). Nothing outside the engine imports more
than this package's names (checked by the import contracts in pyproject.toml). An engine is
given :class:`Services` and an :class:`EngineHost`; sandbox identity binding is
:class:`WorkloadAuth`."""
from .base.auth import VerifierGrant, WorkloadAuth
from .base.common import AgentBusy, NoAgent, WorkflowError
from .base.shared import EngineHost, Services
from .change import ChangeEngine
from .controller import Controller

__all__ = ["AgentBusy", "ChangeEngine", "Controller", "EngineHost", "NoAgent",
           "Services", "VerifierGrant", "WorkflowError", "WorkloadAuth"]
