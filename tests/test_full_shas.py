"""Shas are kept in full (WORKFLOW.md §1.2): where one comes in – a report's attestations, a
plan's or replan's ``work.context``, a resolver's proposal, an MCP tool's ``sha`` – an
abbreviation exactly one commit starts with is expanded; an ambiguous or unknown one is refused,
saying what to give instead."""
from __future__ import annotations

import subprocess
from collections import Counter

import pytest
from engine_harness import TASKS, Flow, scenario

from legion_orch.git import GitRemote, ShaError
from legion_orch.workflow import attest

pytestmark = pytest.mark.anyio


async def _hub(tmp_path, commits: int = 1) -> tuple[GitRemote, list[str]]:
    """A blank hub with ``commits`` more commits on ``main`` (fast-import: many, quickly)."""
    hub = GitRemote(tmp_path / "git", "app", None, "main")
    first = await hub.sync()
    stream = "".join(
        f"commit refs/heads/main\ncommitter t <t@t> {1700000000 + i} +0000\ndata 4\nc{i:03d}\n"
        + (f"from {first}\n" if i == 0 else "") + "\n" for i in range(commits))
    subprocess.run(["git", "fast-import", "--quiet"], cwd=hub.repo, input=stream, text=True,
                   check=True)
    out = subprocess.run(["git", "rev-list", "main"], cwd=hub.repo, capture_output=True,
                         text=True, check=True).stdout.split()
    return hub, out


async def test_an_unambiguous_abbreviation_is_expanded_and_a_full_sha_kept(tmp_path):
    hub, [head, *_] = await _hub(tmp_path)
    assert await hub.expand_commit(head) == head
    assert await hub.expand_commit(head[:7]) == head
    assert await hub.expand_commit(head[:7].upper()) == head
    ref = f"git:app/src/x.py@{head[:9]}"
    assert await attest.expand(ref, hub) == f"git:app/src/x.py@{head}"
    assert await attest.expand("file:plan/spec.md@2", hub) == "file:plan/spec.md@2"


async def test_an_unknown_or_malformed_sha_is_refused_with_what_to_give(tmp_path):
    hub, _ = await _hub(tmp_path)
    with pytest.raises(ShaError, match="no commit starting 0000000"):
        await hub.expand_commit("0000000")
    with pytest.raises(ShaError, match="no commit 0{40}"):
        await hub.expand_commit("0" * 40)
    for bad in ("abc", "xyz1234", ""):
        with pytest.raises(ShaError, match="full 40-character sha"):
            await hub.expand_commit(bad)


async def test_an_ambiguous_abbreviation_is_refused_naming_the_commits(tmp_path):
    hub, shas = await _hub(tmp_path, 600)                 # 4-hex prefixes collide by now
    prefix, n = Counter(s[:4] for s in shas).most_common(1)[0]
    assert n >= 2, "no colliding prefix: add commits"
    with pytest.raises(ShaError, match=f"{prefix} is ambiguous") as e:
        await hub.expand_commit(prefix)
    assert all(s in str(e.value) for s in shas if s.startswith(prefix))
    assert "full 40-character sha" in str(e.value)
    longer = next(s for s in shas if s.startswith(prefix))
    assert await hub.expand_commit(longer[:12]) == longer           # more characters: one
    refs, errors = await attest.expand_all([f"git:app@{prefix}", f"git:app@{longer[:12]}"], hub)
    assert refs[1] == f"git:app@{longer}" and len(errors) == 1 and "ambiguous" in errors[0]


async def test_check_all_takes_an_abbreviated_attestation(tmp_path):
    hub, [head, *_] = await _hub(tmp_path)
    parsed, v = await attest.check_all([f"git:app@{head[:8]}"], ["app"], None, hub)  # type: ignore[arg-type]
    assert v == [] and parsed[0].sha == head
    _, v = await attest.check_all(["git:app@0000000"], ["app"], None, hub)           # type: ignore[arg-type]
    assert v and v[0].startswith("G-R3:") and "no commit starting 0000000" in v[0]


async def test_a_plan_documents_context_is_expanded_wherever_it_is_nested(tmp_path):
    hub, [head, *_] = await _hub(tmp_path)
    plan = {"add": [{"into": "delivery", "ticket": {
        "id": "G", "type": "grouping", "work": {},
        "subtasks": [[{"id": "T9", "work": {"context": [f"git:app/a.py@{head[:7]}",
                                                        "file:plan/spec.md@1"]}}]],
        "criteria": [{"id": "c", "work": {"context": ["git:app@0000000"]}}]}}],
        "update": {"T1": {"work": {"context": [f"git:app@{head[:10]}"]}}}}
    out, errors = await attest.plan_shas(plan, hub)
    t9 = out["add"][0]["ticket"]["subtasks"][0][0]
    assert t9["work"]["context"] == [f"git:app/a.py@{head}", "file:plan/spec.md@1"]
    assert out["update"]["T1"]["work"]["context"] == [f"git:app@{head}"]
    assert errors == ["add[0].criteria[0].work.context[0]: git:app@0000000: no commit starting "
                      "0000000 in the workspace: give the full 40-character sha"]
    assert plan["update"]["T1"]["work"]["context"] == [f"git:app@{head[:10]}"]   # not changed


@scenario()
async def test_a_replan_with_a_short_sha_in_its_context_applies_with_the_full_one(f: Flow):
    await f.to_build()
    head = await f.ce.git.ref_sha("refs/heads/main")
    work = {k: v for k, v in TASKS[0].items() if k not in ("id", "title", "depends_on")}
    ticket = {"id": "T9", "type": "agent:implementer", "goal": "more", "inputs": ["spec"],
              "work": work | {"files_modified": ["src/more.txt"],
                              "context": [f"git:{f.ce.workspace}/src/greet.txt@{head[:7]}"]},
              "criteria": [{"id": f"{c}:T9", "type": ty} for c, ty in (
                  ("verify", "controller:verify"), ("review", "agent:reviewer"),
                  ("can-merge", "controller:can-merge"))]}
    bad = ticket | {"id": "T8", "work": ticket["work"] | {"context": [
        f"git:{f.ce.workspace}@0000000"]}}
    await f.change_action("pause")
    await f.implement("T1")                               # it drains to its conclusion point
    with pytest.raises(Exception, match="no commit starting 0000000"):
        await f.change_action("replan", plan={"add": [{"into": "builds", "group": 9,
                                                       "ticket": bad}]})
    await f.change_action("replan", plan={"add": [{"into": "builds", "group": 9,
                                                   "ticket": ticket}]})
    await f.change_action("resume")
    await f.settle()
    assert f.ticket("T9").work["context"] == [f"git:{f.ce.workspace}/src/greet.txt@{head}"]


@scenario()
async def test_a_report_attesting_a_short_sha_is_accepted_and_kept_in_full(f: Flow):
    await f.to_build()
    r = f.run("T1", "implementer")
    work = f.ticket("T1").work
    sha = f.commit(r, {p: "hello\n" for p in work["files_modified"]})
    ev = await f.write(r, "evidence.md", "\n".join(f"- `{c}` -> exit 0"
                                                   for c in work["verification"]))
    out = await f.report(r, [f"git:{f.ce.workspace}@{sha[:7]}", ev], outputs={"evidence": ev})
    assert out.accepted, out
    assert f.ce.runs[r.id].report.attestations[0] == f"git:{f.ce.workspace}@{sha}"
    assert f.ticket("T1").sha == sha


@scenario()
async def test_a_resolver_proposal_with_an_unknown_short_sha_goes_back_to_be_corrected(f: Flow):
    from test_resolver import blocked

    from legion_orch.workflow.engine.services.resolver import escalation_key
    await blocked(f)
    head = await f.ce.git.ref_sha("refs/heads/main")
    key = escalation_key(f.ticket("intake"))
    p = {"action": "retry", "payload": {"answer": "English"}, "rationale": "r",
         "attestations": [f"git:{f.ce.workspace}@0000000"]}
    why = await f.ce._resolver.propose("intake", key, p)
    assert why and "no commit starting 0000000" in why
    p["attestations"] = [f"git:{f.ce.workspace}@{head[:8]}"]
    assert await f.ce._resolver.propose("intake", key, p) is None
    assert f.ce.change.proposals["intake"].attestations == [f"git:{f.ce.workspace}@{head}"]
