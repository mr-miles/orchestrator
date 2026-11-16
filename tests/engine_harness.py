"""Deterministic in-process harness for the workflow state machine.

A real Controller, document store and git remote, but a fake sandbox runner whose agents never
run on their own: the test plays each agent by calling the engine's agent API (``agent_write``,
``handle_report``) exactly as the MCP server would, and decides when the agent process exits.
``Flow.settle()`` ticks the scheduler until every background task (verification, merge, ship,
sandbox deletion) has finished, so each step ends in a stable, assertable state.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from legion_orch.config import load_config
from legion_orch.sandbox import BuildError, EnvironmentBuilder, Runner, SandboxError
from legion_orch.server.mcp import MCP_TOOLS
from legion_orch.workflow.engine import ChangeEngine, Controller
from legion_orch.workflow.model import LIVE_RUN, Run
from legion_orch.workflow.risk import SIGNALS

ROOT = Path(__file__).resolve().parent.parent
REPO = "app"


class FakeProc:
    """An agent process that exits when the test says so (or when killed)."""

    def __init__(self):
        self.returncode: int | None = None
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return self.returncode

    def exit(self, rc: int = 0) -> None:
        if self.returncode is None:
            self.returncode = rc
            self._done.set()

    def kill(self) -> None:
        self.exit(-9)


class FakeRunner(Runner):
    """Records sandbox operations; verification commands return scripted results."""

    def __init__(self):
        self.created: list[str] = []
        self.specs: dict[str, object] = {}               # sandbox name -> SandboxSpec
        self.uploads: list[str] = []
        self.deleted: list[str] = []
        self.started: list[str] = []
        self.commands: dict[str, tuple[int, str]] = {}   # verification command -> (rc, output)
        self.setup_rc = 0
        self.fail_create: str | None = None              # role whose sandbox creation fails
        self.fail_delete = False
        self.clash = 0             # the next N creates find the name taken on the gateway

    async def create(self, spec):
        if self.clash:
            self.clash -= 1
            raise SandboxError(f"sandbox '{spec.name}' already exists")
        if self.fail_create == spec.role:
            raise SandboxError("openshell sandbox create failed (quota)")
        self.created.append(spec.name)
        self.specs[spec.name] = spec
        return spec.name

    async def upload(self, name, payload_dir):
        self.uploads.append(name)

    async def exec(self, name, argv, *, workdir=None, timeout=600):
        if argv[1].endswith("verify-setup.sh"):
            return self.setup_rc, "checked out\n"
        return self.commands.get(argv[-1], (0, "ok\n"))

    async def start_agent(self, name, log_file):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(f"agent started in {name}\nworking...\n")
        self.started.append(name)
        return FakeProc()

    async def delete(self, name):
        if self.fail_delete:
            raise SandboxError("openshell sandbox delete failed")
        self.deleted.append(name)


class FakeBuilder(EnvironmentBuilder):
    """Records the images and network providers provisioning asks for; ``fail`` makes the
    next build fail. Images are "built" once, then found (content-addressed reuse)."""

    def __init__(self):
        self.built: list[str] = []                 # image tags built
        self.recipes: dict[str, object] = {}       # tag -> ImageRecipe
        self.providers: dict[str, object] = {}     # provider id -> NetworkProvider
        self.fail: str | None = None

    async def ensure_image(self, recipe):
        if self.fail:
            msg, self.fail = self.fail, None
            raise BuildError(msg)
        self.recipes[recipe.tag] = recipe
        if recipe.tag in self.built:
            return recipe.tag, f"{recipe.tag} exists; reused"
        self.built.append(recipe.tag)
        return recipe.tag, f"built {recipe.tag}"

    async def ensure_provider(self, provider):
        self.providers[provider.id] = provider
        return f"provider {provider.id}"


def make_source_repo(path: Path) -> None:
    path.mkdir()
    git = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "src").mkdir()
    (path / "src" / "greet.txt").write_text("hi\n")
    (path / "README.md").write_text("demo\n")
    git("add", "-A")
    git("commit", "-q", "-m", "init")


def make_config(tmp_path: Path, *, blank_repo: bool = False, environment: dict | None = None,
                **limits):
    lim = {"max_parallel": 4, "review_max_cycles": 3, "critique_max_cycles": 2,
           "run_timeout_s": 120} | limits
    cfg_path = tmp_path / "orchestrator.yaml"
    cfg_path.write_text(textwrap.dedent(f"""
        project: engine-test
        repo: {{name: {REPO}{'' if blank_repo else f", path: {tmp_path / 'src-repo'}"}, base_branch: main}}
        decision_owners: [miles, ana]
        state_dir: state
        assets_dir: {ROOT / 'assets'}
        workflow_doc: {ROOT / 'WORKFLOW.md'}
        limits: {json.dumps(lim)}
        sandbox:
          runner: local
          templates_dir: {ROOT / 'policies'}
          agent_command: "{sys.executable} -c pass"
    """))
    if environment is not None:
        env = {"library_dir": str(ROOT / "environments")} | environment
        with cfg_path.open("a") as f:
            f.write(f"environment: {json.dumps(env)}\n")
    return load_config(cfg_path)


def signals(**scores: int) -> dict:
    """Risk signals: every signal 0 unless given. R1 by default (total 3, no floor)."""
    scores = {"scope_breadth": 1, "novelty_uncertainty": 1, "verification_quality": 1} | scores
    return {"signals": [{"name": n, "score": scores.get(n, 0), "rationale": "assessed"}
                        for n in SIGNALS]}


TASKS = [
    {"id": "T1", "title": "greet", "soul": "backend-engineer", "requirements": ["REQ-1"],
     "depends_on": [], "files_modified": ["src/greet.txt"], "instructions": "write hello",
     "acceptance": ["contains hello"], "verification": ["grep -q hello src/greet.txt"]},
]
ORACLE = {
    "acceptance_tests": ["tests/acceptance/test_greet.sh"],
    "acceptance_commands": ["sh tests/acceptance/test_greet.sh"],
    "integration_commands": [],
    "criteria": [{"id": "AC-1", "requirement": "REQ-1", "text": "hello"}],
    "surfaces": [{"id": "files", "description": "the files", "commands": ["test -f README.md"],
                  "pins": ["README.md"]}],
}


ENVIRONMENT = {
    "summary": "python project tested with uv",
    "profiles": {
        "dev": {"tools": ["python@3.12", "uv", "ruff"],          # uv brings pypi
                "resources": {"memory_mib": 4096, "vcpus": 2}},
        "check": {"tools": ["python@3.12", "uv", "ruff"], "offline": True},
    },
    "roles": {"implementer": "dev", "oracle": "dev", "verifier": "check", "reviewer": "check"},
    "rationale": "pyproject pins 3.12; implementers add dependencies from pypi",
}


class Flow:
    """One change driven step by step. ``await Flow.create(tmp_path)`` to start."""

    def __init__(self, tmp_path: Path, *, blank_repo: bool = False, **limits):
        self.tmp = tmp_path
        if not blank_repo and not (tmp_path / "src-repo").exists():
            make_source_repo(tmp_path / "src-repo")
        self.cfg = make_config(tmp_path, blank_repo=blank_repo, **limits)
        self.runner = FakeRunner()
        self.builder = FakeBuilder()
        self.ctl = Controller(self.cfg, runner=self.runner, builder=self.builder)
        self.ctl.set_mcp_tools(list(MCP_TOOLS))
        self.ce: ChangeEngine = None   # type: ignore[assignment]

    @classmethod
    async def create(cls, tmp_path: Path, goal: str = "Say hello", *, blank_repo: bool = False,
                     **limits) -> Flow:
        f = cls(tmp_path, blank_repo=blank_repo, **limits)
        await f.ctl.git.ensure()
        f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
        f.ce = await f.ctl.start_change(goal)
        await f.settle()
        return f

    # ------------------------------------------------------------------ driving
    async def settle(self, timeout: float = 20) -> None:
        """Tick until nothing changes and the only pending tasks are agents waiting to exit."""
        last = None
        for _ in range(int(timeout / 0.02)):
            await self.ce.tick()
            await asyncio.sleep(0.02)
            pending = [t for t in self.ctl._tasks if not t.done()]
            waiting = [p for p in self.ctl.svc.procs.values() if p.returncode is None]
            state = (self._fingerprint(), len(pending))
            if len(pending) <= len(waiting) and state == last:
                return
            last = state
        raise AssertionError(f"engine did not settle; nodes={self.status()}")

    def _fingerprint(self):
        ch = self.ce.change
        return (ch.status, tuple((k, n.status, n.cycle) for k, n in self.ce.nodes.items()),
                tuple((k, r.status, r.sandbox_deleted) for k, r in self.ce.runs.items()))

    def status(self, node: str | None = None):
        if node:
            return self.ce.nodes[node].status
        return {k: n.status for k, n in self.ce.nodes.items()}

    def run(self, node: str, role: str | None = None) -> Run:
        """The live run of ``node`` (for ``role``)."""
        live = [r for r in self.ce.runs.values() if r.node_id == node and r.status in LIVE_RUN
                and (role is None or r.role == role)]
        assert len(live) == 1, f"expected one live run of {node}/{role}; got {live}"
        return live[0]

    def runs(self, node: str, role: str | None = None) -> list[Run]:
        return [r for r in self.ce.runs.values() if r.node_id == node
                and (role is None or r.role == role)]

    async def exit_agent(self, run: Run, rc: int = 0) -> None:
        self.ctl.svc.procs[run.id].exit(rc)
        await self.settle()

    async def human(self, node: str, action: str, approver: str = "miles", **payload):
        res = await self.ce.human_action(node, action, approver, payload)
        await self.settle()
        return res

    async def change_action(self, action: str, approver: str = "miles", **payload):
        res = await self.ce.change_action(action, approver, payload)
        await self.settle()
        return res

    # ------------------------------------------------------------------ playing an agent
    async def write(self, run: Run, rel: str, content: str | dict) -> str:
        if not isinstance(content, str):
            content = json.dumps(content)
        v = await self.ce.agent_write(run, rel, content, self.ce.latest(rel))
        return v.attestation

    async def report(self, run: Run, atts=(), data=None, status="DONE", *, exit_agent=True,
                     blocked_reason=None, missing_decision=None, summary="done"):
        out = await self.ce.handle_report(run.id, status, summary, list(atts), data,
                                          blocked_reason, missing_decision)
        if out.accepted and exit_agent:
            self.ctl.svc.procs[run.id].exit(0)
        await self.settle()
        return out

    def commit(self, run: Run, files: dict[str, str], *, push: bool = True,
               base: str | None = None) -> str:
        """Commit ``files`` on top of the run's base and push to its branch; return the sha."""
        wt = self.tmp / f"wt-{run.id}-{len(os.listdir(self.tmp))}"
        g = lambda *a, **kw: subprocess.run(["git", *a], cwd=wt, check=True, capture_output=True,
                                            text=True, **kw).stdout.strip()
        subprocess.run(["git", "clone", "-q", str(self.ctl.git.repo), str(wt)], check=True,
                       capture_output=True)
        g("checkout", "-q", base or run.base_sha)
        g("config", "user.email", "agent@sandbox")
        g("config", "user.name", "agent")
        for path, text in files.items():
            (wt / path).parent.mkdir(parents=True, exist_ok=True)
            (wt / path).write_text(text)
        g("add", "-A")
        g("commit", "-q", "-m", f"{run.role} {run.id}")
        sha = g("rev-parse", "HEAD")
        if push:
            ref = f"refs/heads/{run.branch}"
            g("push", "-q", "--force", "origin", f"HEAD:{ref}",
              env=os.environ | {"LEGION_ALLOWED_REF": ref})
        return sha

    # ------------------------------------------------------------------ role scripts
    async def intake(self, risk: dict | None = None, reqs: str = "- REQ-1: greet.txt says hello\n"):
        r = self.run("intake")
        atts = [await self.write(r, "intake/requirements.md", f"# Requirements\n\n{reqs}"),
                await self.write(r, "intake/risk.json", risk or signals())]
        return await self.report(r, atts)

    async def plan(self, tasks=None, oracle=None, environment: dict | None = None,
                   keep_environment: bool = False):
        """With the environment stage the plan includes ``plan/environment.json``
        (``environment``, default ENVIRONMENT), or attests the existing one (``keep_environment``)."""
        r = self.run("plan")
        atts = [await self.write(r, "plan/spec.md", f"# Spec (cycle {r.cycle})\n"),
                await self.write(r, "plan/oracle.json", oracle or ORACLE),
                await self.write(r, "plan/taskgraph.json", {"tasks": tasks or TASKS})]
        for req in self.ce.change.requirements:
            spec = (f"# {req} delta (cycle {r.cycle})\n\n## Current behaviour\nmissing\n\n"
                    "## Change\nadd it\n\n## Acceptance criteria\n- works\n")
            atts.append(await self.write(r, f"plan/specs/{req}.md", spec))
        if "H-E1" in self.ce.nodes:
            atts.append(self.ce.latest_att("plan/environment.json") if keep_environment else
                        await self.write(r, "plan/environment.json", environment or ENVIRONMENT))
        return await self.report(r, atts)

    async def critique(self, verdict: str = "PASS"):
        r = self.run("critique")
        docs = ("plan/spec.md", "plan/oracle.json", "plan/taskgraph.json") + (
            ("plan/environment.json",) if "H-E1" in self.ce.nodes else ())
        plan = [self.ce.latest_att(p) for p in docs]
        findings = [] if verdict == "PASS" else [{
            "severity": "WARNING", "confidence": "HIGH", "title": "spec too thin",
            "fix": "expand the spec", "attestations": [plan[0]]}]
        doc = await self.write(r, f"critique/cycle-{r.cycle}.md", f"critique {verdict}")
        return await self.report(r, plan + [doc], {"verdict": verdict, "findings": findings,
                                                   "checked": ["traceability"]})

    async def implement(self, task: str = "T1", files: dict | None = None, evidence: str | None = None,
                        **kw):
        node = f"build:{task}"
        r = self.run(node, "implementer")
        t = self.ce.nodes[node].task
        sha = self.commit(r, files or {p: f"hello from {task} cycle {r.cycle}\n"
                                       for p in t.files_modified})
        ev = await self.write(r, f"tasks/{task}/evidence/cycle-{r.cycle}.md",
                              evidence if evidence is not None
                              else "\n".join(f"- `{c}` -> exit 0" for c in t.verification))
        return await self.report(r, [f"git:{REPO}@{sha}", ev], **kw)

    async def review(self, task: str = "T1", verdict: str = "PASS", role: str = "reviewer",
                     title: str = "missing newline handling"):
        node = f"build:{task}"
        r = self.run(node, role)
        cand = f"git:{REPO}@{r.judged_sha}"
        findings = [] if verdict == "PASS" else [{
            "severity": "BLOCKER", "confidence": "HIGH", "dimension": "correctness",
            "title": title, "observation": "x", "impact": "y", "fix": "rewrite it",
            "attestations": [cand]}]
        doc = await self.write(r, f"tasks/{task}/reviews/{role}-cycle-{r.cycle}.md", verdict)
        return await self.report(r, [cand, doc], {"verdict": verdict, "findings": findings,
                                                  "checked": ["diff"]})

    async def oracle(self, files: dict | None = None):
        r = self.run("oracle")
        sha = self.commit(r, files or {"tests/acceptance/test_greet.sh": "grep -q hello src/greet.txt\n"})
        doc = await self.write(r, "oracle/tests.md", "AC-1 -> tests/acceptance/test_greet.sh")
        return await self.report(r, [f"git:{REPO}@{sha}", doc])

    async def integration(self, verdict: str = "PASS"):
        r = self.run("integration")
        cmds = self.ce.oracle_commands()
        doc = await self.write(r, "integration/report.md", "results")
        return await self.report(r, [f"git:{REPO}@{r.judged_sha}", doc], {
            "verdict": verdict, "findings": [],
            "results": [{"command": c, "exit_code": 0} for c in cmds]})

    # ------------------------------------------------------------------ whole stages
    async def to_h2(self, tier: str | None = None, **plan_kw) -> None:
        """Intake, H-1 (optionally at ``tier``), plan and a passing critique (R2: and oracle)."""
        await self.intake()
        await self.human("H-1", "accept", **({"tier": tier} if tier else {}))
        await self.plan(**plan_kw)
        await self.critique("PASS")
        if "H-E1" in self.ce.nodes:
            await self.human("H-E1", "approve")
            assert self.status("provision") == "ACCEPTED", self.status()
        if "oracle" in self.ce.nodes:
            await self.oracle()

    async def to_build(self, tier: str | None = None, **plan_kw) -> None:
        await self.to_h2(tier, **plan_kw)
        await self.human("H-2", "approve")
        assert self.status("H-2") == "ACCEPTED", self.status()


def scenario(**limits):
    """Run an async test ``fn(flow)`` against a fresh change (``limits`` override config)."""
    def wrap(fn):
        @functools.wraps(fn)
        def test(tmp_path):
            async def go():
                await fn(await Flow.create(tmp_path, **limits))
            asyncio.run(go())
        test.__signature__ = inspect.Signature(
            [inspect.Parameter("tmp_path", inspect.Parameter.POSITIONAL_OR_KEYWORD)])
        return test
    return wrap
