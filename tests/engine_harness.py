"""Deterministic in-process harness for the workflow: a change's tickets.

A real Controller, document store and git remote, but a fake sandbox runner whose agents never
run on their own: the test plays each agent by calling the engine's agent API (``agent_write``,
``handle_report``) exactly as the MCP server would, and decides when the agent process exits.
``Flow.settle()`` waits until every background task (verification, merges, provisioning,
publishing, sandbox deletion) has finished or legitimately waits – on an agent's process, a
decision owner, or a slot – so each step ends in a stable, assertable state.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from legion_orch.config import load_config
from legion_orch.sandbox import BuildError, EnvironmentBuilder, Runner, SandboxError
from legion_orch.workflow.engine import ChangeEngine, Controller
from legion_orch.workflow.model import LIVE_RUN, Run
from legion_orch.workflow.risk import SIGNALS
from legion_orch.workflow.tickets import Ticket

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


def _run_env(payload_dir) -> str:
    f = Path(payload_dir) / "run.env"
    return f.read_text() if f.exists() else ""


class FakeRunner(Runner):
    """Records sandbox operations; verification commands return scripted results."""

    def __init__(self):
        self.created: list[str] = []
        self.specs: dict[str, object] = {}               # sandbox name -> SandboxSpec
        self.policies: dict[str, dict] = {}              # sandbox name -> its policy document
        self.uploads: list[str] = []
        self.deleted: list[str] = []
        self.started: list[str] = []                     # agents started (start_agent)
        self.stopped: list[str] = []                     # kept sandboxes stopped between runs
        self.restarted: list[str] = []                   # stopped sandboxes started again
        self.run_envs: list[str] = []                    # run.env of every payload, in order
        self.mcp_configs: list[str] = []                 # mcp.json of every agent payload
        self.fail_stop = False
        self.payloads: dict[str, dict[str, str]] = {}    # sandbox -> payload files at create
        self.uploaded: dict[str, dict[str, str]] = {}    # sandbox -> its latest uploaded payload
        self._reflecting: dict[str, str] = {}             # sandbox -> reflection prompt
        self.reflected: list[tuple[str, str]] = []        # (sandbox, prompt) of each reflection
        self.reflection_skills: dict[str, list[str]] = {}  # sandbox -> skills its reflection got
        self.reflection_text = "## Time sinks\nThe tests took long.\n"
        self.reflection_session: str | None = None     # set: the resume does not take
        self.commands: dict[str, tuple[int, str]] = {}   # verification command -> (rc, output)
        self.execs: list[tuple[str, list[str]]] = []      # (sandbox, argv) of every exec
        self.setup_rc = 0
        self.fail_create: str | None = None              # role whose sandbox creation fails
        self.fail_delete = False
        self.clash = 0             # the next N creates find the name taken on the gateway
        self.phase: dict[str, str] = {}                  # the gateway's sandboxes: name -> phase
        self.list_complete = True                        # False: list() returns a partial page

    async def create(self, spec):
        if self.clash:
            self.clash -= 1
            raise SandboxError(f"sandbox '{spec.name}' already exists")
        if self.fail_create == spec.role:
            raise SandboxError("openshell sandbox create failed (quota)")
        self.created.append(spec.name)
        self.run_envs.append(_run_env(spec.payload_dir))
        if (Path(spec.payload_dir) / "mcp.json").exists():
            self.mcp_configs.append((Path(spec.payload_dir) / "mcp.json").read_text())
        self.payloads[spec.name] = {str(f.relative_to(spec.payload_dir)): f.read_text()
                                    for f in Path(spec.payload_dir).rglob("*") if f.is_file()}
        self.specs[spec.name] = spec
        self.policies[spec.name] = yaml.safe_load(Path(spec.policy_file).read_text())
        self.phase[spec.name] = "Ready"
        return spec.name

    async def upload(self, name, payload_dir):
        self.uploads.append(name)
        self.run_envs.append(_run_env(payload_dir))
        self.uploaded[name] = {str(f.relative_to(payload_dir)): f.read_text()
                               for f in Path(payload_dir).rglob("*") if f.is_file()}
        if (Path(payload_dir) / "mcp.json").exists():
            self.mcp_configs.append((Path(payload_dir) / "mcp.json").read_text())
        if "LEGION_REQUIRE_SESSION" in self.run_envs[-1]:          # a reflection turn
            self._reflecting[name] = (Path(payload_dir) / "prompt.md").read_text()
            self.reflection_skills[name] = sorted(
                str(f.relative_to(payload_dir)) for f in Path(payload_dir).rglob("skills/*/SKILL.md"))

    async def stop(self, name):
        if self.fail_stop:
            raise SandboxError("openshell sandbox stop is not supported")
        self.stopped.append(name)
        if name in self.phase:
            self.phase[name] = "Stopped"

    async def start(self, name):
        self.restarted.append(name)
        if name in self.phase:
            self.phase[name] = "Ready"

    async def exec(self, name, argv, *, workdir=None, timeout=600):
        self.execs.append((name, list(argv)))
        if argv[1].endswith(("clone.sh", "checkout.sh", "branch.sh")):
            return self.setup_rc, "checked out\n"
        return self.commands.get(argv[-1], (0, "ok\n"))

    async def start_agent(self, name, log_file):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        if name in self._reflecting:          # the resumed agent answers and exits
            self.reflected.append((name, self._reflecting.pop(name)))
            init = ({"type": "system", "subtype": "init", "session_id": self.reflection_session}
                    if self.reflection_session else None)
            log_file.write_text((json.dumps(init) + "\n" if init else "")
                                + json.dumps({"type": "result", "subtype": "success",
                                              "result": self.reflection_text}) + "\n")
            proc = FakeProc()
            proc.exit(0)
            return proc
        with open(log_file, "a") as log:                # appended, as the real runner does
            log.write(f"agent started in {name}\nworking...\n")
        self.started.append(name)
        return FakeProc()

    async def delete(self, name):
        if self.fail_delete:
            raise SandboxError("openshell sandbox delete failed")
        self.deleted.append(name)
        self.phase.pop(name, None)

    async def list(self, selector=None):
        """The gateway's listing: every sandbox created and not deleted, labelled as created,
        those matching ``selector``."""
        from legion_orch.sandbox import GatewaySandbox, Listing
        from legion_orch.sandbox.templates import label_value
        found = (GatewaySandbox(name=n, id=n, phase=ph, labels={k: label_value(v) for k, v in (
                     ("legion.change", s.change), ("legion.task", s.task), ("legion.role", s.role),
                     ("legion.run", s.run)) if v}, annotations={"legion.task": s.task})
                 for n, ph in self.phase.items() if (s := self.specs.get(n)) is not None)
        return Listing(tuple(sb for sb in found
                             if all(sb.labels.get(k) == v for k, v in (selector or {}).items())),
                       complete=self.list_complete)


class FakeBuilder(EnvironmentBuilder):
    """Records the images provisioning asks for; ``fail`` makes the
    next build fail. Images are "built" once, then found (content-addressed reuse)."""

    def __init__(self):
        self.built: list[str] = []                 # image tags built
        self.recipes: dict[str, object] = {}       # tag -> ImageRecipe
        self.fail: str | None = None
        self.removed: list[str] = []               # changes whose images were removed
        self.hold: asyncio.Event | None = None     # set: builds wait until it is set

    async def remove_change_images(self, change):
        self.removed.append(change)
        return f"removed the images of {change}"

    async def ensure_image(self, recipe):
        if self.hold is not None:
            await self.hold.wait()
        if self.fail:
            msg, self.fail = self.fail, None
            raise BuildError(msg)
        self.recipes[recipe.tag] = recipe
        if recipe.tag in self.built:
            return recipe.tag, f"{recipe.tag} exists; reused"
        self.built.append(recipe.tag)
        return recipe.tag, f"built {recipe.tag}"


def make_source_repo(path: Path, files: dict[str, str] | None = None) -> None:
    """The source repository a change starts from; ``files``: committed on top (a mise.toml)."""
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
    for rel, text in (files or {}).items():
        (path / rel).write_text(text)
    if files:
        git("add", "-A")
        git("commit", "-q", "-m", "files")


def make_config(tmp_path: Path, *, blank_repo: bool = False, environment: dict | None = None,
                **limits):
    lim = {"max_parallel": 4, "system_retries": 2, "system_retry_s": 0.01,
           "run_timeout_s": 120} | limits
    cfg_path = tmp_path / "orchestrator.yaml"
    source = "" if blank_repo else f", source: {tmp_path / 'src-repo'}"
    cfg_path.write_text(textwrap.dedent(f"""
        project: engine-test
        workspaces: [{{name: {REPO}{source}, base_branch: main}}]
        decision_owners: [miles, ana]
        state_dir: state
        assets_dir: {ROOT / 'assets'}
        workflow_doc: {ROOT / 'WORKFLOW.md'}
        limits: {json.dumps(lim)}
        sandbox:
          runner: local
          templates_dir: {ROOT / 'policies'}
          agent_bin: "true"
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
    """One change driven ticket by ticket. ``await Flow.create(tmp_path)`` to start.

    The test plays each agent through the engine's agent API (``agent_write``, ``handle_report``
    with the outputs its role names), and each decision owner through ``human_action``; the
    controller's own tickets (verification, can-merge, provisioning, publishing) run by
    themselves against the fake runner and the real hub."""

    def __init__(self, tmp_path: Path, *, blank_repo: bool = False,
                 repo_files: dict[str, str] | None = None, **limits):
        self.tmp = tmp_path
        if not blank_repo and not (tmp_path / "src-repo").exists():
            make_source_repo(tmp_path / "src-repo", repo_files)
        self.cfg = make_config(tmp_path, blank_repo=blank_repo, **limits)
        self.cfg.retrospective.reflect = False      # tests that want reflections turn them on
        self.cfg.resolver.enabled = False           # ... and the resolver on escalations
        self.cfg.retrospective.auto = False         # ... and automatic retrospectives
        self.runner = FakeRunner()
        self.builder = FakeBuilder()
        self.ctl = Controller(self.cfg, runner=self.runner, builder=self.builder)
        self.ctls = [self.ctl]         # every controller started (restarts too): stopped at the end
        self.ce: ChangeEngine = None   # type: ignore[assignment]

    async def stop(self) -> None:
        """Stop every controller this flow started, as a controller stops (in order)."""
        for ctl in self.ctls:
            await asyncio.wait_for(ctl.stop(), 10)

    @classmethod
    async def create(cls, tmp_path: Path, goal: str = "Say hello", *, blank_repo: bool = False,
                     **limits) -> Flow:
        f = cls(tmp_path, blank_repo=blank_repo, **limits)
        await f.git.ensure()
        f.ctl.svc.tmp.mkdir(parents=True, exist_ok=True)
        f.ce = await f.ctl.start_change(goal, REPO)
        await f.settle()
        return f

    # ------------------------------------------------------------------ driving
    async def restart(self, *, recover: bool = False) -> tuple[Controller, ChangeEngine]:
        """The controller started again on this one's state – its tables, documents and the
        gateway's sandboxes, as :meth:`Controller.start` loads them (without its loops);
        ``recover``: and what start() does next (live runs ended, tickets taken up again)."""
        ctl = Controller(self.cfg, runner=self.runner, builder=self.builder)
        self.ctls.append(ctl)
        ctl.load()
        await ctl.adopt_sandboxes()
        ce = ctl.changes[self.ce.cid]
        if recover:
            async with ctl.svc.lock:
                ce.recover()
        return ctl, ce

    async def settle(self, timeout: float = 20) -> None:
        """Wait until nothing changes and every pending task waits legitimately: on an agent
        process that has not exited, on a decision owner (a sign-off), for a slot, or on an
        A2A message's reply. Settled: the same quiet state twice, a few loop turns apart."""
        loop = asyncio.get_running_loop()
        deadline, last = loop.time() + timeout, None
        while loop.time() < deadline:
            for _ in range(self.TURNS):            # let ready callbacks and wake-ups run
                await asyncio.sleep(0)
            pending = [t for t in self.ctl._tasks if not t.done()]
            state = (self._fingerprint(), len(pending))
            quiet = len(pending) <= self.waiting()
            if quiet and state == last:
                return
            last = state
            if quiet:
                continue                           # look again after a few loop turns
            bumped = asyncio.ensure_future(self.ctl._bumped.wait())
            try:
                await asyncio.wait([*pending, bumped], timeout=self.POLL,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                bumped.cancel()
        raise AssertionError(f"engine did not settle; tickets={self.status()}")

    POLL = 0.02     # the longest it waits for a sign of progress before looking again
    TURNS = 10      # event-loop turns between two looks at a quiet engine

    def waiting(self) -> int:
        """Tasks that wait legitimately (across the controller's changes): each live agent's,
        each sign-off's, each waiting for a slot, each A2A message awaiting its reply, each
        on-complete process waiting for reflections."""
        procs = sum(1 for p in self.ctl.svc.procs.values() if p.returncode is None)
        signoffs = sum(len(ce._signoffs.waiting) for ce in self.ctl.changes.values())
        inbox = self.ctl.a2a.inbox if self.ctl.a2a is not None else None
        messages = sum(1 for (cid, card) in (inbox._messages if inbox else {})
                       for i in inbox.pending(cid, card) if not i.reply.done())
        reflecting = sum(ce._flow.awaiting_reflections for ce in self.ctl.changes.values())
        return procs + signoffs + self.ctl.svc.dispatcher.waiting + messages + reflecting

    def _fingerprint(self):
        ch = self.ce.change
        return (ch.status, tuple((k, t.status, t.attempt) for k, t in self.ce.st.tickets.items()),
                tuple((k, r.status, r.sandbox_deleted) for k, r in self.ce.runs.items()))

    @property
    def git(self):
        """The test workspace's hub."""
        return self.ctl.hub(REPO)

    def ticket(self, tid: str) -> Ticket:
        """A ticket of the tree, or of a side process (the retrospective)."""
        t = self.ce.ticket(tid)
        assert t is not None, f"no ticket {tid}; tickets={self.status()}"
        return t

    def status(self, tid: str | None = None):
        if tid:
            return self.ce.ticket(tid).status
        return {k: t.status for k, t in self.ce.st.tickets.items()}

    def run(self, tid: str, role: str | None = None) -> Run:
        """The live run of ticket ``tid`` (of ``role``)."""
        live = [r for r in self.ce.runs.values() if r.ticket == tid and r.status in LIVE_RUN
                and not r.consult and (role is None or r.role == role)]
        assert len(live) == 1, f"expected one live run of {tid}/{role}; got {live}; " \
                               f"tickets={self.status()}"
        return live[0]

    def runs(self, tid: str, role: str | None = None) -> list[Run]:
        return [r for r in self.ce.runs.values() if r.ticket == tid
                and (role is None or r.role == role)]

    def steps(self, run: Run) -> list[list[str]]:
        """The preparation steps run in the run's sandbox: script name and arguments."""
        return [[a[1].rsplit("/", 1)[-1], *a[2:]] for sb, a in self.runner.execs
                if sb == run.sandbox and a[0] == "bash" and a[1].endswith(".sh")
                and "/payload/" in a[1]]

    async def exit_agent(self, run: Run, rc: int = 0) -> None:
        self.ctl.svc.procs[run.id].exit(rc)
        await self.settle()

    async def human(self, tid: str, action: str, approver: str = "miles", **payload):
        res = await self.ce.human_action(tid, action, approver, payload)
        await self.settle()
        return res

    async def signoff(self, tid: str, approve: bool = True, reason: str = "",
                      approver: str = "miles"):
        """A decision owner's decision on the sign-off ``tid`` (waiting for them)."""
        assert tid in self.ce.signoffs_waiting(), \
            f"{tid} is not waiting for a sign-off; tickets={self.status()}"
        return await self.human(tid, "approve" if approve else "reject", approver,
                                **({"reason": reason} if reason else {}))

    async def change_action(self, action: str, approver: str = "miles", **payload):
        res = await self.ce.change_action(action, approver, payload)
        await self.settle()
        return res

    # ------------------------------------------------------------------ playing an agent
    async def write(self, run: Run, rel: str, content: str | dict) -> str:
        """Write ``rel`` in the run's ticket's directory (``tickets/<id>/<rel>``)."""
        if not isinstance(content, str):
            content = json.dumps(content)
        path = f"tickets/{run.ticket}/{rel}"
        v = await self.ce.agent_write(run, path, content, self.ce.latest(path))
        return v.attestation

    async def report(self, run: Run, atts=(), data=None, status="DONE", *, outputs=None,
                     exit_agent=True, blocked_reason=None, missing_decision=None,
                     summary="done"):
        out = await self.ce.handle_report(run.id, status, summary, list(atts), data,
                                          blocked_reason, missing_decision, False, outputs or {})
        if out.accepted and exit_agent:               # report records; the reply ends the task
            await self.reply(run, "input-required" if status == "BLOCKED" else "completed")
            self.ctl.svc.procs[run.id].exit(0)
        await self.settle()
        return out

    async def reply(self, run: Run, state: str = "completed", text: str = "done") -> dict:
        """The agent ends (or pauses) its run's task (MCP a2a_reply)."""
        return await self.ce.reply_task(run, state, text)

    def commit(self, run: Run, files: dict[str, str], *, push: bool = True,
               base: str | None = None) -> str:
        """Commit ``files`` on top of the run's base and push to its branch; return the sha."""
        wt = self.tmp / f"wt-{run.id}-{len(os.listdir(self.tmp))}"
        g = lambda *a, **kw: subprocess.run(["git", *a], cwd=wt, check=True, capture_output=True,
                                            text=True, **kw).stdout.strip()
        subprocess.run(["git", "clone", "-q", str(self.git.repo), str(wt)], check=True,
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
        req = await self.write(r, "requirements.md", f"# Requirements\n\n{reqs}")
        rsk = await self.write(r, "risk.json", risk or signals())
        return await self.report(r, [req, rsk], outputs={"requirements": req, "risk": rsk})

    async def plan(self, tasks=None, oracle=None, environment: dict | None = None,
                   criteria: tuple[str, ...] | None = None, doc: dict | None = None,
                   scaffold: dict | None = None, tid: str = "design"):
        """The planner's outputs: the spec and its delta specs, the oracle, the environment,
        and the tickets (``doc``; else built from task-like dicts, ``tasks``, with the tier's
        criteria – ``criteria`` to say otherwise)."""
        r = self.run(tid)
        reqs = re.findall(r"REQ-[0-9]+", self.ce.doc("tickets/intake/requirements.md").content)
        spec = await self.write(r, "spec.md", f"# Spec (attempt {r.attempt})\n")
        specs = [await self.write(r, f"specs/{q}.md", (
            f"# {q} delta (attempt {r.attempt})\n\n## Current behaviour\nmissing\n\n"
            "## Change\nadd it\n\n## Acceptance criteria\n- works\n")) for q in dict.fromkeys(reqs)]
        orc = await self.write(r, "oracle.json", oracle or ORACLE)
        env = await self.write(r, "environment.json", environment or (
            ENVIRONMENT if self.cfg.environment is not None else NO_ENVIRONMENT))
        plan = await self.write(r, "tickets.json", doc or plan_doc(tasks or TASKS, criteria or (
            ("verify", "can-merge") if self.ce.change.tier == "R0"
            else ("verify", "review", "can-merge")), scaffold))
        return await self.report(r, [spec, *specs, orc, env, plan], outputs={
            "spec": spec, "specs": specs, "oracle": orc, "environment": env, "tickets": plan})

    async def critique(self, verdict: str = "PASS"):
        r = self.run("critique")
        design = self.ticket("design")
        judged = [x for v in design.outputs.values() for x in (v if isinstance(v, list) else [v])]
        findings = [] if verdict == "PASS" else [{
            "severity": "WARNING", "confidence": "HIGH", "title": "spec too thin",
            "fix": "expand the spec", "attestations": [design.outputs["spec"]]}]
        doc = await self.write(r, "critique.md", f"critique {verdict}")
        return await self.report(r, [*judged, doc], {"verdict": verdict, "findings": findings,
                                                     "checked": ["traceability"]},
                                 outputs={"critique": doc})

    async def oracle(self, files: dict | None = None, tid: str = "oracle"):
        r = self.run(tid)
        sha = self.commit(r, files or {"tests/acceptance/test_greet.sh": "grep -q hello src/greet.txt\n"})
        doc = await self.write(r, "tests.md", "AC-1 -> tests/acceptance/test_greet.sh")
        return await self.report(r, [f"git:{REPO}@{sha}", doc], outputs={"tests": doc})

    async def implement(self, task: str = "T1", files: dict | None = None,
                        evidence: str | None = None, **kw):
        r = self.run(task, self.ticket(task).type.partition(":")[2])   # implementer, scaffolder
        work = self.ticket(task).work
        sha = self.commit(r, files or {p: f"hello from {task} attempt {r.attempt}\n"
                                       for p in work["files_modified"]})
        ev = await self.write(r, "evidence.md", evidence if evidence is not None
                              else "\n".join(f"- `{c}` -> exit 0" for c in work["verification"]))
        return await self.report(r, [f"git:{REPO}@{sha}", ev], outputs={"evidence": ev}, **kw)

    async def review(self, task: str = "T1", verdict: str = "PASS", role: str = "reviewer",
                     title: str = "missing newline handling"):
        tid = f"{'review' if role == 'reviewer' else role}:{task}"
        r = self.run(tid, role)
        cand = f"git:{REPO}@{r.judged_sha}"
        findings = [] if verdict == "PASS" else [{
            "severity": "BLOCKER", "confidence": "HIGH", "dimension": "correctness",
            "title": title, "observation": "x", "impact": "y", "fix": "rewrite it",
            "attestations": [cand]}]
        doc = await self.write(r, "review.md", verdict)
        return await self.report(r, [cand, doc], {"verdict": verdict, "findings": findings,
                                                  "checked": ["diff"]}, outputs={"review": doc})

    async def integration(self, verdict: str = "PASS"):
        r = self.run("integration")
        cmds = self.ce.oracle_commands()
        doc = await self.write(r, "report.md", "results")
        head = f"git:{REPO}@{r.judged_sha or r.base_sha}"      # a judge: delivery's head
        findings = [] if verdict == "PASS" else [{
            "severity": "BLOCKER", "confidence": "HIGH", "dimension": "correctness",
            "title": "the greeting is never printed", "observation": "x", "impact": "y",
            "fix": "print it", "attestations": [head]}]
        return await self.report(r, [head, doc], {
            "verdict": verdict, "findings": findings,
            "results": [{"command": c, "exit_code": 0} for c in cmds]}, outputs={"report": doc})

    PR = ("# Greet the user\n\n## Motivation\n\nREQ-1: greet.txt says hello.\n\n"
          "## Changes\n\n- src/greet.txt\n\n## Verification\n\n- the acceptance tests\n")

    async def describe(self, text: str | None = None):
        r = self.run("describe", "describer")
        doc = await self.write(r, "pr.md", text if text is not None else self.PR)
        return await self.report(r, [f"git:{REPO}@{r.judged_sha}", doc], outputs={"pr": doc})

    # ------------------------------------------------------------------ whole stretches
    async def to_build(self, risk: dict | None = None, **plan_kw) -> None:
        """Intake (``risk``: its signals, so its tier), H-1, the plan, a passing critique, H-E1
        (provisioning follows) and – R2+ – the oracle, its preflight and H-2 (R3: the
        baseline and H-2b): the builds start."""
        await self.intake(risk)
        await self.signoff("H-1")
        await self.plan(**plan_kw)
        await self.critique("PASS")
        await self.signoff("H-E1")
        assert self.status("provision") == "passed", self.status()
        if self.status("scaffold") != "cancelled":
            await self.implement("scaffold")
            await self.review("scaffold")
        if "oracle" in self.ce.st.tickets:
            await self.oracle()
            await self.signoff("H-2")
        if "baseline" in self.ce.st.tickets:            # the controller's: then signed off
            await self.signoff("H-2b")

    async def to_end(self, **kw) -> None:
        """A whole change: to the builds, each build and its review, the integration, the
        description and H-3 – published."""
        await self.to_build(**kw)
        await self.to_end_from_build()

    async def to_end_from_build(self) -> None:
        """From the builds on: each build and its review, the integration, the description and
        H-3 – published."""
        for task in [t for t, x in self.ce.st.tickets.items() if x.type == "agent:implementer"]:
            await self.implement(task)
            if f"review:{task}" in self.ce.st.tickets:
                await self.review(task)
        await self.integration()
        await self.describe()
        await self.signoff("H-3")


NO_ENVIRONMENT = {"summary": "the default image", "profiles": {"default": {"tools": []}},
                  "roles": {}, "rationale": "no environment stage"}


def plan_doc(tasks: list[dict], criteria: tuple[str, ...], scaffold: dict | None = None) -> dict:
    """The planner's tickets for task-like dicts: each a build under ``builds`` with
    ``criteria`` (``verify``, ``review``, ``security``, ``can-merge``), grouped by its
    ``depends_on`` (a task's group comes after its dependencies'); the skeleton's ``scaffold``
    given its work (``scaffold``), else cancelled. (The integration is ``delivery``'s criterion:
    the skeleton's.)"""
    types = {"verify": "controller:verify", "review": "agent:reviewer",
             "security": "agent:security", "can-merge": "controller:can-merge"}
    level: dict[str, int] = {}
    for t in tasks:
        level[t["id"]] = 1 + max((level[d] for d in t.get("depends_on", [])), default=-1)
    adds, first = [], set()
    for t in sorted(tasks, key=lambda t: level[t["id"]]):
        g = level[t["id"]]
        work = {k: v for k, v in t.items() if k not in ("id", "title", "depends_on")}
        adds.append({"into": "builds", "group": g, "join": g in first, "ticket": {
            "id": t["id"], "type": "agent:implementer", "goal": t.get("title", t["id"]),
            "inputs": ["spec", "specs"], "work": work,
            "criteria": [{"id": f"{c}:{t['id']}", "type": types[c]} for c in criteria]}})
        first.add(g)
    if scaffold is not None:
        return {"add": adds, "update": {"scaffold": {"work": scaffold}}}
    return {"add": adds, "cancel": ["scaffold"]}


def scenario(**limits):
    """Run an async test ``fn(flow)`` against a fresh change (``limits`` override config)."""
    def wrap(fn):
        @functools.wraps(fn)
        def test(tmp_path):
            async def go():
                f = await Flow.create(tmp_path, **limits)
                try:
                    await fn(f)
                finally:
                    await f.stop()
            asyncio.run(go())
        test.__signature__ = inspect.Signature(
            [inspect.Parameter("tmp_path", inspect.Parameter.POSITIONAL_OR_KEYWORD)])
        return pytest.mark.scenario(test)
    return wrap
