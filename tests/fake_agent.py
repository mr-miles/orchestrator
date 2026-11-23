"""Scripted stand-in for an LLM agent, used by the end-to-end test.

Talks to the controller exactly like a real agent: MCP JSON-RPC over HTTP with the run token,
git clone/push against the controller remote. Deliberately makes mistakes in places to exercise
the guards and rework loops:

* critic: REWORK on the plan's attempt 1, PASS on attempt 2
* T1 reviewer: FAIL on T1's attempt 1 (HIGH finding), PASS afterwards
* T2 implementer: wrong content on attempt 1 -> its verification fails -> rework
* T3 implementer: first report touches an out-of-scope file -> G-B3 rejection -> fixes + re-reports
Each role writes in its ticket's directory and reports the outputs its role names.
"""
import json
import os
import subprocess
import sys
import urllib.request

SB = os.environ.get("SANDBOX_ROOT", "/sandbox")
URL = os.environ["LEGION_MCP_URL"]
# Under OpenShell the gateway injects the bearer; the local runner emulates that via the env.
TOKEN = os.environ["LEGION_ACCESS_TOKEN"]
for _f in ("run.env", "mcp.json", "prompt.md", "soul.md"):
    _p = os.path.join(SB, "payload", _f)
    if os.path.exists(_p):
        assert TOKEN not in open(_p).read(), f"credential leaked into payload {_f}"
assert "LEGION_TOKEN" not in open(os.path.join(SB, "payload", "run.env")).read()
# the local runner gives the agent its own Claude config dir, where run-agent.sh put the skills
_cc = os.environ["CLAUDE_CONFIG_DIR"]
assert _cc == os.path.join(SB, "claude"), _cc
assert os.path.isfile(os.path.join(_cc, "skills", "legion-harness-contract", "SKILL.md"))
REPO = "app"
_id = 0


def call(name, **args):
    global _id
    _id += 1
    body = json.dumps({"jsonrpc": "2.0", "id": _id, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"})
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())["result"]
    text = res["content"][0]["text"] if res.get("content") else "null"
    if res.get("isError"):
        raise RuntimeError(f"{name}: {text}")
    if name == "report" and not args.get("dry_run"):
        out = json.loads(text)
        if out.get("accepted"):
            ACCEPTED.append(args["status"])          # how the task is ended (a2a_reply)
    if "structuredContent" in res and res["structuredContent"] is not None:
        sc = res["structuredContent"]
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(text)


def git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def write(path, content):
    try:
        base = call("docs_read", path=path)["version"]
    except RuntimeError:
        base = None
    return call("docs_write", path=path, content=content, base_version=base)["attestation"]


def wfile(path, content):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def commit_push(msg, branch):
    git("add", "-A")
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:   # a rework may
        git("commit", "-q", "-m", msg)                                           # change nothing
    git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}")
    return git("rev-parse", "HEAD")


ACCEPTED: list[str] = []


def owner_attempt(a) -> int:
    """The attempt of the work a criterion judges (its owner's): a rerun criterion is new."""
    ctx = call("workflow_context")
    return call("ticket_status", ticket=ctx["parent"]["id"])["attempt"]


PLAN = "tickets/design"


def main():
    work = call("inbox")["tasks"][0]                    # the orchestrator's task: the assignment
    assert work["from"] == "orchestrator" and work["kind"] == "work", work
    a = work["assignment"]
    role, attempt = a["role"], a["attempt"]
    me = f"tickets/{a['ticket_id']}"                    # its ticket's directory: its write scope
    call("progress", message=f"{role} starting attempt {attempt}", percent=5)
    if role == "intake":
        req = write(f"{me}/requirements.md",
                    "# Requirements\n\n- REQ-1: greet.txt says hello\n- REQ-2: bye.txt says goodbye\n")
        signals = [{"name": n, "score": 0, "rationale": "not affected"} for n in (
            "security", "authorization", "data_migration", "external_side_effect", "ui",
            "performance", "infrastructure", "irreversible_action", "novelty_uncertainty",
            "verification_quality")]
        signals += [{"name": "public_api", "score": 2, "rationale": "files are the public API"},
                    {"name": "scope_breadth", "score": 1, "rationale": "two files"}]
        if "R3" in a["goal"]:
            signals = [x for x in signals if x["name"] != "security"] + [
                {"name": "security", "score": 3, "rationale": "pretend secrets are involved"}]
        risk_doc = json.dumps({"signals": signals})
        # dry runs over MCP: they write and count nothing
        bad = call("report", status="DONE", summary="requirements captured", attestations=[req],
                   outputs={"requirements": req}, dry_run=True)
        assert bad["dry_run"] and not bad["would_accept"] and not bad["accepted"], bad
        assert any(v.startswith("G-R4") for v in bad["violations"]), bad
        check = call("docs_write", path=f"{me}/risk.json", content=risk_doc, dry_run=True)
        assert check["dry_run"] and check["version"] == 1 and not check["unchanged"], check
        risk = write(f"{me}/risk.json", risk_doc)
        assert risk == check["attestation"], (risk, check)
        out = {"requirements": req, "risk": risk}
        ok = call("report", status="DONE", summary="requirements captured", attestations=[req, risk],
                  outputs=out, dry_run=True)
        assert ok["would_accept"] and ok["attempts_left"] == a["attempts_left"], (ok, a)
        print(call("report", status="DONE", summary="requirements captured",
                   attestations=[req, risk], outputs=out))
    elif role == "planner":
        spec = write(f"{me}/spec.md", f"# Spec (attempt {attempt})\n\nWrite greet/bye files and docs.\n")
        deltas = [write(f"{me}/specs/{r}.md", f"# {r} delta spec (attempt {attempt})\n\n## Current "
                        f"behaviour\nmissing\n\n## Change\nadd it\n\n## Acceptance criteria\n- works\n")
                  for r in ("REQ-1", "REQ-2")]
        oracle = write(f"{me}/oracle.json", json.dumps({
            "acceptance_tests": ["tests/acceptance/test_greet.sh"],
            "acceptance_commands": ["sh tests/acceptance/test_greet.sh"],
            "integration_commands": ["test -f docs/README.md"],
            "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "hello"},
                         {"id": "AC-2", "requirement": "REQ-2", "text": "goodbye"}],
            "surfaces": [{"id": "files", "description": "the repository files are the interface",
                          "commands": ["test -f README.md"], "pins": ["README.md"]}]}))
        env = write(f"{me}/environment.json", json.dumps({
            "summary": "the default image", "profiles": {"default": {"tools": []}}, "roles": {},
            "rationale": "no environment stage"}))
        r3 = a["tier"] == "R3"
        crit = ["verify", "review", *(["security", "architecture"] if r3 else []), "can-merge"]
        kinds = {"verify": "controller:verify", "review": "agent:reviewer",
                 "security": "agent:security", "architecture": "agent:architecture",
                 "can-merge": "controller:can-merge"}

        def build(tid, title, soul, reqs, files, verification, group, join=False):
            return {"into": "delivery", "group": group, "join": join, "ticket": {
                "id": tid, "type": "agent:implementer", "goal": title, "inputs": ["spec", "specs"],
                "work": {"soul": soul, "requirements": reqs, "files_modified": files,
                         "instructions": title, "acceptance": [title], "verification": verification},
                "criteria": [{"id": f"{c}:{tid}", "type": kinds[c]} for c in crit]}}
        plan = {"add": [
            build("T1", "write hello", "backend-engineer", ["REQ-1"], ["src/greet.txt"],
                  ["grep -q hello src/greet.txt"], 0),
            build("T2", "write goodbye", "senior-developer", ["REQ-2"], ["src/bye.txt"],
                  ["grep -q goodbye src/bye.txt"], 0, join=True),
            build("T3", "document", "senior-developer", ["REQ-1"], ["docs/"],
                  ["test -f docs/README.md"], 1)],
            "cancel": ["scaffold"]}                      # the repository has its tooling
        # the store refuses out-of-scope writes, and the agent sees why
        assert call("docs_schema", path=f"{me}/oracle.json")["schema"] == "oracle"
        try:
            call("docs_write", path="tickets/intake/requirements.md", content="- REQ-9: sneaky",
                 base_version=call("docs_read", path="tickets/intake/requirements.md")["version"])
            raise AssertionError("a write outside the ticket's directory should have been refused")
        except RuntimeError as e:
            assert "G-W1" in str(e), e
        tickets = write(f"{me}/tickets.json", json.dumps(plan))
        print(call("report", status="DONE", summary="plan written",
                   attestations=[spec, oracle, env, tickets] + deltas,
                   outputs={"spec": spec, "specs": deltas, "oracle": oracle, "environment": env,
                            "tickets": tickets}))
    elif role == "critic":
        design = call("ticket_status", ticket="design")      # what it judges: every output
        plan = [x for v in design["outputs"].values() for x in (v if isinstance(v, list) else [v])]
        if design["attempt"] == 1:
            data = {"verdict": "REWORK", "findings": [{
                "severity": "WARNING", "confidence": "HIGH", "title": "spec too thin",
                "fix": "expand spec", "attestations": [plan[0]]}]}
        else:
            data = {"verdict": "PASS", "findings": [], "checked": ["traceability"]}
        doc = write(f"{me}/critique.md", f"critique {data['verdict']}")
        print(call("report", status="DONE", summary="critique", attestations=plan + [doc],
                   data=data, outputs={"critique": doc}))
    elif role == "oracle":
        wfile("tests/acceptance/test_greet.sh",
              "grep -q hello src/greet.txt && grep -q goodbye src/bye.txt\n")
        sha = commit_push("oracle: acceptance tests", a["git"]["branch"])
        doc = write(f"{me}/tests.md", "AC-1, AC-2 -> tests/acceptance/test_greet.sh")
        print(call("report", status="DONE", summary="oracle",
                   attestations=[f"git:{REPO}@{sha}", doc], outputs={"tests": doc}))
    elif role == "implementer":
        tid = a["ticket_id"]
        if tid == "T1":
            wfile("src/greet.txt", "hello\n" if attempt == 1 else "hello\n\n")
        elif tid == "T2":
            wfile("src/bye.txt", "goodbye\n" if attempt > 1 else "farewell\n")
        else:
            wfile("docs/README.md", "# Docs\nhello and goodbye\n")
            if a["attempts_left"] == 3 and attempt == 1:
                wfile("src/greet.txt", "hello world\n")   # out of scope -> G-B3
        sha = commit_push(f"{tid}: implement", a["git"]["branch"])
        cmds = a["work"]["verification"]
        ev = write(f"{me}/evidence.md", "\n".join(f"- `{c}` -> exit 0" for c in cmds))
        res = call("report", status="DONE", summary=f"{tid} done",
                   attestations=[f"git:{REPO}@{sha}", ev], outputs={"evidence": ev})
        print(res)
        if not res["accepted"]:
            assert any(v.startswith("G-B3") for v in res["violations"]), res
            git("checkout", "HEAD~1", "--", "src/greet.txt")
            git("commit", "-q", "--amend", "--no-edit", "-a")
            git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{a['git']['branch']}")
            sha = git("rev-parse", "HEAD")
            ev = write(f"{me}/evidence.md",
                       "\n".join(f"- `{c}` -> exit 0" for c in cmds) + "\n(fixed scope)")
            print(call("report", status="DONE", summary=f"{tid} fixed scope",
                       attestations=[f"git:{REPO}@{sha}", ev], outputs={"evidence": ev}))
    elif role in ("reviewer", "security", "architecture"):
        tid = a["ticket_id"].split(":", 1)[1]                # the build it judges
        cand = a["judged_sha"]
        catt = f"git:{REPO}@{cand}"
        if tid == "T1" and role == "reviewer" and owner_attempt(a) == 1:
            data = {"verdict": "FAIL", "checked": ["diff"], "findings": [{
                "severity": "BLOCKER", "confidence": "HIGH", "dimension": "correctness",
                "title": "missing newline handling", "observation": "x", "impact": "y",
                "fix": "rewrite", "attestations": [catt]}]}
        else:
            data = {"verdict": "PASS", "checked": ["diff", "tests"], "findings": []}
        doc = write(f"{me}/review.md", f"{data['verdict']}")
        print(call("report", status="DONE", summary="reviewed", attestations=[catt, doc],
                   data=data, outputs={"review": doc}))
    elif role == "baseline":
        head = a["head_sha"]
        orc = json.loads(call("docs_read", path=f"{PLAN}/oracle.json")["content"])
        results = [{"command": c, "exit_code": subprocess.run(["sh", "-c", c]).returncode}
                   for c in orc["acceptance_commands"] + orc["integration_commands"]]
        doc = write(f"{me}/report.md", f"baseline at {head}: {results}")
        print(call("report", status="DONE", summary="baseline", attestations=[f"git:{REPO}@{head}", doc],
                   data={"results": results}, outputs={"report": doc}))
    elif role == "describer":
        head = a["head_sha"]
        files = git("ls-files", "src", "docs").split()          # what the builds wrote
        pr = write(f"{me}/pr.md", "# Say hello and goodbye\n\n## Motivation\n\n"
                   "REQ-1 and REQ-2: greet and say goodbye.\n\n## Changes\n\n"
                   + "".join(f"- `{f}`\n" for f in files) +
                   "\n## Verification\n\n- the protected acceptance tests, at the head\n")
        print(call("report", status="DONE", summary="PR described",
                   attestations=[f"git:{REPO}@{head}", pr], outputs={"pr": pr}))
    elif role == "integration":
        head = a["judged_sha"]                           # a judge: delivery's head
        orc = json.loads(call("docs_read", path=f"{PLAN}/oracle.json")["content"])
        results = []
        cmds = orc["acceptance_commands"] + orc["integration_commands"]
        for c in dict.fromkeys(cmds):
            results.append({"command": c, "exit_code": subprocess.run(["sh", "-c", c]).returncode})
        ok = all(r["exit_code"] == 0 for r in results)
        doc = write(f"{me}/report.md", f"results: {results}")
        print(call("report", status="DONE", summary="integration", attestations=[f"git:{REPO}@{head}", doc],
                   data={"verdict": "PASS" if ok else "FAIL", "results": results, "findings": []},
                   outputs={"report": doc}))
    else:
        call("report", status="BLOCKED", summary="unknown role", attestations=[],
             blocked_reason="fake agent has no script", missing_decision=f"how to act as {role}")


def end_task():
    """report(...) recorded the results; the task is ended with a2a_reply (the state the
    accepted report supports)."""
    if ACCEPTED:
        state = "input-required" if ACCEPTED[-1] == "BLOCKED" else "completed"
        out = call("a2a_reply", task_id=os.environ["LEGION_RUN_ID"], text="done", state=state)
        assert out["ok"], out
        assert out["inbox"] == [], out                  # what is left: nothing (its task ended)


if __name__ == "__main__":
    try:
        main()
        end_task()
    except Exception as e:
        print("fake agent error:", e, file=sys.stderr)
        raise
