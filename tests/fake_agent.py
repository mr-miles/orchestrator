"""Scripted stand-in for an LLM agent, used by the end-to-end test.

Talks to the controller exactly like a real agent: MCP JSON-RPC over HTTP with the run token,
git clone/push against the controller remote. Deliberately makes mistakes in places to exercise
the guards and rework loops:

* critic: REWORK on cycle 1, PASS on cycle 2
* T1 reviewer: FAIL on cycle 1 (HIGH finding), PASS afterwards
* T2 implementer: wrong content on cycle 1 -> deterministic verification fails -> rework
* T3 implementer: first report touches an out-of-scope file -> G-B3 rejection -> fixes + re-reports
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
    git("commit", "-q", "-m", msg)
    git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}")
    return git("rev-parse", "HEAD")


def main():
    a = call("get_assignment")
    role, cycle = a["role"], a["cycle"]
    call("progress", message=f"{role} starting cycle {cycle}", percent=5)
    if role == "intake":
        req = write("intake/requirements.md",
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
        risk = write("intake/risk.json", json.dumps({"signals": signals}))
        print(call("report", status="DONE", summary="requirements captured", attestations=[req, risk]))
    elif role == "planner":
        spec = write("plan/spec.md", f"# Spec (cycle {cycle})\n\nWrite greet/bye files and docs.\n")
        deltas = [write(f"plan/specs/{r}.md", f"# {r} delta spec (cycle {cycle})\n\n## Current behaviour\n"
                        f"missing\n\n## Change\nadd it\n\n## Acceptance criteria\n- works\n")
                  for r in ("REQ-1", "REQ-2")]
        oracle = write("plan/oracle.json", json.dumps({
            "acceptance_tests": ["tests/acceptance/test_greet.sh"],
            "acceptance_commands": ["sh tests/acceptance/test_greet.sh"],
            "integration_commands": ["test -f docs/README.md"],
            "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "hello"},
                         {"id": "AC-2", "requirement": "REQ-2", "text": "goodbye"}],
            "surfaces": [{"id": "files", "description": "the repository files are the interface",
                          "commands": ["test -f README.md"], "pins": ["README.md"]}]}))
        tasks = [
            {"id": "T1", "title": "greet", "soul": "backend-engineer", "requirements": ["REQ-1"],
             "depends_on": [], "files_modified": ["src/greet.txt"], "instructions": "write hello",
             "acceptance": ["contains hello"], "verification": ["grep -q hello src/greet.txt"]},
            {"id": "T2", "title": "bye", "soul": "senior-developer", "requirements": ["REQ-2"],
             "depends_on": [], "files_modified": ["src/bye.txt"], "instructions": "write goodbye",
             "acceptance": ["contains goodbye"], "verification": ["grep -q goodbye src/bye.txt"]},
            {"id": "T3", "title": "docs", "soul": "senior-developer", "requirements": ["REQ-1"],
             "depends_on": ["T1", "T2"], "files_modified": ["docs/"], "instructions": "document",
             "acceptance": ["docs exist"], "verification": ["test -f docs/README.md"]},
        ]
        # the store refuses invalid / out-of-scope writes, and the agent sees why
        assert call("docs_schema", path="plan/taskgraph.json")["schema"] == "taskgraph"
        for path, body, gid in (("plan/taskgraph.json", json.dumps({"tasks": [{"id": "X"}]}), "G-W3"),
                                ("intake/requirements.md", "- REQ-9: sneaky", "G-W1")):
            try:
                call("docs_write", path=path, content=body,
                     base_version=(call("docs_read", path=path)["version"]
                                   if path.startswith("intake") else None))
                raise AssertionError(f"write to {path} should have been refused")
            except RuntimeError as e:
                assert gid in str(e), e
        tg = write("plan/taskgraph.json", json.dumps({"tasks": tasks}))
        print(call("report", status="DONE", summary="plan written",
                   attestations=[spec, oracle, tg] + deltas))
    elif role == "critic":
        plan = [call("docs_read", path=p)["attestation"] for p in
                ("plan/spec.md", "plan/oracle.json", "plan/taskgraph.json")]
        head = a["base_sha"]
        if cycle == 1:
            data = {"verdict": "REWORK", "findings": [{
                "severity": "WARNING", "confidence": "HIGH", "title": "spec too thin",
                "fix": "expand spec", "attestations": [plan[0]]}]}
        else:
            data = {"verdict": "PASS", "findings": [], "checked": ["traceability"]}
        doc = write(f"critique/cycle-{cycle}.md", f"critique {data['verdict']}")
        print(call("report", status="DONE", summary="critique", attestations=plan + [doc], data=data))
    elif role == "oracle":
        wfile("tests/acceptance/test_greet.sh",
              "grep -q hello src/greet.txt && grep -q goodbye src/bye.txt\n")
        sha = commit_push("oracle: acceptance tests", a["git"]["branch"])
        doc = write("oracle/tests.md", "AC-1, AC-2 -> tests/acceptance/test_greet.sh")
        print(call("report", status="DONE", summary="oracle", attestations=[f"git:{REPO}@{sha}", doc]))
    elif role == "implementer":
        tid = a["task"]["id"]
        if tid == "T1":
            wfile("src/greet.txt", "hello\n" if cycle == 1 else "hello\n\n")
        elif tid == "T2":
            wfile("src/bye.txt", "goodbye\n" if cycle > 1 else "farewell\n")
        else:
            wfile("docs/README.md", "# Docs\nhello and goodbye\n")
            if a["attempts_left"] == 3 and cycle == 1:
                wfile("src/greet.txt", "hello world\n")   # out of scope -> G-B3
        sha = commit_push(f"{tid}: implement", a["git"]["branch"])
        cmds = a["task"]["verification"]
        ev = write(f"tasks/{tid}/evidence/cycle-{cycle}.md",
                   "\n".join(f"- `{c}` -> exit 0" for c in cmds))
        res = call("report", status="DONE", summary=f"{tid} done", attestations=[f"git:{REPO}@{sha}", ev])
        print(res)
        if not res["accepted"]:
            assert any(v.startswith("G-B3") for v in res["violations"]), res
            git("checkout", "HEAD~1", "--", "src/greet.txt")
            git("commit", "-q", "--amend", "--no-edit", "-a")
            git("push", "-q", "--force", "origin", f"HEAD:refs/heads/{a['git']['branch']}")
            sha = git("rev-parse", "HEAD")
            ev = write(f"tasks/{tid}/evidence/cycle-{cycle}.md",
                       "\n".join(f"- `{c}` -> exit 0" for c in cmds) + "\n(fixed scope)")
            print(call("report", status="DONE", summary=f"{tid} fixed scope",
                       attestations=[f"git:{REPO}@{sha}", ev]))
    elif role in ("reviewer", "security", "architecture"):
        tid = a["task"]["id"]
        cand = a["candidate_sha"]
        catt = f"git:{REPO}@{cand}"
        if tid == "T1" and cycle == 1:
            data = {"verdict": "FAIL", "checked": ["diff"], "findings": [{
                "severity": "BLOCKER", "confidence": "HIGH", "dimension": "correctness",
                "title": "missing newline handling", "observation": "x", "impact": "y",
                "fix": "rewrite", "attestations": [catt]}]}
        else:
            data = {"verdict": "PASS", "checked": ["diff", "tests"], "findings": []}
        doc = write(f"tasks/{tid}/reviews/{role}-cycle-{cycle}.md", f"{data['verdict']}")
        print(call("report", status="DONE", summary="reviewed", attestations=[catt, doc], data=data))
    elif role == "baseline":
        head = a["head_sha"]
        orc = json.loads(call("docs_read", path="plan/oracle.json")["content"])
        results = [{"command": c, "exit_code": subprocess.run(["sh", "-c", c]).returncode}
                   for c in orc["acceptance_commands"] + orc["integration_commands"]]
        doc = write("baseline/report.md", f"baseline at {head}: {results}")
        print(call("report", status="DONE", summary="baseline", attestations=[f"git:{REPO}@{head}", doc],
                   data={"results": results}))
    elif role == "release":
        p = write("release/plan.md", "# Release\n\n## Health criteria\n- ok\n\n## Rollback criteria\n- not ok\n")
        r = write("release/rollback.md", f"Revert to {a['base_sha']}\n")
        print(call("report", status="DONE", summary="release plan", attestations=[p, r]))
    elif role == "integration":
        head = a["head_sha"]
        orc = json.loads(call("docs_read", path="plan/oracle.json")["content"])
        results = []
        cmds = orc["acceptance_commands"] + orc["integration_commands"] + \
            [c for sf in orc.get("surfaces", []) for c in sf["commands"]]
        for c in dict.fromkeys(cmds):
            results.append({"command": c, "exit_code": subprocess.run(["sh", "-c", c]).returncode})
        ok = all(r["exit_code"] == 0 for r in results)
        doc = write("integration/report.md", f"results: {results}")
        print(call("report", status="DONE", summary="integration", attestations=[f"git:{REPO}@{head}", doc],
                   data={"verdict": "PASS" if ok else "FAIL", "results": results, "findings": []}))
    else:
        call("report", status="BLOCKED", summary="unknown role", attestations=[],
             blocked_reason="fake agent has no script", missing_decision=f"how to act as {role}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("fake agent error:", e, file=sys.stderr)
        raise
