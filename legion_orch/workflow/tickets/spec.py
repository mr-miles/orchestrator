"""The workflow, as ``assets/workflow.yaml`` specifies it (plan: ~/plans/tickets-design.md §6, §7):

* ``skeleton`` – the root every change starts from: ``requirements``, ``plan``, ``delivery``,
  ``evaluate``, ``release``, with the bare minimum under them. A ticket with ``tiers`` is linked
  only at those tiers – by the sign-off that applies ``tier`` (H-1), once the tier is known;
* ``tiers`` – per risk tier, the tickets and criteria a plan must contain (the policy guard,
  :func:`check_policy`, refuses a tree without them) and the attempt limits per ticket type; a tier
  may ``extend`` another;
* ``criteria`` – per criterion type, whether a failure may be waived;
* ``hooks`` – the side processes: ``on_complete`` (after any ending), ``on_escalation``.

A ticket's ``subtasks`` are groups in order: a list item is a ticket (a group of one) or a list of
tickets (run in parallel). :func:`render` writes the whole as the ``workflow`` skill, for the
planner and the resolver."""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .model import GROUPING, Severity, Status, Ticket
from .rules import Add, Replan, validate

TIER = "tier"                                   # what H-1's sign-off applies
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
    branch: str | None = None
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


class TierPolicy(_Strict):
    extends: str | None = None
    requires: dict[str, list[str]] = {}         # ticket -> tickets that must be under it
    every: list[EveryRule] = []
    labels: dict[str, list[str]] = {}           # risk label -> criteria its code tickets have
    criteria: dict[str, list[str]] = {}         # ticket -> criteria types it has
    last: dict[str, str] = {}                   # ticket -> the type in its last group
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
        extends an unknown one (or itself), a policy names a ticket the skeleton lacks."""
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
            for where in [*policy.requires, *policy.criteria, *policy.last]:
                if where not in ids:
                    out.append(f"tiers.{name}: no skeleton ticket {where!r}")
        for t in self.skeleton.walk():
            for tier in t.tiers or []:
                if tier not in self.tiers:
                    out.append(f"skeleton {t.id}: unknown tier {tier!r}")
        return out

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
            for k, v in p.requires.items():
                out.requires[k] = list(dict.fromkeys(out.requires.get(k, []) + v))
            for k, v in p.criteria.items():
                out.criteria[k] = list(dict.fromkeys(out.criteria.get(k, []) + v))
            for k, v in p.labels.items():
                out.labels[k] = list(dict.fromkeys(out.labels.get(k, []) + v))
            out.every = [r for r in out.every if r.type not in {x.type for x in p.every}] + p.every
            out.last |= p.last
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
                   signs=s.signs, applies=s.applies, branch=s.branch)
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


def _instance(tid: str, name: str) -> bool:
    """``tid`` is ``name`` or a later instance of it (``provision.2``)."""
    return tid == name or tid.startswith(name + ".")


def _under(t: dict[str, Ticket], tid: str) -> list[Ticket]:
    out, todo = [], [tid]
    while todo:
        x = t[todo.pop()]
        kids = _live(t, [m for g in x.subtasks for m in g] + x.criteria)
        out += kids
        todo += [k.id for k in kids]
    return out


def _labels(t: dict[str, Ticket], x: Ticket) -> set[str]:
    out, y = set(x.labels), x
    while y.parent and y.parent in t:
        y = t[y.parent]
        out |= set(y.labels)
    return out


def check_policy(spec: WorkflowSpec, tier: str, t: dict[str, Ticket]) -> list[str]:
    """What the tree lacks that the tier's policy requires (the planner's tree, every replan)."""
    p, v = spec.policy(tier), []

    def find(name: str) -> list[Ticket]:
        return [x for x in t.values() if _instance(x.id, name) and x.status not in CLOSED]

    def criteria_types(x: Ticket) -> set[str]:
        return {c.type for c in _live(t, x.criteria)}

    for where, names in p.requires.items():
        for owner in find(where):
            under = {x.id for x in _under(t, owner.id)}
            for name in names:
                if not any(_instance(i, name) for i in under):
                    v.append(f"tier {tier}: {owner.id} must contain {name}")
    for where, types in p.criteria.items():
        for owner in find(where):
            v += [f"tier {tier}: {owner.id} must have a {ty} criterion"
                  for ty in types if ty not in criteria_types(owner)]
    for rule in p.every:
        for x in t.values():
            if x.type == rule.type and x.role != "criterion" and x.status not in CLOSED:
                v += [f"tier {tier}: {x.id} ({x.type}) must have a {ty} criterion"
                      for ty in rule.criteria if ty not in criteria_types(x)]
    for label, types in p.labels.items():
        for x in t.values():
            if x.role != "criterion" and x.type.startswith("agent:") \
                    and x.status not in CLOSED and label in _labels(t, x):
                v += [f"tier {tier}: {x.id} is labelled {label}: it must have a {ty} criterion"
                      for ty in types if ty not in criteria_types(x)]
    for where, ty in p.last.items():
        for owner in find(where):
            groups = [g for g in owner.subtasks if _live(t, g)]
            if not groups or not any(x.type == ty for x in _live(t, groups[-1])):
                v.append(f"tier {tier}: {owner.id}'s last group must be its {ty}")
    for x in t.values():
        if x.role == "criterion" and x.type in spec.criteria and x.status not in CLOSED \
                and x.waivable and not spec.criteria[x.type].waivable:
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
        lines += [f"- `{k}` contains {', '.join(f'`{x}`' for x in v)}" for k, v in p.requires.items()]
        lines += [f"- `{k}` has criteria {', '.join(f'`{x}`' for x in v)}"
                  for k, v in p.criteria.items()]
        lines += [f"- every `{r.type}` ticket has criteria "
                  + ", ".join(f"`{x}`" for x in r.criteria) for r in p.every]
        lines += [f"- tickets labelled `{k}` have criteria {', '.join(f'`{x}`' for x in v)}"
                  for k, v in p.labels.items()]
        lines += [f"- `{k}`'s last group is its `{v}`" for k, v in p.last.items()]
        lines += [f"- `{k}` tickets have {v} attempts" for k, v in p.limits.items()]
    return "\n".join(lines) + "\n"
