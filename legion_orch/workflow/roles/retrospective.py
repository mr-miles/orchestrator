"""retrospective (on demand, once a change has ended): an agent studies how the change went and
proposes improvements to the souls, skills, prompts and process.

Agents cannot read runs' logs (``control/runs/``) through MCP, and reading many large logs through a tool is
slow anyway, so the run gets the change's material as files in its payload
(``<payload>/retro/``, see :func:`bundle`): the agents' whole logs and their digests, the
agents' own reflections, the controller's timeline, run and ticket records, the change's documents,
the current assets and whether each run's prompt still matches them.
"""
from __future__ import annotations

import json
from typing import Any

from ..model import Run
from ..tickets import Ticket
from ..view import RoleContext
from .base import RoleHandler, register

ROOT = "retro"
HIDDEN = ("control/timeline/", "control/digests/", "control/reflections/", "control/runs/",
          "control/assets/")

README = """# Material for the retrospective of change {cid}

Everything here is a copy; nothing you change here has any effect.

- `reflections/<run>.md` – **start here**: what each agent said, at the end of its work, cost it
  time and where its guidance failed.
- `change.json`, `tickets.json`, `runs.json` – the change; its tickets (the tree, attempts,
  findings, outputs, what each waited for); every run (role, soul, attempt, timings, failure,
  rejected reports, resumed sessions).
- `timeline.jsonl` – the controller's events in order (dispatch, sandbox, verification, rework,
  human actions).
- `digests/<run>.json` – per run: tool calls and errors, repeated commands and reads, slowest tool
  calls, longest model waits, turns and cost.
- `logs/<run>.log` – each run's whole agent log (Claude Code stream-json; `legion_ts` is when the
  controller received each event). Large: grep or `jq` them, guided by the digests.
- `docs/` – the latest version of each of the change's documents (requirements, plan, critiques,
  evidence, reviews, verification, approvals, feedback).
- `assets/` – the current souls, skills, prompts and `roles.yaml`: propose edits against these.
- `findings.json` – the judgement ledger, built by the controller: per ticket with criteria,
  each commit judged with each criterion's verdict and findings, and each finding's first and
  later rounds. Judged commits are pinned in git as `refs/legion/evidence/<run>`:
  `git fetch origin '+refs/legion/*:refs/legion/*'` to diff them.
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
    out["tickets.json"] = dump([x.model_dump(mode="json") for x in e.tickets.t.values()])
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
    out["findings.json"] = dump(review_ledger(e))
    for r in runs:
        if text := e.stored_log(r.id):
            out[f"logs/{r.id}.log"] = text
        for rel, key in ((f"control/digests/{r.id}.json", "digests"),
                         (f"control/reflections/{r.id}.md", "reflections")):
            if e.latest(rel):
                out[f"{key}/{rel.rsplit('/', 1)[1]}"] = e.doc(rel).content
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


def review_ledger(e: RoleContext) -> dict[str, Any]:
    """Every judgement of the change, cross-referenced, for judging the judges: per ticket with
    criteria, each commit judged (by its judged sha, in order) with each criterion's verdict and
    findings, and each finding's first and later appearances (possible escapes: whether the
    faulty code was already there is for the agent to check in git)."""
    out: dict[str, Any] = {}
    for x in e.tickets.t.values():
        if not x.criteria:
            continue
        judged: dict[str, dict] = {}
        for r in e.runs.values():
            if r.ticket not in x.criteria or r.report is None or r.consult:
                continue
            data = r.report.data or {}
            c = judged.setdefault(r.judged_sha or "", {"sha": r.judged_sha, "verdicts": {},
                                                       "findings": []})
            c["verdicts"][r.ticket] = data.get("verdict")
            c["findings"] += [f | {"by": r.ticket, "run": r.id} for f in data.get("findings", [])
                              if isinstance(f, dict)]
        rounds = list(judged.values())
        seen: dict[str, dict] = {}
        for i, c in enumerate(rounds):
            for f in c["findings"]:
                key = f"{f.get('by')}:{f.get('title')}"
                entry = seen.setdefault(key, f | {"first_round": i + 1, "rounds": []})
                entry["rounds"].append(i + 1)
        out[x.id] = {"type": x.type, "status": x.status, "attempts": x.attempt, "sha": x.sha,
                     "rounds": [{"sha": c["sha"], "verdicts": c["verdicts"]} for c in rounds],
                     "findings": list(seen.values()), "last_findings": x.findings}
    return {"about": "Built by the controller from the change's tickets and runs. Judged commits "
                     "are pinned as refs/legion/evidence/<run>; fetch them to diff what each "
                     "judge saw.", "tickets": out}


@register
class Retrospective(RoleHandler):
    roles = ("retrospective",)

    def extra_context(self, e: RoleContext, ticket: Ticket) -> list[str]:
        """Earlier changes' retrospectives: whether a lesson is new or keeps coming back."""
        own = e.p("")
        return [m.attestation for m in e.docs.list("changes/")
                if m.path.endswith("/retro/report.md") and not m.path.startswith(own)][-10:]

    def payload_files(self, e: RoleContext, ticket: Ticket, run: Run) -> dict[str, str]:
        return bundle(e)
