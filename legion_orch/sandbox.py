"""Sandbox runners.

``OpenShellRunner`` drives the NVIDIA OpenShell CLI:

    openshell sandbox create --name <n> --detach --no-tty [--from IMG] [--provider P]...
                             --policy <policy.yaml> --upload <payload>:/sandbox --no-git-ignore
    openshell sandbox exec <n> --no-tty [--workdir W] -- <argv>
    openshell sandbox delete <n>

``LocalRunner`` emulates the same contract with local directories and subprocesses. It provides
**no isolation** and exists for development and tests only. Inside payload scripts the sandbox
root is ``${SANDBOX_ROOT:-/sandbox}``.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config


@dataclass
class SandboxSpec:
    name: str
    payload_dir: Path          # uploaded as /sandbox/payload
    policy_file: Path
    agent: bool = True         # False => deterministic verifier (no providers, git fetch only)
    labels: dict[str, str] = field(default_factory=dict)


class SandboxError(Exception):
    pass


async def _run(argv: list[str], timeout: float, env: dict | None = None,
               cwd: str | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        env=env, cwd=cwd)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timeout after {timeout}s"
    return proc.returncode, out.decode(errors="replace")


class Runner:
    async def create(self, spec: SandboxSpec) -> None: ...
    async def upload(self, name: str, payload_dir: Path) -> None: ...
    async def exec(self, name: str, argv: list[str], *, workdir: str | None = None,
                   timeout: float = 600) -> tuple[int, str]: ...
    async def start_agent(self, name: str, log_file: Path) -> asyncio.subprocess.Process: ...
    async def delete(self, name: str) -> None: ...


class OpenShellRunner(Runner):
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.bin = cfg.sandbox.openshell_bin

    async def _os(self, *args: str, timeout: float = 600) -> str:
        rc, out = await _run([self.bin, *args], timeout)
        if rc != 0:
            raise SandboxError(f"openshell {' '.join(args[:3])} failed ({rc}): {out.strip()[-2000:]}")
        return out

    async def create(self, spec: SandboxSpec) -> None:
        sc = self.cfg.sandbox
        args = ["sandbox", "create", "--name", spec.name, "--detach", "--no-tty",
                "--policy", str(spec.policy_file),
                "--upload", f"{spec.payload_dir}:/sandbox", "--no-git-ignore"]
        image = sc.image if spec.agent else (sc.verifier_image or sc.image)
        if image:
            args += ["--from", image]
        if spec.agent:
            for p in sc.providers:
                args += ["--provider", p]
        for k, v in spec.labels.items():
            args += ["--label", f"{k}={v}"]
        args += sc.extra_create_args
        await self._os(*args, timeout=900)

    async def upload(self, name: str, payload_dir: Path) -> None:
        await self._os("sandbox", "upload", name, f"{payload_dir}", "/sandbox", "--no-git-ignore")

    async def exec(self, name, argv, *, workdir=None, timeout=600):
        args = [self.bin, "sandbox", "exec", name, "--no-tty"]
        if workdir:
            args += ["--workdir", workdir]
        return await _run(args + ["--", *argv], timeout)

    async def start_agent(self, name, log_file):
        log = open(log_file, "ab")
        try:
            return await asyncio.create_subprocess_exec(
                self.bin, "sandbox", "exec", name, "--no-tty", "--",
                "bash", "/sandbox/payload/run-agent.sh",
                stdout=log, stderr=asyncio.subprocess.STDOUT)
        finally:
            log.close()

    async def delete(self, name):
        await self._os("sandbox", "delete", name, timeout=300)


class LocalRunner(Runner):
    """Development runner: /sandbox maps to <state>/local-sandboxes/<name>."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.root = cfg.state_dir / "local-sandboxes"

    def _root(self, name: str) -> Path:
        return self.root / name

    def _map(self, name: str, s: str) -> str:
        return str(self._root(name)) + s[len("/sandbox"):] if s.startswith("/sandbox") else s

    def _env(self, name: str) -> dict:
        env = dict(os.environ)
        env["SANDBOX_ROOT"] = str(self._root(name))
        return env

    async def create(self, spec):
        r = self._root(spec.name)
        if r.exists():
            raise SandboxError(f"sandbox {spec.name} already exists")
        r.mkdir(parents=True)
        await self.upload(spec.name, spec.payload_dir)

    async def upload(self, name, payload_dir):
        shutil.copytree(payload_dir, self._root(name) / payload_dir.name, dirs_exist_ok=True)

    async def exec(self, name, argv, *, workdir=None, timeout=600):
        argv = [self._map(name, a) for a in argv]
        cwd = self._map(name, workdir) if workdir else str(self._root(name))
        return await _run(argv, timeout, env=self._env(name), cwd=cwd)

    async def start_agent(self, name, log_file):
        log = open(log_file, "ab")
        try:
            return await asyncio.create_subprocess_exec(
                "bash", str(self._root(name) / "payload" / "run-agent.sh"),
                stdout=log, stderr=asyncio.subprocess.STDOUT, env=self._env(name),
                cwd=str(self._root(name)))
        finally:
            log.close()

    async def delete(self, name):
        shutil.rmtree(self._root(name), ignore_errors=True)


def make_runner(cfg: Config) -> Runner:
    if cfg.sandbox.runner == "openshell":
        return OpenShellRunner(cfg)
    if cfg.sandbox.runner == "local":
        return LocalRunner(cfg)
    raise ValueError(f"unknown sandbox runner {cfg.sandbox.runner!r}")


# --------------------------------------------------------------------------- payload scripts
RUN_AGENT_SH = r"""#!/usr/bin/env bash
# Generated by legion-orch. Prepares the working copy from the controller's git remote and starts
# the agent. Re-entrant: rework runs in the same sandbox reuse /sandbox/work with a new token.
set -euo pipefail
SB="${SANDBOX_ROOT:-/sandbox}"
P="$SB/payload"
set -a; source "$P/run.env"; set +a
AUTH="Authorization: Bearer $LEGION_TOKEN"
if [ "$LEGION_NEEDS_REPO" = "1" ]; then
  if [ ! -d "$SB/work/.git" ]; then
    git -c http.extraHeader="$AUTH" clone --quiet "$LEGION_GIT_URL" "$SB/work"
  fi
  cd "$SB/work"
  git config http.extraHeader "$AUTH"
  git config user.name "legion-$LEGION_ROLE"
  git config user.email "$LEGION_RUN_ID@legion.local"
  git fetch --quiet origin
  git cat-file -e "$LEGION_BASE_SHA^{commit}" 2>/dev/null || git fetch --quiet origin "$LEGION_BASE_SHA"
  if git rev-parse --verify --quiet "refs/heads/$LEGION_BRANCH" >/dev/null; then
    git checkout --quiet "$LEGION_BRANCH"
  else
    git checkout --quiet -B "$LEGION_BRANCH" "$LEGION_BASE_SHA"
  fi
else
  mkdir -p "$SB/work"; cd "$SB/work"
fi
export LEGION_MCP_CONFIG="$P/mcp.json"
exec bash -lc "$LEGION_AGENT_COMMAND"
"""

VERIFY_SETUP_SH = r"""#!/usr/bin/env bash
# Generated by legion-orch. Deterministic verifier: read-only clone of the commit under test.
set -euo pipefail
SB="${SANDBOX_ROOT:-/sandbox}"
source "$SB/payload/run.env"
git -c http.extraHeader="Authorization: Bearer $LEGION_TOKEN" clone --quiet --no-checkout "$LEGION_GIT_URL" "$SB/work"
cd "$SB/work"
git cat-file -e "$1^{commit}" 2>/dev/null || git -c http.extraHeader="Authorization: Bearer $LEGION_TOKEN" fetch --quiet origin "$1"
git checkout --quiet --detach "$1"
"""
