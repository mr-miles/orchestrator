"""Load souls, skills, prompts and role bindings from WORKFLOW.md.

WORKFLOW.md is the single source of truth. Tagged blocks look like::

    <!-- asset: soul/senior-developer -->
    ~~~~markdown
    ...
    ~~~~
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ASSET_RE = re.compile(
    r"<!--\s*asset:\s*([A-Za-z0-9_/-]+)\s*-->\s*\n~~~~[A-Za-z]*\n(.*?)\n~~~~[ \t]*(?:\n|$)",
    re.DOTALL,
)
VAR_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
GUARD_ID_RE = re.compile(r"\|\s*((?:G|H)-[A-Z]+[0-9]+|H-[0-9]+)\s*\|")

TEMPLATE_VARS = {
    "change_id", "goal", "tier", "repo_name", "base_sha", "head_sha", "branch", "task_id",
    "task_title", "task_json", "cycle", "role", "context_refs", "feedback", "candidate_sha",
    "write_scope", "required_docs", "workdir", "task_soul",
}


class AssetError(Exception):
    pass


@dataclass
class Role:
    name: str
    soul: str
    skills: list[str]
    write: list[str]
    required_docs: list[str]
    git: str  # none | commit | candidate | head
    repo: bool = True
    souls_allowed: list[str] = field(default_factory=list)


@dataclass
class RenderedPrompt:
    soul: str
    prompt: str
    pin: str  # sha256 over soul + prompt

    @property
    def combined(self) -> str:
        return self.soul + "\n\n" + self.prompt


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
    def __init__(self, path: Path):
        self.path = Path(path)
        self.text = self.path.read_text()
        self.doc_sha = "sha256:" + hashlib.sha256(self.text.encode()).hexdigest()
        self.blocks: dict[str, str] = {}
        for m in ASSET_RE.finditer(self.text):
            key = m.group(1)
            if key in self.blocks:
                raise AssetError(f"duplicate asset block {key!r} in {path}")
            self.blocks[key] = m.group(2)
        if "roles" not in self.blocks:
            raise AssetError("WORKFLOW.md has no `roles` asset block")
        raw = yaml.safe_load(self.blocks["roles"])
        self.roles: dict[str, Role] = {}
        for name, r in raw.items():
            self.roles[name] = Role(name=name, **r)
        self._validate()

    # ------------------------------------------------------------------ queries
    def soul(self, name: str) -> str:
        return self._get(f"soul/{name}")

    def skill(self, name: str) -> str:
        return self._get(f"skill/{name}")

    def prompt(self, role: str) -> str:
        return self._get(f"prompt/{role}")

    def souls(self) -> list[str]:
        return sorted(k.split("/", 1)[1] for k in self.blocks if k.startswith("soul/"))

    def documented_guard_ids(self) -> set[str]:
        return set(GUARD_ID_RE.findall(self.text))

    def _get(self, key: str) -> str:
        try:
            return self.blocks[key]
        except KeyError:
            raise AssetError(f"G-D2: asset {key!r} not found in {self.path.name}") from None

    def _validate(self) -> None:
        for role in self.roles.values():
            souls = role.souls_allowed or [role.soul]
            for s in souls:
                if "{{" not in s:
                    self.soul(s)
            for sk in role.skills:
                self.skill(sk)
            self.prompt(role.name)
            if role.git not in {"none", "commit", "candidate", "head"}:
                raise AssetError(f"role {role.name}: bad git requirement {role.git!r}")
        for key, body in self.blocks.items():
            for var in VAR_RE.findall(body):
                if var not in TEMPLATE_VARS:
                    raise AssetError(f"asset {key}: unknown template variable {{{{{var}}}}}")

    # ------------------------------------------------------------------ render
    def render_role(self, role_name: str, variables: dict[str, Any]) -> RenderedPrompt:
        """G-D2: resolve soul, skills and prompt; bind every variable; compute the pin."""
        role = self.roles[role_name]
        soul_name = render(role.soul, variables)
        if role.souls_allowed and soul_name not in role.souls_allowed:
            raise AssetError(f"G-D2: soul {soul_name!r} not allowed for role {role_name}")
        soul = self.soul(soul_name)
        parts = [render(self.prompt(role_name), variables)]
        for sk in role.skills:
            parts.append(render(self.skill(sk), variables))
        prompt = "\n\n---\n\n".join(parts)
        pin = "sha256:" + hashlib.sha256((soul + "\x00" + prompt).encode()).hexdigest()
        return RenderedPrompt(soul=soul, prompt=prompt, pin=pin)

    def role_paths(self, role_name: str, variables: dict[str, Any]) -> tuple[list[str], list[str]]:
        role = self.roles[role_name]
        return ([render(w, variables) for w in role.write],
                [render(d, variables) for d in role.required_docs])
