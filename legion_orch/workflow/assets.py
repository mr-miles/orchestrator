"""Load souls, skills, prompts and role bindings from the assets folder.

Layout (naming convention)::

    assets/
      roles.yaml             role -> soul, skills, write scope, required docs, git requirement
      souls/<soul>.md        agent persona, injected verbatim
      skills/<skill>.md      skill: frontmatter with only a ``description`` (when to load it),
                             then the body; every run gets every skill as ``legion-<skill>``
      prompts/<role>.md      task prompt template (file name = role name)
      workflows/<name>.yaml  a workflow: skeleton, tier policies and hooks (tickets/spec.py); the
                             folder is optional but then holds ``default.yaml``; each one also
                             generates the ``workflow`` skill of its changes

Skills are not put in the prompt. Each is rendered with the run's variables into an Agent Skill
(``legion-<skill>/SKILL.md``) that the agent loads on demand; the prompt only names the ones the
role should load first (``roles.yaml`` ``skills``).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from ..store import SchemaRegistry
from .tickets.model import Ticket
from .tickets.spec import SKILL_DESCRIPTION as WORKFLOW_SKILL_DESCRIPTION
from .tickets.spec import SpecError, WorkflowSpec
from .tickets.spec import render as render_workflow

WORKFLOWS_DIR, DEFAULT_WORKFLOW, WORKFLOW_SKILL = "workflows", "default", "workflow"
CONTROLLER_TYPES = {"sync", "verify", "preflight", "baseline", "can-merge", "provision", "publish"}
VAR_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
WORKFLOW_NAME_RE = re.compile(r"^[a-z0-9-]+$")

TEMPLATE_VARS = {
    # the change
    "change_id", "goal", "tier", "repo_name", "change_base_sha", "oracle_sha",
    # the ticket and its round
    "ticket_id", "ticket_goal", "work_json", "attempt", "role", "context_refs", "findings",
    "outputs", "write_scope", "task_soul",
    # where it works
    "workdir", "branch", "base_sha", "head_sha", "judged_sha", "sandbox_tools",
    # role-specific (their handlers bind them)
    "environment", "scaffold",
}
ROLE_KEYS = {"soul", "skills", "git", "repo", "souls_allowed", "report_schema",
             "outputs", "work_schema"}
OUTPUT_RE = re.compile(r"^[a-z][a-z0-9-]*\*?$")


class AssetError(Exception):
    pass


class GitMode(StrEnum):
    """What git result a role's report must attest (G-R5, G-RV1)."""
    NONE = "none"              # no git result
    COMMIT = "commit"          # its own pushed commit (G-B2)
    CANDIDATE = "candidate"    # the candidate under review
    HEAD = "head"              # the integration head


@dataclass
class Role:
    name: str
    soul: str
    skills: list[str]
    git: GitMode
    repo: bool = True
    souls_allowed: list[str] = field(default_factory=list)
    report_schema: str = "report-none"
    outputs: list[str] = field(default_factory=list)   # names it must produce (``x*``: a list)
    work_schema: str | None = None      # the schema of its tickets' ``work`` (None: no work data)


SKILL_PREFIX = "legion-"
MAX_DESCRIPTION = 1024


@dataclass
class RenderedPrompt:
    soul: str
    prompt: str
    pin: str  # sha256 over soul, prompt and every rendered skill
    skills: dict[str, str] = field(default_factory=dict)   # skill name -> SKILL.md


def skill_name(name: str) -> str:
    return SKILL_PREFIX + name


def _split_frontmatter(path: Path, text: str) -> tuple[dict, str]:
    """A skill file's frontmatter (YAML between ``---`` lines) and its body."""
    if not text.startswith("---\n"):
        raise AssetError(f"{path}: a skill starts with frontmatter: ---, description: ..., ---")
    head, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise AssetError(f"{path}: unterminated frontmatter")
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError as e:
        raise AssetError(f"{path}: invalid frontmatter: {e}") from None
    return meta, body.lstrip("\n")


def render(template: str, variables: dict[str, Any]) -> str:
    missing: list[str] = []

    def sub(m: re.Match) -> str:
        key = m.group(1)
        if key not in variables:
            missing.append(key)
            return m.group(0)
        return str(variables[key])

    out = VAR_RE.sub(sub, template)
    if missing:
        raise AssetError(f"G-D2: unbound template variables: {sorted(set(missing))}")
    return out


class Assets:
    def __init__(self, root: Path):
        self.root = Path(root)
        if not (self.root / "roles.yaml").is_file():
            raise AssetError(f"{self.root}/roles.yaml not found")
        self.souls_ = self._load_dir("souls")
        self.skills_, self.skill_descriptions = self._load_skills()
        self.workflows = self._load_workflows()
        self.prompts_ = self._load_dir("prompts")
        raw = yaml.safe_load((self.root / "roles.yaml").read_text()) or {}
        self.roles: dict[str, Role] = {}
        for name, r in raw.items():
            unknown = set(r) - ROLE_KEYS
            if unknown:
                raise AssetError(f"roles.yaml: role {name}: unknown keys {sorted(unknown)}")
            try:
                git = GitMode(r.pop("git", "none"))
            except ValueError:
                raise AssetError(f"roles.yaml: role {name}: bad git requirement; one of "
                                 f"{[m.value for m in GitMode]}") from None
            self.roles[name] = Role(name=name, git=git, **r)
        self.schemas = SchemaRegistry(self.root / "schemas")
        self.sha = self._hash()
        self._validate()

    def _load_dir(self, sub: str) -> dict[str, str]:
        d = self.root / sub
        if not d.is_dir():
            raise AssetError(f"missing asset folder {d}")
        out = {}
        for p in sorted(d.iterdir()):
            if p.suffix != ".md" or not NAME_RE.match(p.stem):
                raise AssetError(f"{p}: asset files must be named <lowercase-name>.md")
            out[p.stem] = p.read_text()
        return out

    def _load_skills(self) -> tuple[dict[str, str], dict[str, str]]:
        """Skill bodies and descriptions. The frontmatter holds only ``description``: other keys
        (``allowed-tools`` …) would change what the agent may do, which roles.yaml governs."""
        bodies, descriptions = {}, {}
        for name, text in self._load_dir("skills").items():
            path = self.root / "skills" / f"{name}.md"
            meta, body = _split_frontmatter(path, text)
            if set(meta) != {"description"}:
                raise AssetError(f"{path}: frontmatter must hold only 'description' "
                                 f"(got {sorted(meta)})")
            d = meta["description"]
            if not isinstance(d, str) or not d.strip() or len(d) > MAX_DESCRIPTION or "{{" in d:
                raise AssetError(f"{path}: description must be a non-empty string of at most "
                                 f"{MAX_DESCRIPTION} characters, without template variables")
            bodies[name], descriptions[name] = body, d.strip()
        return bodies, descriptions

    @property
    def workflow(self) -> WorkflowSpec | None:
        """The default workflow (None without a workflows folder)."""
        return self.workflows.get(DEFAULT_WORKFLOW)

    def _load_workflows(self) -> dict[str, WorkflowSpec]:
        """Every ``workflows/<name>.yaml``, validated by the spec, by name; and the default's
        skill (``workflow``). Without the folder: none."""
        d = self.root / WORKFLOWS_DIR
        if not d.is_dir():
            return {}
        out: dict[str, WorkflowSpec] = {}
        for p in sorted(d.iterdir()):
            if p.suffix != ".yaml" or not p.is_file() or not WORKFLOW_NAME_RE.match(p.stem):
                raise AssetError(f"{WORKFLOWS_DIR}/{p.name}: workflow files must be named "
                                 f"<lowercase-name>.yaml (letters, digits, -)")
            try:
                out[p.stem] = WorkflowSpec.load(p)
            except SpecError as e:
                raise AssetError(str(e)) from None
        if DEFAULT_WORKFLOW not in out:
            raise AssetError(f"{WORKFLOWS_DIR}/{DEFAULT_WORKFLOW}.yaml is required when "
                             f"{WORKFLOWS_DIR}/ exists")
        if WORKFLOW_SKILL in self.skills_:
            raise AssetError(f"skills/{WORKFLOW_SKILL}.md: the {WORKFLOW_SKILL} skill is generated "
                             f"from {WORKFLOWS_DIR}/")
        self.skills_[WORKFLOW_SKILL] = render_workflow(out[DEFAULT_WORKFLOW])
        self.skill_descriptions[WORKFLOW_SKILL] = WORKFLOW_SKILL_DESCRIPTION
        return out

    def workflow_skill(self, name: str) -> str:
        """The generated ``workflow`` skill body of one workflow."""
        if name not in self.workflows:
            raise AssetError(f"unknown workflow {name!r}; have {sorted(self.workflows)}")
        return render_workflow(self.workflows[name])

    def work_problems(self, ticket: Ticket) -> list[str]:
        """What is wrong with a ticket's ``work`` for its role (the role's work schema; a role
        without one takes none)."""
        role = self.roles.get(ticket.type.partition(":")[2]) if ticket.type.startswith("agent:") \
            else None
        if role is None or role.work_schema is None:
            return [f"{ticket.id}: a {ticket.type} ticket takes no work data"] if ticket.work else []
        return [f"{ticket.id}: {e}" for e in self.schemas.validate_data(role.work_schema,
                                                                          ticket.work)]

    def role_outputs(self) -> dict[str, list[str]]:
        """Each role's output names (a list output without its ``*``)."""
        return {r.name: [o.rstrip("*") for o in r.outputs] for r in self.roles.values()}

    @staticmethod
    def _workflow_types(spec: WorkflowSpec) -> list[str]:
        """Every ticket type the workflow names: its skeleton's, its policies'."""
        types = [t.type for t in spec.skeleton.walk()]
        for name in spec.tiers:
            p = spec.policy(name)
            types += [r.type for r in p.every] + [c for r in p.every for c in r.criteria]
            types += [c for v in p.labels.values() for c in v] + [*p.exists, *p.limits]
            types += [x for r in p.order for x in (r.each, r.after or r.before)]
        return types

    def _workflow_roles(self, spec: WorkflowSpec) -> list[str]:
        """Every role the workflow names: its agent tickets', its policies', its hooks'."""
        return [t.partition(":")[2] for t in self._workflow_types(spec) if t.startswith("agent:")] \
            + list(spec.hooks.values())

    def _workflow_problems(self, spec: WorkflowSpec) -> list[str]:
        bad = sorted({t for t in self._workflow_types(spec) if not self._type_ok(t)})
        out = [f"unknown ticket types {bad} (grouping, human:signoff, agent:<role>, "
               f"controller:{sorted(CONTROLLER_TYPES)})"] if bad else []
        if unknown := sorted(set(self._workflow_roles(spec)) - set(self.roles)):
            out.append(f"roles not in roles.yaml: {unknown}")
        return out + spec.subjects_problems(self.role_outputs())

    @staticmethod
    def _type_ok(t: str) -> bool:
        kind, _, x = t.partition(":")
        return t in ("grouping", "human:signoff") or (kind == "agent" and bool(x)) \
            or (kind == "controller" and x in CONTROLLER_TYPES)

    def _files(self) -> list[Path]:
        return [self.root / "roles.yaml", self.root / "schemas" / "bindings.yaml"] + sorted(
            (self.root / WORKFLOWS_DIR).glob("*.yaml")) + sorted(
            p for sub in ("souls", "skills", "prompts") for p in (self.root / sub).glob("*.md")) \
            + sorted((self.root / "schemas").glob("*.json"))

    def _hash(self) -> str:
        h = hashlib.sha256()
        for p in self._files():
            h.update(str(p.relative_to(self.root)).encode() + b"\0" + p.read_bytes() + b"\0")
        return "sha256:" + h.hexdigest()

    def manifest(self) -> dict[str, str]:
        """sha256 of each asset file (relative path -> hash), as loaded: which files a later
        asset set changed. The set's ``sha`` covers exactly these files."""
        return {str(p.relative_to(self.root)): "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self._files()}

    def check_prompt(self, role: str, template_vars: dict[str, str], pin: str | None,
                     recorded: dict[str, str] | None = None,
                     workflow: str = DEFAULT_WORKFLOW) -> dict[str, Any]:
        """Whether these assets reproduce a run's prompt: re-rendered with the run's template
        values, ``exact`` if the pin matches. Otherwise ``changed`` lists the asset files that
        differ from the manifest recorded for the run's asset set (``recorded``)."""
        out: dict[str, Any] = {"status": "unknown", "changed": []}
        try:
            if template_vars and role in self.roles:
                if self.render_role(role, dict(template_vars), workflow).pin == pin:
                    return {"status": "exact", "changed": []}
                out["status"] = "changed"
        except AssetError as e:
            out = {"status": "changed", "changed": [], "error": str(e)}
        if recorded is not None:
            now = self.manifest()
            out["changed"] = sorted(k for k in set(now) | set(recorded) if now.get(k) != recorded.get(k))
        return out

    # ------------------------------------------------------------------ queries
    def soul(self, name: str) -> str:
        return self._get(self.souls_, "souls", name)

    def soul_text(self, name: str) -> str:
        """A soul's text, or none (a soul no longer in the asset set)."""
        return self.souls_.get(name, "")

    def skill(self, name: str) -> str:
        return self._get(self.skills_, "skills", name)

    def prompt(self, role: str) -> str:
        return self._get(self.prompts_, "prompts", role)

    def souls(self) -> list[str]:
        return sorted(self.souls_)

    def _get(self, table: dict[str, str], sub: str, name: str) -> str:
        try:
            return table[name]
        except KeyError:
            raise AssetError(f"G-D2: asset {sub}/{name}.md not found in {self.root}") from None

    def _validate(self) -> None:
        used_souls = set()
        for role in self.roles.values():
            for s in role.souls_allowed or [role.soul]:
                if "{{" not in s:
                    self.soul(s)
                    used_souls.add(s)
            for sk in role.skills:
                self.skill(sk)
            self.prompt(role.name)
        self.skill("harness-contract")              # every prompt tells the agent to load it
        for role in self.roles.values():
            if bad := [o for o in role.outputs if not OUTPUT_RE.match(o)]:
                raise AssetError(f"roles.yaml {role.name}: bad output names {bad}")
        for name, spec in self.workflows.items():
            if errs := self._workflow_problems(spec):
                raise AssetError(f"{WORKFLOWS_DIR}/{name}.yaml: " + "; ".join(errs))
        used_schemas = {n for _, n in self.schemas.bindings} \
            | {r.report_schema for r in self.roles.values()} \
            | {r.work_schema for r in self.roles.values() if r.work_schema}
        for r in self.roles.values():
            if r.work_schema and r.work_schema not in self.schemas.schemas:
                raise AssetError(f"role {r.name}: unknown work_schema {r.work_schema!r}")
        for r in self.roles.values():
            if r.report_schema not in self.schemas.schemas:
                raise AssetError(f"role {r.name}: unknown report_schema {r.report_schema!r}")
            if self.schemas.get(r.report_schema).fmt != "json":
                raise AssetError(f"role {r.name}: report_schema must be a JSON schema")
        orphans = ([f"schemas/{s}.json" for s in set(self.schemas.schemas) - used_schemas]
                   + [f"souls/{s}.md" for s in set(self.souls_) - used_souls]
                   + [f"prompts/{p}.md" for p in set(self.prompts_) - set(self.roles)])
        if orphans:
            raise AssetError(f"asset files not referenced by roles.yaml: {sorted(orphans)}")
        for table, sub in ((self.souls_, "souls"), (self.skills_, "skills"), (self.prompts_, "prompts")):
            for name, body in table.items():
                for var in VAR_RE.findall(body):
                    if var not in TEMPLATE_VARS:
                        raise AssetError(f"{sub}/{name}.md: unknown template variable {{{{{var}}}}}")
        for name, role in self.roles.items():
            for var in VAR_RE.findall(role.soul):
                if var not in TEMPLATE_VARS:
                    raise AssetError(f"roles.yaml {name}: unknown variable {{{{{var}}}}}")

    # ------------------------------------------------------------------ render
    def render_role(self, role_name: str, variables: dict[str, Any],
                    workflow: str = DEFAULT_WORKFLOW) -> RenderedPrompt:
        """G-D2: resolve soul, skills and prompt; bind every variable; compute the pin. The
        ``workflow`` skill is that of the given workflow."""
        role = self.roles[role_name]
        soul_name = render(role.soul, variables)
        if role.souls_allowed and soul_name not in role.souls_allowed:
            raise AssetError(f"G-D2: soul {soul_name!r} not allowed for role {role_name}")
        soul = self.soul(soul_name)
        prompt = render(self.prompt(role_name), variables).rstrip() + "\n\n" + self._load_first(role)
        skills = {skill_name(n): self._skill_md(n, variables) for n in sorted(self.skills_)}
        if WORKFLOW_SKILL in self.skills_:
            skills[skill_name(WORKFLOW_SKILL)] = self._skill_md(
                WORKFLOW_SKILL, variables, self.workflow_skill(workflow))
        h = hashlib.sha256((soul + "\x00" + prompt).encode())
        for n, text in skills.items():
            h.update(b"\x00" + n.encode() + b"\x00" + text.encode())
        return RenderedPrompt(soul=soul, prompt=prompt, pin="sha256:" + h.hexdigest(), skills=skills)

    @staticmethod
    def _load_first(role: Role) -> str:
        names = ", ".join(f"`{skill_name(s)}`" for s in role.skills if s != "harness-contract")
        return ("Your instructions are legion skills (Skill tool). Load `legion-harness-contract` "
                "before your first action" + (f", and when you reach their step: {names}" if names
                                              else "") + ". Load any other legion skill whose "
                "description fits what you are doing.\n")

    def _skill_md(self, name: str, variables: dict[str, Any], body: str | None = None) -> str:
        """``SKILL.md`` of one skill, rendered with the run's variables."""
        head = yaml.safe_dump({"name": skill_name(name), "description": self.skill_descriptions[name]},
                              sort_keys=False, allow_unicode=True, width=10_000)
        return f"---\n{head}---\n\n{render(self.skill(name) if body is None else body, variables)}"
