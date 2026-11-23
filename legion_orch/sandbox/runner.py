"""Sandbox runners.

``OpenShellRunner`` drives the NVIDIA OpenShell CLI:

    (every call is prefixed with ``openshell --workspace <workspace> [-g <gateway>]``)
    openshell sandbox create <flags rendered from policies/agent-sandbox.template.yaml>
    openshell sandbox get <n> --output json          # sandbox id used in its SPIFFE ID
    openshell sandbox exec <n> --no-tty [--workdir W] -- <argv>
    openshell sandbox delete <n>

No credential is placed in a sandbox. The controller's providers (``legion-mcp`` for agents, and
``legion-git`` or ``legion-git-push``; created once per workspace, see ``legion-orch
setup-openshell``) give the sandbox its controller endpoints and make the gateway supervisor
exchange the sandbox's SPIFFE JWT-SVID at the controller and inject the bearer into requests
to it.

``LocalRunner`` emulates the same contract with local directories and subprocesses. It provides
**no isolation** and exists for development and tests only. It also stands in for the gateway:
before starting a process it obtains a bearer through ``gateway(sandbox_ref)`` (dev SPIRE → real
``/oauth/token``) and passes it in the process environment, never in a file. Inside payload
scripts the sandbox root is the directory above the script's own (``sandbox.root`` in OpenShell).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..util import now_iso, run_command
from .names import check_name
from .templates import base_values, label_value, render, sandbox_providers, spec_to_args

# Obtains a bearer for a sandbox ref (what the OpenShell gateway does for real sandboxes).
Gateway = Callable[[str], Awaitable[str]]


@dataclass
class SandboxEnv:
    """A sandbox's environment (from the approved environment spec): image, network policies
    (``network_policies`` entries for the sandbox policy) and resources. None fields keep the
    configured defaults."""
    image: str | None = None
    network: dict[str, dict] = field(default_factory=dict)
    cpu: str | None = None              # --cpu
    memory: str | None = None           # --memory
    driver_config: str | None = None    # --driver-config-json (disk)


@dataclass
class SandboxSpec:
    name: str
    payload_dir: Path          # uploaded as /sandbox/payload
    policy_file: Path
    agent: bool = True         # False => deterministic verifier (legion-git only)
    push: bool = False         # agents: legion-git-push instead of legion-git
    change: str = ""
    task: str = ""
    role: str = ""
    run: str = ""
    env: SandboxEnv | None = None


class SandboxError(Exception):
    pass


CHANGE_LABEL = "legion.change"
STOPPED = "Stopped"


@dataclass(frozen=True)
class GatewaySandbox:
    """A sandbox as the gateway lists it (``openshell sandbox list``): its name, id, phase
    (``Ready``, ``Stopped``, …) and labels; our sandboxes' exact ids are annotations
    (``legion.task``)."""
    name: str
    id: str = ""
    phase: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)

    @property
    def change(self) -> str | None:
        """The change it was created for (its ``legion.change`` label), if it is ours."""
        return self.labels.get(CHANGE_LABEL)

    @property
    def stopped(self) -> bool:
        return self.phase == STOPPED


@dataclass(frozen=True)
class Listing:
    """The gateway's sandboxes matching a selector, every page read; ``complete`` is False only
    when it gave up before the last page – and only a complete listing says a sandbox is gone."""
    sandboxes: tuple[GatewaySandbox, ...]
    complete: bool = True


def _selects(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())


async def _run(argv: list[str], timeout: float, env: dict | None = None,
               cwd: str | None = None) -> tuple[int, str]:
    """A command's exit code and output; a timeout is exit 124, as timeout(1) has it."""
    try:
        return await run_command(argv, timeout, env=env, cwd=cwd)
    except TimeoutError:
        return 124, f"timeout after {timeout}s"


class LoggedProcess:
    """An agent process whose output the controller writes to its log as it arrives, adding
    the time it arrived to each stream-json event (``legion_ts``; the agent CLI's events carry
    none). Other lines (setup script, shell) are written unchanged."""

    CHUNK = 1 << 16

    def __init__(self, proc: asyncio.subprocess.Process, log_file: Path):
        self.proc = proc
        self._pump = asyncio.ensure_future(self._copy(log_file))

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    def kill(self) -> None:
        self.proc.kill()

    async def wait(self) -> int:
        rc = await self.proc.wait()
        await self._pump
        return rc

    async def _copy(self, log_file: Path) -> None:
        pending = b""
        with open(log_file, "ab") as log:
            while chunk := await self.proc.stdout.read(self.CHUNK):
                *lines, pending = (pending + chunk).split(b"\n")
                log.write(b"".join(stamp(line) + b"\n" for line in lines))
                log.flush()
            if pending:
                log.write(stamp(pending) + b"\n")


def stamp(line: bytes) -> bytes:
    """``{"type": ...}`` -> ``{"legion_ts": "<now>", "type": ...}``; other lines unchanged."""
    if line.startswith(b'{"') and line.rstrip().endswith(b"}"):
        return b'{"legion_ts":"' + now_iso().encode() + b'",' + line[1:]
    return line


async def spawn_logged(argv: list[str], log_file: Path, **kw) -> LoggedProcess:
    """Start ``argv`` with stdin closed and stdout+stderr copied into ``log_file``."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, **kw)
    return LoggedProcess(proc, log_file)


def env_values(values: dict, env: SandboxEnv, *, agent: bool) -> dict:
    """Template values for a sandbox with an environment: its image and resources (the agent
    or the verifier template's placeholders). Its network policies are in the policy file."""
    image = "AGENT_IMAGE" if agent else "VERIFIER_IMAGE"
    out = dict(values) | {"SANDBOX_CPU": env.cpu, "SANDBOX_MEMORY": env.memory,
                          "DRIVER_CONFIG": env.driver_config}
    if env.image:
        out[image] = env.image
    return out


class Runner:
    async def create(self, spec: SandboxSpec) -> str:
        """Create the sandbox; return its workload reference (the SPIFFE ID suffix)."""
    async def upload(self, name: str, payload_dir: Path) -> None: ...
    async def exec(self, name: str, argv: list[str], *, workdir: str | None = None,
                   timeout: float = 600) -> tuple[int, str]: ...
    async def start_agent(self, name: str, log_file: Path) -> asyncio.subprocess.Process: ...
    async def delete(self, name: str) -> None: ...
    async def stop(self, name: str) -> None:
        """Stop a sandbox kept between runs; its workspace (and agent session) is preserved."""
    async def start(self, name: str) -> None:
        """Start a stopped sandbox again."""
    async def list(self, selector: dict[str, str] | None = None) -> Listing:
        """The workspace's sandboxes whose labels match ``selector`` (all: none)."""
        return Listing(())


def openshell_cli(cfg: Config) -> list[str]:
    """The openshell command prefix: every call targets the configured workspace (and gateway)."""
    sc = cfg.sandbox
    return [sc.openshell_bin, "--workspace", sc.workspace] + (["-g", sc.gateway] if sc.gateway else [])


class OpenShellRunner(Runner):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.cli = openshell_cli(cfg)   # binary + global --workspace / -g flags

    async def _os(self, *args: str, timeout: float = 600) -> str:
        rc, out = await _run([*self.cli, *args], timeout)
        if rc != 0:
            raise SandboxError(f"openshell {' '.join(args[:3])} failed ({rc}): {out.strip()[-2000:]}")
        return out

    async def create(self, spec: SandboxSpec) -> str:
        """``sandbox create`` from policies/{agent,verifier}-sandbox.template.yaml."""
        check_name(spec.name)
        values = base_values(self.cfg) | {
            "SANDBOX_NAME": spec.name, "POLICY_FILE": str(spec.policy_file),
            "PAYLOAD_UPLOAD": f"{spec.payload_dir}:{self.cfg.sandbox.root}", "CHANGE_ID": spec.change,
            "TASK_ID": spec.task, "ROLE": spec.role, "RUN_ID": spec.run or spec.name,
            "SANDBOX_PROVIDERS": sandbox_providers(self.cfg, push=spec.push)}
        if spec.env:
            values = env_values(values, spec.env, agent=spec.agent)
        tmpl = render(self.cfg, "agent_sandbox" if spec.agent else "verifier_sandbox", values)
        await self._os("sandbox", "create", *spec_to_args(tmpl), *self.cfg.sandbox.extra_create_args,
                       timeout=900)
        info = json.loads(await self._os("sandbox", "get", spec.name, "--output", "json"))
        ref = info.get(self.cfg.identity.subject_field) if isinstance(info, dict) else None
        if not ref:
            raise SandboxError(f"`sandbox get {spec.name}` has no {self.cfg.identity.subject_field!r} "
                               "field to match the SPIFFE ID; set identity.subject_field")
        return str(ref)

    async def upload(self, name: str, payload_dir: Path) -> None:
        await self._os("sandbox", "upload", name, f"{payload_dir}", self.cfg.sandbox.root,
                       "--no-git-ignore")

    async def exec(self, name, argv, *, workdir=None, timeout=600):
        args = [*self.cli, "sandbox", "exec", name, "--no-tty"]
        if workdir:
            args += ["--workdir", workdir]
        return await _run(args + ["--", *argv], timeout)

    async def start_agent(self, name, log_file):
        # stdin is closed: `sandbox exec` forwards it into the sandbox, where nothing reads it,
        # and input that fills the supervisor's buffer gets the agent killed (exit 74)
        return await spawn_logged([*self.cli, "sandbox", "exec", name, "--no-tty", "--",
                                   "bash", f"{self.cfg.sandbox.root}/payload/run-agent.sh"],
                                  log_file)

    async def delete(self, name):
        await self._os("sandbox", "delete", name, timeout=300)

    async def stop(self, name):
        await self._os("sandbox", "stop", name, timeout=300)

    async def start(self, name):
        await self._os("sandbox", "start", name, timeout=900)

    MAX_PAGES = 100

    async def list(self, selector: dict[str, str] | None = None) -> Listing:
        """``sandbox list --output json [--selector k=v,…]``, every page (``--page-token``)."""
        args = ["sandbox", "list", "--output", "json"]
        if selector:
            args += ["--selector", ",".join(f"{k}={v}" for k, v in selector.items())]
        found: list[GatewaySandbox] = []
        token = ""
        for _ in range(self.MAX_PAGES):
            out = json.loads(await self._os(*args, *(["--page-token", token] if token else []),
                                            timeout=120))
            found += [GatewaySandbox(name=s.get("name", ""), id=s.get("id", ""),
                                     phase=s.get("phase", ""), labels=s.get("labels") or {},
                                     annotations=s.get("annotations") or {})
                      for s in out.get("sandboxes", [])]
            token = out.get("next_page_token") or ""
            if not token:
                return Listing(tuple(found))
        return Listing(tuple(found), complete=False)       # it would not end: not concluded


class LocalRunner(Runner):
    """Development runner: /sandbox maps to <state>/local-sandboxes/<name>; ref == name."""

    def __init__(self, cfg: Config, gateway: Gateway):
        """``gateway(sandbox_ref)`` obtains a bearer for a sandbox the way the OpenShell gateway
        would (dev SPIRE SVID exchanged at the controller's token endpoint)."""
        self.cfg = cfg
        self.root = cfg.state_dir / "local-sandboxes"
        self.gateway = gateway

    def _root(self, name: str) -> Path:
        return self.root / name

    def _map(self, name: str, s: str) -> str:
        root = self.cfg.sandbox.root
        return str(self._root(name)) + s[len(root):] if s == root or s.startswith(root + "/") else s

    def _claude_dir(self, name: str) -> Path:
        return self._root(name) / "claude"

    async def _env(self, name: str) -> dict:
        env = dict(os.environ)
        env["SANDBOX_ROOT"] = str(self._root(name))
        env["LEGION_ACCESS_TOKEN"] = await self.gateway(name)
        # The agent runs as the operator: its own Claude config dir keeps the run's skills and
        # sessions out of the operator's (and other sandboxes') ~/.claude.
        env["CLAUDE_CONFIG_DIR"] = str(self._claude_dir(name))
        return env

    async def create(self, spec):
        check_name(spec.name)            # same rule as the gateway, so local runs catch it too
        r = self._root(spec.name)
        if r.exists():
            raise SandboxError(f"sandbox {spec.name} already exists")
        r.mkdir(parents=True)
        (r / ".legion-sandbox.json").write_text(json.dumps(   # what the gateway would list
            {"labels": {k: label_value(v) for k, v in (
                (CHANGE_LABEL, spec.change), ("legion.task", spec.task),
                ("legion.role", spec.role), ("legion.run", spec.run)) if v},
             "annotations": {"legion.task": spec.task}}))
        self._link_credentials(spec.name)
        await self.upload(spec.name, spec.payload_dir)
        return spec.name

    def _link_credentials(self, name: str) -> None:
        """The operator's Claude login, linked into the sandbox's config dir (an API key or OAuth
        token in the environment needs nothing)."""
        d = self._claude_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        own = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"
        if own.is_file() and not (d / ".credentials.json").exists():
            (d / ".credentials.json").symlink_to(own)

    async def upload(self, name, payload_dir):
        shutil.copytree(payload_dir, self._root(name) / payload_dir.name, dirs_exist_ok=True)

    async def exec(self, name, argv, *, workdir=None, timeout=600):
        argv = [self._map(name, a) for a in argv]
        cwd = self._map(name, workdir) if workdir else str(self._root(name))
        return await _run(argv, timeout, env=await self._env(name), cwd=cwd)

    async def start_agent(self, name, log_file):
        env = await self._env(name)
        return await spawn_logged(["bash", str(self._root(name) / "payload" / "run-agent.sh")],
                                  log_file, env=env, cwd=str(self._root(name)))

    async def delete(self, name):
        shutil.rmtree(self._root(name), ignore_errors=True)

    async def list(self, selector: dict[str, str] | None = None) -> Listing:
        """The local sandboxes (their directories); never stopped (stop is a no-op here)."""
        found = []
        for meta in sorted(self.root.glob("*/.legion-sandbox.json")) if self.root.exists() else []:
            data = json.loads(meta.read_text())
            if not _selects(selector or {}, data.get("labels", {})):
                continue
            found.append(GatewaySandbox(name=meta.parent.name, id=meta.parent.name, phase="Ready",
                                        labels=data.get("labels", {}),
                                        annotations=data.get("annotations", {})))
        return Listing(tuple(found))


def make_runner(cfg: Config, dev_gateway: Gateway | None = None) -> Runner:
    """The configured runner. The local runner emulates the gateway with ``dev_gateway``."""
    if cfg.sandbox.runner == "openshell":
        return OpenShellRunner(cfg)
    if cfg.sandbox.runner == "local":
        if dev_gateway is None:
            raise ValueError("the local runner needs gateway emulation (identity.dev_spire)")
        return LocalRunner(cfg, dev_gateway)
    raise ValueError(f"unknown sandbox runner {cfg.sandbox.runner!r}")
