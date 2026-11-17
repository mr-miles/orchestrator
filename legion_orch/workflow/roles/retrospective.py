"""retrospective (on demand, once a change has ended): an agent studies how the change went and
proposes improvements to the souls, skills, prompts and process.

Agents cannot read ``control/logs/`` through MCP, and reading many large logs through a tool is
slow anyway, so the run gets the change's material as files in its payload
(``<payload>/retro/``, see :func:`bundle`): the agents' whole logs and their digests, the
agents' own reflections, the controller's timeline, run and node records, the change's documents,
the current assets and whether each run's prompt still matches them.
"""
from __future__ import annotations

import json
from typing import Any

from ..model import Node, Run
from ..view import RoleContext
from .base import RoleHandler, register

ROOT = "retro"
HIDDEN = ("control/logs/", "control/timeline/", "control/digests/", "control/reflections/", "control/nodes/",
          "control/runs/", "control/assets/")

README = """# Material for the retrospective of change {cid}

Everything here is a copy; nothing you change here has any effect.

- `reflections/<run>.md` – **start here**: what each agent said, at the end of its work, cost it
  time and where its guidance failed.
- `change.json`, `nodes.json`, `runs.json` – the change; its nodes (feedback, finding history,
  escalations); every run (role, soul, cycle, timings, failure, rejected reports, resumed
  sessions).
- `timeline.jsonl` – the controller's events in order (dispatch, sandbox, verification, rework,
  human actions).
- `digests/<run>.json` – per run: tool calls and errors, repeated commands and reads, slowest tool
  calls, longest model waits, turns and cost.
- `logs/<run>.log` – each run's whole agent log (Claude Code stream-json; `legion_ts` is when the
  controller received each event). Large: grep or `jq` them, guided by the digests.
- `docs/` – the latest version of each of the change's documents (requirements, plan, critiques,
  evidence, reviews, verification, approvals, feedback).
- `assets/` – the current souls, skills, prompts and `roles.yaml`: propose edits against these.
- `prompt-check.json` – per run: `exact` if the current assets reproduce its prompt, otherwise
  the asset files that changed since it ran.
"""


def _runs(e: RoleContext) -> list[Run]:
    return [r for r in e.runs.values() if r.role != "retrospective"]


def bundle(e: RoleContext) -> dict[str, str]:
    """The retrospective's material, path (under ``retro/``) -> text."""
    out: dict[str, str] = {"README.md": README.format(cid=e.cid)}
    dump = lambda v: json.dumps(v, indent=1, sort_keys=True, default=str)  # noqa: E731
    out["change.json"] = dump(e.change.model_dump(mode="json"))
    out["nodes.json"] = dump([n.model_dump(mode="json") for n in e.nodes.values()])
    runs = _runs(e)
    out["runs.json"] = dump([r.model_dump(mode="json", exclude={"assignment"}) for r in runs])
    out["timeline.jsonl"] = e.timeline()
    checks: dict[str, Any] = {}
    manifests: dict[str, dict | None] = {}
    for r in runs:
        if r.assets_sha and r.assets_sha not in manifests:
            path = f"control/assets/{r.assets_sha.removeprefix('sha256:')[:16]}.json"
            manifests[r.assets_sha] = e.json_doc(path)["files"] if e.latest(path) else None
        checks[r.id] = {"role": r.role, "soul": r.soul, "assets_sha": r.assets_sha} | \
            e.assets.check_prompt(r.role, r.template_vars, r.prompt_pin,
                                  manifests.get(r.assets_sha))
    out["prompt-check.json"] = dump(checks)
    for r in runs:
        if text := e.stored_log(r.id):
            out[f"logs/{r.id}.log"] = text
        for rel, key in ((f"control/digests/{r.id}.json", "digests"),
                         (f"control/reflections/{r.id}.md", "reflections")):
            if e.latest(rel):
                out[f"{key}/{rel.rsplit('/', 1)[1]}"] = e.doc(rel).content
    if e.latest("control/logs/provision.log"):
        out["logs/provision.log"] = e.doc("control/logs/provision.log").content
    prefix = e.p("")
    for meta in e.docs.list(prefix):
        rel = meta.path[len(prefix):]
        if not rel.startswith(HIDDEN) and not rel.startswith(f"{ROOT}/"):
            out[f"docs/{rel}"] = e.docs.read(meta.path, meta.version).content
    a = e.assets
    out["assets/roles.yaml"] = (a.root / "roles.yaml").read_text()
    for sub, table in (("souls", a.souls_), ("skills", a.skills_), ("prompts", a.prompts_)):
        for name, body in table.items():
            out[f"assets/{sub}/{name}.md"] = body
    return {f"{ROOT}/{k}": v for k, v in out.items()}


@register
class Retrospective(RoleHandler):
    roles = ("retrospective",)
    judges_head = True            # reads the change's final state; pushes nothing
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        """Earlier changes' retrospectives: whether a lesson is new or keeps coming back."""
        own = e.p("")
        return [m.attestation for m in e.docs.list("changes/")
                if m.path.endswith("/retro/report.md") and not m.path.startswith(own)][-10:]

    def payload_files(self, e: RoleContext, node: Node, run: Run) -> dict[str, str]:
        return bundle(e)
