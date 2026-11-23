"""The workflow, as ``assets/workflow.yaml`` specifies it (plan: ~/plans/tickets-design.md §6, §7):

* ``skeleton`` – the root every change starts from: ``requirements``, ``plan``, ``delivery``,
  ``evaluate``, ``release``, with the bare minimum under them. A ticket with ``tiers`` is linked
  only at those tiers – by the sign-off that applies ``tier`` (H-1), once the tier is known;
* ``tiers`` – per risk tier, the rules a tree must keep, all in terms of ticket types – never ids,
  which are only names: the types it must contain (``exists``), the order they must run in
  (``order``: each ticket of one type after – or before – one of another), the criteria each type
  has (``every``, and per risk label ``labels``) – and the attempt limits per type; the policy
  guard (:func:`check_policy`) refuses a tree that breaks one; a tier may ``extend`` another;
* ``criteria`` – per criterion type, whether a failure may be waived;
* ``hooks`` – the side processes: ``on_complete`` (after any ending), ``on_escalation``.

A ticket's ``subtasks`` are groups in order: a list item is a ticket (a group of one) or a list of
tickets (run in parallel). :func:`render` writes the whole as the ``workflow`` skill, for the
planner and the resolver."""
from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .model import GROUPING, Severity, Status, Ticket
from .outputs import TIER, subjects
from .rules import Add, Replan, precedes, validate

CLOSED = {Status.FAILED, Status.CANCELLED}


class SpecError(ValueError):
    pass


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TicketSpec(_Strict):
    id: str
    type: str = GROUPING
    goal: str = ""
    labels: list[str] = []
    inputs: list[str] = []
    subtasks: list[list[TicketSpec]] = []
    criteria: list[TicketSpec] = []
    severity: Severity | None = None            # (criteria) default: rework; escalate on grouping
    signs: list[str] = []
    applies: str | None = None
    tiers: list[str] | None = None              # linked only at these tiers

    @field_validator("subtasks", mode="before")
    @classmethod
    def _groups(cls, v: object) -> object:
        """A list item is one ticket (a group of one) or a group."""
        return [g if isinstance(g, list) else [g] for g in v] if isinstance(v, list) else v

    def walk(self) -> Iterator[TicketSpec]:
        yield self
        for t in [m for g in self.subtasks for m in g] + self.criteria:
            yield from t.walk()


class EveryRule(_Strict):
    """Every live ticket of ``type`` has criteria of these types."""
    type: str
    criteria: list[str]


class OrderRule(_Strict):
    """Every live ticket of type ``each`` comes ``after`` one of a type (one is finished before it
    starts) – or ``before`` one."""
    each: str
    after: str | None = None
    before: str | None = None

    @model_validator(mode="after")
    def _one(self) -> OrderRule:
        if (self.after is None) == (self.before is None):
            raise ValueError(f"order rule for {self.each}: give exactly one of after, before")
        return self

    def text(self) -> str:
        return (f"every `{self.each}` comes after a `{self.after}`" if self.after else
                f"every `{self.each}` comes before a `{self.before}`")


class TierPolicy(_Strict):
    extends: str | None = None
    exists: list[str] = []                      # ticket types the tree must contain
    order: list[OrderRule] = []
    every: list[EveryRule] = []
    labels: dict[str, list[str]] = {}           # risk label -> criteria its code tickets have
    limits: dict[str, int] = {}                 # ticket type -> attempts


class CriterionType(_Strict):
    waivable: bool = True


class WorkflowSpec(_Strict):
    hooks: dict[Literal["on_complete", "on_escalation"], str] = {}
    criteria: dict[str, CriterionType] = {}
    skeleton: TicketSpec
    tiers: dict[str, TierPolicy] = Field(min_length=1)

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path) -> WorkflowSpec:
        try:
            spec = cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})
        except (ValidationError, yaml.YAMLError) as e:
            raise SpecError(f"{path}: {e}") from None
        if errs := spec.problems():
            raise SpecError(f"{path}: " + "; ".join(errs))
        return spec

    def problems(self) -> list[str]:
        """What makes the file unusable: the skeleton is not a valid tree at some tier, a tier
        extends an unknown one (or itself), the skeleton itself breaks a tier's order."""
        out = []
        ids = [t.id for t in self.skeleton.walk()]
        if dup := sorted({i for i in ids if ids.count(i) > 1}):
            out.append(f"skeleton: duplicate ids {dup}")
        for name in self.tiers:
            try:
                policy = self.policy(name)
            except SpecError as e:
                out.append(str(e))
                continue
            built = {t.id: t for t in self.skeleton_at(name)}
            out += [f"skeleton at {name}: {e}" for e in validate(built)]
            out += [f"skeleton at {name}: {e}" for e in _order(policy, name, built, partial=True)]
        for t in self.skeleton.walk():
            for tier in t.tiers or []:
                if tier not in self.tiers:
                    out.append(f"skeleton {t.id}: unknown tier {tier!r}")
        return out

    def subjects_problems(self, role_outputs: Mapping[str, list[str]]) -> list[str]:
        """Whether every sign-off in the skeleton (at every tier) signs outputs its owner's work
        produces – the roles' declared outputs standing in for reports –, applies ``tier`` or one
        of them, and whether every input names an output some sign-off approves."""
        def has(x: Ticket, name: str) -> bool:
            return x.type.startswith("agent:") and x.role != "criterion" \
                and name in role_outputs.get(x.type.partition(":")[2], [])
        out: list[str] = []
        signed: set[str] = set()
        for name in self.tiers:
            built = {t.id: t for t in self.skeleton_at(name)}
            for x in built.values():
                if not x.signs and not x.applies:
                    continue
                _, errs = subjects(built, x.id, has)
                out += [f"skeleton at {name}: {e}" for e in errs if e not in out]
                signed |= set(x.signs)
                if x.applies and x.applies != TIER and x.applies not in x.signs:
                    out.append(f"{x.id} applies {x.applies!r}, which it does not sign")
        for t in self.skeleton.walk():
            out += [f"{t.id}: input {i!r} is no sign-off's output" for i in t.inputs
                    if i not in signed]
        return list(dict.fromkeys(out))

    def policy(self, tier: str) -> TierPolicy:
        """The tier's policy with what it extends folded in."""
        seen: list[str] = []
        chain: list[TierPolicy] = []
        name: str | None = tier
        while name is not None:
            if name in seen or name not in self.tiers:
                raise SpecError(f"tiers.{tier}: extends {'itself' if name in seen else name!r}")
            seen.append(name)
            chain.append(self.tiers[name])
            name = self.tiers[name].extends
        out = TierPolicy()
        for p in reversed(chain):                 # the base first, then what extends it
            out.exists = list(dict.fromkeys(out.exists + p.exists))
            out.order = out.order + [r for r in p.order if r not in out.order]
            for k, v in p.labels.items():
                out.labels[k] = list(dict.fromkeys(out.labels.get(k, []) + v))
            out.every = [r for r in out.every if r.type not in {x.type for x in p.every}] + p.every
            out.limits |= p.limits
        return out

    # ------------------------------------------------------------------ building
    def skeleton_at(self, tier: str | None) -> list[Ticket]:
        """The skeleton as tickets – at ``tier`` with its tiered tickets, at None without
        them (a change's start: its tier is not known yet)."""
        out: list[Ticket] = []
        self._build(self.skeleton, None, "root", tier, out)
        return out

    def tier_additions(self, tier: str, tickets: dict[str, Ticket]) -> Replan:
        """The tier's skeleton tickets, as the replan H-1's sign-off applies – each where the
        skeleton has it, its group's position counted among the tickets linked so far – and the
        tier's attempt limits on the tickets not yet started."""
        adds: list[Add] = []
        for parent in self.skeleton.walk():
            if parent.id not in tickets:
                continue
            index = 0
            for group in parent.subtasks:
                tiered = [m for m in group if m.tiers is not None]
                if any(m.id in tickets for m in group):
                    index += 1
                    continue
                if tiered and all(tier in (m.tiers or []) for m in tiered):
                    for i, m in enumerate(group):
                        sub: list[Ticket] = []
                        self._build(m, tickets[parent.id], "subtask", tier, sub)
                        adds.append(Add(sub[0], parent.id, index, new_group=i == 0, tree=sub[1:]))
                    index += 1
        limits = self.policy(tier).limits
        update = {x.id: {"limit": limits[x.type]} for x in tickets.values()
                  if x.status == Status.PENDING and x.type in limits and x.limit != limits[x.type]}
        return Replan(add=adds, update=update)

    def _build(self, s: TicketSpec, parent: Ticket | None, role: str, tier: str | None,
               out: list[Ticket]) -> Ticket | None:
        """``s`` and what is under it, at ``tier``, appended to ``out`` parents first."""
        if s.tiers is not None and tier not in s.tiers:
            return None
        x = Ticket(id=s.id, parent=parent.id if parent else None, type=s.type, goal=s.goal,
                   role=role, labels=s.labels, inputs=s.inputs,  # type: ignore[arg-type]
                   signs=s.signs, applies=s.applies)
        if role == "criterion":            # a grouping ticket's criteria escalate (R4)
            x.severity = s.severity or (Severity.ESCALATE if parent and parent.type == GROUPING
                                        else Severity.REWORK)
        out.append(self.prepare(x, tier))
        for g in s.subtasks:
            if group := [y.id for m in g if (y := self._build(m, x, "subtask", tier, out))]:
                x.subtasks.append(group)
        for c in s.criteria:
            if self._build(c, x, "criterion", tier, out):
                x.criteria.append(c.id)
        return x

    def prepare(self, x: Ticket, tier: str | None) -> Ticket:
        """What the workflow says of a ticket of its type: whether it may be waived, its attempt
        limit at the tier."""
        if x.role == "criterion" and x.type in self.criteria:
            x.waivable = self.criteria[x.type].waivable
        if tier is not None and (limit := self.policy(tier).limits.get(x.type)):
            x.limit = limit
        return x


# ------------------------------------------------------------------------------- the guard
def _live(t: dict[str, Ticket], ids: Iterable[str]) -> list[Ticket]:
    return [t[i] for i in ids if i in t and t[i].status not in CLOSED]


def _labels(t: dict[str, Ticket], x: Ticket) -> set[str]:
    out, y = set(x.labels), x
    while y.parent and y.parent in t:
        y = t[y.parent]
        out |= set(y.labels)
    return out


def _order(p: TierPolicy, tier: str, t: dict[str, Ticket], partial: bool = False) -> list[str]:
    """The order rules a tree breaks. ``partial``: a tree still to be planned (the skeleton) –
    a rule whose other type is not there yet is not broken."""
    live = [x for x in t.values() if x.status not in CLOSED]
    v = []
    for rule in p.order:
        other = rule.after or rule.before
        for x in [x for x in live if x.type == rule.each]:
            ok = any(precedes(t, y.id, x.id) if rule.after else precedes(t, x.id, y.id)
                     for y in live if y.type == other)
            if not ok and any(y.type == other for y in live):
                v.append(f"tier {tier}: {x.id} ({x.type}) must come "
                         f"{'after' if rule.after else 'before'} a {other}")
            elif not ok and not partial:
                v.append(f"tier {tier}: {x.id} ({x.type}) must come "
                         f"{'after' if rule.after else 'before'} a {other}, and there is none")
    return v


def check_policy(spec: WorkflowSpec, tier: str, t: dict[str, Ticket]) -> list[str]:
    """The tier policy's rules the tree breaks (the planner's tree, every replan): by ticket type,
    whatever the tickets are called."""
    p = spec.policy(tier)
    live = [x for x in t.values() if x.status not in CLOSED]
    v = [f"tier {tier}: the plan has no {ty}" for ty in p.exists
         if not any(x.type == ty for x in live)]
    v += _order(p, tier, t)

    def criteria_types(x: Ticket) -> set[str]:
        return {c.type for c in _live(t, x.criteria)}

    for rule in p.every:
        for x in live:
            if x.type == rule.type and x.role != "criterion":
                v += [f"tier {tier}: {x.id} ({x.type}) must have a {ty} criterion"
                      for ty in rule.criteria if ty not in criteria_types(x)]
    for label, types in p.labels.items():
        for x in live:
            if x.role != "criterion" and x.type.startswith("agent:") and label in _labels(t, x):
                v += [f"tier {tier}: {x.id} is labelled {label}: it must have a {ty} criterion"
                      for ty in types if ty not in criteria_types(x)]
    for x in live:
        if x.role == "criterion" and x.type in spec.criteria and x.waivable \
                and not spec.criteria[x.type].waivable:
            v.append(f"{x.id}: a {x.type} criterion cannot be waivable")
    return v


# ------------------------------------------------------------------------------- the skill
SKILL_DESCRIPTION = ("The workflow's skeleton, the ticket model and each risk tier's policy. Load "
                     "when you plan tickets, propose a replan, or need to know what a tier requires.")


def render(spec: WorkflowSpec) -> str:
    """The ``workflow`` skill's body: the skeleton, the rules a tree must keep, each tier."""
    lines = ["## Skill: The workflow (tickets)", "",
             "A change is a tree of **tickets**. A ticket is its own work (by its type: "
             "`agent:<role>`, `human:signoff`, `controller:<action>`; `grouping` has none), then "
             "its **subtask groups** in order (a group's tickets run in parallel), then its "
             "**criteria** in order, stopping at the first failure. A criterion is a leaf ticket "
             "whose outcome is a verdict; its `severity` says what a failure does: `rework` (the "
             "ticket's work runs again with the findings) or `escalate` (a human decides). A "
             "grouping ticket's criteria always escalate. Risk labels are inherited by subtasks.",
             "", "Tickets are added only after a ticket's completed groups, never under a passed "
             "ticket; a failed ticket is never changed: new tickets replace it.", "",
             "### The skeleton", ""]

    def tree(s: TicketSpec, depth: int) -> None:
        pad = "  " * depth
        extra = []
        if s.tiers:
            extra.append(f"tiers {', '.join(s.tiers)}")
        if s.signs:
            extra.append(f"signs {', '.join(s.signs)}")
        lines.append(f"{pad}- `{s.id}` ({s.type}{'; ' + '; '.join(extra) if extra else ''})"
                     + (f": {s.goal}" if s.goal else ""))
        for i, g in enumerate(s.subtasks):
            if len(g) > 1:
                lines.append(f"{pad}  - group {i + 1}, in parallel:")
            for m in g:
                tree(m, depth + (2 if len(g) > 1 else 1))
        for c in s.criteria:
            lines.append(f"{pad}  - criterion `{c.id}` ({c.type}"
                         + (f", signs {', '.join(c.signs)}" if c.signs else "") + ")")
    tree(spec.skeleton, 0)
    fixed = sorted(k for k, v in spec.criteria.items() if not v.waivable)
    if fixed:
        lines += ["", "Criteria that can never be waived: " + ", ".join(f"`{k}`" for k in fixed)
                  + "."]
    for name in spec.tiers:
        p = spec.policy(name)
        lines += ["", f"### Tier {name}", ""]
        if p.exists:
            lines.append("- the tree contains " + ", ".join(f"`{x}`" for x in p.exists))
        lines += [f"- {r.text()} (finished before it starts)" if r.after else f"- {r.text()}"
                  for r in p.order]
        lines += [f"- every `{r.type}` ticket has criteria "
                  + ", ".join(f"`{x}`" for x in r.criteria) for r in p.every]
        lines += [f"- tickets labelled `{k}` have criteria {', '.join(f'`{x}`' for x in v)}"
                  for k, v in p.labels.items()]
        lines += [f"- `{k}` tickets have {v} attempts" for k, v in p.limits.items()]
    return "\n".join(lines) + "\n"
