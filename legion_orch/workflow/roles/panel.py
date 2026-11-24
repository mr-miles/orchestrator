"""reviewer, security, architecture: a judge of a build ticket's commit (G-RV*)."""
from __future__ import annotations

from ...git import path_matches
from .. import guards
from ..guards.work import GUARDED
from ..view import RoleContext
from .base import (
    Report,
    RoleHandler,
    Said,
    beyond_scope_of_owner,
    guarded,
    judged,
    outputs_of_owner,
    register,
)


@register
class Panel(RoleHandler):
    roles = ("reviewer", "security", "architecture")

    def extra_variables(self, e: RoleContext, ticket) -> dict[str, str]:
        """What the judged build changed beyond its scope, each path with its reason and why it
        needed one (a protected path: judged strictly), and the files of the builds alongside
        it; and the review's scope – everything, or (``scope_only``) only those changes."""
        listed = beyond_scope_of_owner(e, ticket)
        owner = e.tickets.t.get(ticket.parent) if ticket.parent else None
        work = owner.work if owner else {}
        strict = guarded(e)
        lines = []
        for b in listed:
            p = b.get("path", "")
            kind = (GUARDED if any(path_matches(p, g) for g in strict) else
                    "forbidden for it" if any(path_matches(p, f)
                                              for f in work.get("files_forbidden", [])) else
                    "outside its files_modified")
            lines.append(f"- `{p}` ({kind}): {b.get('reason', '')}")
        beside = []
        if owner and owner.parent and owner.parent in e.tickets.t:
            for g in e.tickets.t[owner.parent].subtasks:
                if owner.id in g:
                    beside = [e.tickets.t[x] for x in g if x != owner.id and x in e.tickets.t]
        text = "\n".join(lines) if lines else "(none: every change is inside its files)"
        if lines and beside:
            text += "\n\nBuilds running alongside it, and the files they own:\n" + "\n".join(
                f"- {b.id}: {', '.join(b.work.get('files_modified', [])) or '(none)'}"
                for b in beside)
        scope = ("only the changes above: judge each reason – nothing else of the build"
                 if ticket.scope_only else "the whole build, the changes above included")
        return {"beyond_scope": text, "review_scope": scope}

    def extra_context(self, e: RoleContext, ticket) -> list[str]:
        """The judged work's evidence (its owner's outputs)."""
        return outputs_of_owner(e, ticket)

    async def guards(self, e: RoleContext, r: Report) -> list[str]:
        return guards.findings(r.data.findings, r.repo_atts) + guards.verdict(
            r.data, cycle=r.run.attempt, require_checked=True)

    async def on_done(self, e: RoleContext, r: Report) -> Said:
        return judged(r.data.verdict, r.data.findings)
