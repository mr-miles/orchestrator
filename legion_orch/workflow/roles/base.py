"""Role handlers: the role-specific half of handling an agent's ``report``.

The engine checks what every role shares (report shape, attestations, the role's outputs and git
result) and then calls the handler registered for the run's role:

* :meth:`RoleHandler.guards` – role-specific guards; returns violation strings;
* :meth:`RoleHandler.on_done` – what an accepted ``DONE`` report says (:class:`Said`): a judge's
  verdict and the findings its ticket's work is told to fix; nothing more for a ticket's own
  work (its outputs and commit are the report's). A handler never changes a ticket, merges or
  records a workflow result: the ticket runner does.

Adding a role = assets (``roles.yaml``, soul, prompt, schemas) + one handler module here.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..attest import FILE_RE, Attestation
from ..model import Finding, Record, Run, Verdict
from ..tickets import Ticket
from ..view import RoleContext

REQ_RE = re.compile(r"\bREQ-[0-9]+\b")


@dataclass
class Report:
    """An accepted-so-far report, after the shared guards passed."""
    run: Run
    ticket: Ticket
    parsed: list[Attestation]
    data: Record                     # typed report data (workflow/report_data.py)
    outputs: dict[str, Any] = field(default_factory=dict)   # name -> its pinned document(s)
    sha: str | None = None                                   # required git result
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def repo_atts(self) -> set[str]:
        return {str(a) for a in self.parsed}


@dataclass(frozen=True)
class Said:
    """What an accepted report says, for its ticket: a judge's verdict (None: not a judge) and
    the findings the judged work is told to fix."""
    verdict: Verdict | None = None
    findings: tuple[str, ...] = ()


class RoleHandler:
    roles: tuple[str, ...] = ()

    def extra_context(self, e: RoleContext, ticket: Ticket) -> list[str]:
        """Role-specific context attestations beyond its inputs and its work's."""
        return []

    def payload_files(self, e: RoleContext, ticket: Ticket, run: Run) -> dict[str, str]:
        """Extra files for the run's payload (path relative to the payload directory -> text)."""
        return {}

    def extra_variables(self, e: RoleContext, ticket: Ticket) -> dict[str, str]:
        """Role-specific prompt template variables beyond the common ones."""
        return {}

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return []

    async def on_done(self, e: RoleContext, r: Report) -> Said:
        """Default: the work is done (its outputs and commit are the report's)."""
        return Said()


def judged(verdict: Verdict, findings: list[Finding]) -> Said:
    """A judge's verdict, with the findings that drive rework (HIGH-confidence blockers and
    warnings) as what the judged work is told."""
    return Said(verdict, tuple(f"[{f.severity}] {f.title}: {f.observation} {f.fix}".strip()
                               for f in findings if f.high)[:30])


def doc_of(e: RoleContext, ref: str | None) -> str | None:
    """The text of a ``file:<path>@<version>`` ref (None: none, or not a document)."""
    m = FILE_RE.match(ref or "")
    return e.docs.read(m["path"], int(m["version"])).content if m else None


def output(e: RoleContext, r: Report, name: str) -> str | None:
    """The text of one of the report's outputs."""
    ref = r.outputs.get(name)
    return doc_of(e, ref if isinstance(ref, str) else None)


def pointer(e: RoleContext, name: str) -> str | None:
    """The text of an approved output (the change's pointer ``name``)."""
    ref = e.tickets.root.pointers.get(name)
    return doc_of(e, ref if isinstance(ref, str) else None)


def requirement_ids(e: RoleContext) -> list[str]:
    """The approved requirements' ids (``REQ-n``), in order."""
    return list(dict.fromkeys(REQ_RE.findall(pointer(e, "requirements") or "")))


def outputs_named(e: RoleContext, name: str, type_: str | None = None) -> list[str]:
    """Every live ticket's pinned output ``name`` (of tickets of ``type_``), in tree order."""
    out: list[str] = []
    for x in e.tickets.t.values():
        if x.status in ("failed", "cancelled") or (type_ and x.type != type_):
            continue
        ref = x.outputs.get(name)
        out += ref if isinstance(ref, list) else [ref] if ref else []
    return out


def outputs_of_owner(e: RoleContext, ticket: Ticket) -> list[str]:
    """The outputs of the work a criterion judges (its owner's)."""
    owner = e.tickets.t.get(ticket.parent) if ticket.parent else None
    out: list[str] = []
    for ref in (owner.outputs.values() if owner else []):
        out += ref if isinstance(ref, list) else [ref]
    return out


MISE_TOML = "mise.toml"


def oracle_paths(e: RoleContext, oracle: dict | None = None) -> list[str]:
    """The approved oracle's tests and support files: only the oracle changes them (§5.4)."""
    orc = oracle if oracle is not None else _json(pointer(e, "oracle"))
    return [*orc.get("acceptance_tests", []), *orc.get("protected_support", [])]


def guarded(e: RoleContext) -> list[str]:
    """The operator's protected paths, and ``mise.toml`` while the environment pins a setup
    (§5.3c): a build changes one only when a decision owner approved it (request_scope)."""
    env = _json(pointer(e, "environment"))
    return [*e.cfg.protected_paths, *([MISE_TOML] if env.get("mise") or e.change.setup_pin else [])]


def protected(e: RoleContext, oracle: dict | None = None) -> list[str]:
    """Paths no plan puts in a build's files (G-P7), and no build changes unapproved: the
    oracle's, and the guarded ones."""
    return [*oracle_paths(e, oracle), *guarded(e)]


async def protected_unchanged(e: RoleContext, head: str | None) -> list[str]:
    """G-INT2: at ``head``, every protected acceptance test and support file is as the oracle's
    latest passed commit has it (a fix's, once there is one)."""
    oracle = next((x for x in reversed(list(e.tickets.t.values())) if x.type == "agent:oracle"
                   and x.status == "passed" and x.sha), None)
    if oracle is None or head is None:
        return []
    orc = _json(pointer(e, "oracle"))
    return [f"G-INT2: protected acceptance test {p} changed since the oracle's commit"
            for p in [*orc.get("acceptance_tests", []), *orc.get("protected_support", [])]
            if await e.git.blob(head, p) != await e.git.blob(oracle.sha, p)]


def _json(text: str | None) -> dict:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


REGISTRY: dict[str, RoleHandler] = {}


def register(cls: type[RoleHandler]) -> type[RoleHandler]:
    inst = cls()
    for role in cls.roles:
        if role in REGISTRY:
            raise ValueError(f"role {role!r} registered twice")
        REGISTRY[role] = inst
    return cls


def handler_for(role: str) -> RoleHandler:
    try:
        return REGISTRY[role]
    except KeyError:
        raise ValueError(f"no handler registered for role {role!r}") from None
