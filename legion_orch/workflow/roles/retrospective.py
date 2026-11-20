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
- `findings.json` – the review ledger, built by the controller: per task, each candidate (sha,
  verification, verdicts) and each finding with the cycle it was first raised, the cycles it came
  back in and the earlier candidates the same role reviewed without raising it; the critique's
  findings; integration; human feedback. Candidates are pinned in git as
  `refs/legion/evidence/<run>`: `git fetch origin '+refs/legion/*:refs/legion/*'` to diff them.
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
    out["findings.json"] = dump(review_ledger(e))
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


def _finding(f: Any) -> dict:
    return {"severity": f.severity, "confidence": f.confidence, "dimension": f.dimension,
            "title": f.title, "fix": f.fix, "attestations": f.attestations,
            "fingerprint": f.fingerprint, "drives_rework": f.high}


def review_ledger(e: RoleContext) -> dict[str, Any]:
    """Every review, critique and verification outcome of the change, cross-referenced, for
    judging the reviews themselves: for each finding, when it was first raised, the cycles it came
    back in, and the earlier candidates the same role reviewed without raising it (possible
    escapes: whether the faulty code was already there is for the agent to check in git)."""
    tasks: dict[str, Any] = {}
    for node in e.build_nodes():
        candidates: dict[int, dict] = {}
        for key, rv in node.reviews.items():
            cycle_s, _, role = key.partition(":")
            if not cycle_s.isdigit():
                continue
            c = candidates.setdefault(int(cycle_s), {"cycle": int(cycle_s), "reviews": {}})
            c["sha"] = c.get("sha") or rv.sha
            c["reviews"][role] = {"verdict": rv.verdict, "run": rv.run, "doc": rv.doc,
                                  "findings": [_finding(f) for f in rv.findings]}
        for cycle_s, ver in node.verification.items():
            if cycle_s.isdigit():
                c = candidates.setdefault(int(cycle_s), {"cycle": int(cycle_s), "reviews": {}})
                c["verification"] = {"sha": ver.sha, "ok": ver.ok,
                                     "failed": [r.command for r in ver.results if r.exit_code]}
        ordered = [candidates[k] for k in sorted(candidates)]
        seen: dict[str, dict] = {}
        for c in ordered:
            for role, rv in c["reviews"].items():
                for f in rv["findings"]:
                    entry = seen.setdefault(f["fingerprint"], f | {
                        "role": role, "first_raised_cycle": c["cycle"], "raised_in_cycles": [],
                        "earlier_reviews": [{"cycle": o["cycle"], "sha": o.get("sha"),
                                             "verdict": o["reviews"][role]["verdict"]}
                                            for o in ordered if o["cycle"] < c["cycle"]
                                            and role in o["reviews"]]})
                    entry["raised_in_cycles"].append(c["cycle"])
        tasks[node.task.id if node.task else node.id] = {
            "node": node.id, "status": node.status, "cycles": node.cycle,
            "accepted_sha": node.accepted_sha,
            "override": node.override.model_dump(mode="json") if node.override else None,
            "candidates": [{k: v for k, v in c.items() if k != "reviews"}
                           | {"verdicts": {r: v["verdict"] for r, v in c["reviews"].items()}}
                           for c in ordered],
            "findings": list(seen.values()),
            "other_feedback": [f.model_dump(mode="json") for f in node.feedback
                               if f.source not in ("review",)],
        }
    critique = [{"run": r.id, "cycle": r.cycle, "verdict": (r.report.data or {}).get("verdict"),
                 "findings": (r.report.data or {}).get("findings", [])}
                for r in e.runs.values() if r.role == "critic" and r.report]
    integration = e.nodes.get("integration")
    return {
        "about": "Built by the controller from the change's records. Candidates are pinned as "
                 "refs/legion/evidence/<run>; fetch them to diff what each review saw.",
        "tasks": tasks,
        "critique": critique,
        "integration": None if integration is None else {
            "status": integration.status,
            "review": (integration.reviews.get("integration").model_dump(mode="json")
                       if "integration" in integration.reviews else None),
            "verification": {k: {"sha": v.sha, "ok": v.ok,
                                 "failed": [r.command for r in v.results if r.exit_code]}
                             for k, v in integration.verification.items()}},
        "human_feedback": [{"node": n.id} | f.model_dump(mode="json")
                           for n in e.nodes.values() if n.kind != "build"
                           for f in n.feedback if f.source in ("feedback", "decisions", "focus")],
    }


@register
class Retrospective(RoleHandler):
    roles = ("retrospective",)
    sees_oracle_commit = False

    def extra_context(self, e: RoleContext, node: Node) -> list[str]:
        """Earlier changes' retrospectives: whether a lesson is new or keeps coming back."""
        own = e.p("")
        return [m.attestation for m in e.docs.list("changes/")
                if m.path.endswith("/retro/report.md") and not m.path.startswith(own)][-10:]

    def payload_files(self, e: RoleContext, node: Node, run: Run) -> dict[str, str]:
        return bundle(e)
