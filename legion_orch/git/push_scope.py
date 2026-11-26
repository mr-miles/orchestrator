"""The paths a push may change, checked by the hub's ``pre-receive`` hook (and, the same rules,
by the host at report time).

A :class:`PushScope` is ordered rules over paths: the first rule a path matches decides – an
``allow`` lets it through, any other names why it is refused; a path no rule matches is refused
as ``outside``. The diff is cumulative, ``base..new`` (two dots, as the host's report-time check
reads it), so a push that reverts an earlier push's out-of-scope change is let through.

The HTTP layer passes a grant's scope to the hook as JSON in ``LEGION_SCOPE`` (and the host's
interpreter in ``LEGION_PYTHON``); the hook runs ``python -m legion_orch.git.push_hook`` with
the ``old new ref`` lines on stdin (its entry point is ``push_hook``, a module of its own:
the package already imports this one), which prints what is refused – git shows it to the pusher –
and exits 1 if anything is. A scope too big for the environment fails the push: never open.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field

from .remote import path_matches

ZERO = "0" * 40


@dataclass(frozen=True)
class Rule:
    kind: str                        # what the rule is about: the caller's name for it
    patterns: tuple[str, ...] = ()   # exact paths, ``dir/`` prefixes or globs (path_matches)
    allow: bool = False
    why: str = ""                    # a refusal: the message, ``{path}`` filled in
    hint: bool = True                # a refusal that the scope's hint answers


@dataclass(frozen=True)
class PushScope:
    gid: str                         # the guard the messages name (G-B3, G-O1)
    base: str                        # what the push's changes are counted from
    rules: tuple[Rule, ...] = ()
    outside: Rule = field(default_factory=lambda: Rule(
        "outside", why="{path} is outside the allowed paths"))
    hint: str = ""                   # said once after refusals it answers

    def rule_for(self, path: str) -> Rule:
        """The rule that decides ``path``: the first that matches, else ``outside``."""
        return next((r for r in self.rules if any(path_matches(path, p) for p in r.patterns)),
                    self.outside)

    def violations(self, changed: list[str]) -> list[str]:
        """What is refused among ``changed`` (empty: all of it is in scope)."""
        refused = [(p, r) for p in changed if not (r := self.rule_for(p)).allow]
        out = [f"{self.gid}: {r.why.replace('{path}', p)}" for p, r in refused]
        if self.hint and any(r.hint for _, r in refused):
            out.append(f"{self.gid}: {self.hint}")
        return out

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, text: str) -> PushScope:
        d = json.loads(text)
        rule = lambda r: Rule(**{**r, "patterns": tuple(r["patterns"])})   # noqa: E731
        return cls(gid=d["gid"], base=d["base"], rules=tuple(rule(r) for r in d["rules"]),
                   outside=rule(d["outside"]), hint=d["hint"])


def changed_paths(base: str, new: str) -> list[str]:
    """``base..new`` in the repository the hook runs in (the pushed objects are readable)."""
    out = subprocess.run(["git", "diff", "--name-only", "--no-renames", f"{base}..{new}"],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def check(scope: PushScope, updates: list[tuple[str, str, str]]) -> list[str]:
    """Every refusal among ``(old, new, ref)`` updates. A ref deletion (``new`` all zeros) has no
    commit to diff and is skipped – the hook refuses deletions before it gets here."""
    return [v for _, new, _ in updates if new != ZERO
            for v in scope.violations(changed_paths(scope.base, new))]
