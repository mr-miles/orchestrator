"""Where environment images and network providers are made. Both operations are idempotent:
content-addressed names mean an existing image or provider is simply found and kept.

* :class:`PodmanBuilder` – ``podman build`` (and ``push`` with a registry) on the controller host;
  ``openshell profile import`` + ``provider create`` in ``sandbox.workspace``.
* :class:`NoBuilder` – the local runner: default images, no providers (no isolation anyway).
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import yaml

from ..config import Config
from .images import ImageRecipe
from .network import NetworkProvider
from .runner import openshell_cli


class BuildError(Exception):
    pass


class EnvironmentBuilder:
    async def ensure_image(self, recipe: ImageRecipe) -> tuple[str | None, str]:
        """Build (or find) the image; return the reference sandboxes use (None: the runner's
        default image) and a log."""
        raise NotImplementedError

    async def ensure_provider(self, provider: NetworkProvider) -> str:
        """Import the profile and create the provider unless they exist; return a log."""
        raise NotImplementedError


class NoBuilder(EnvironmentBuilder):
    async def ensure_image(self, recipe):
        return None, f"local runner: {recipe.tag} not built (default image)\n"

    async def ensure_provider(self, provider):
        return f"local runner: provider {provider.id} not created\n"


async def _run(argv: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"timeout after {timeout}s"
    return proc.returncode, out.decode(errors="replace")


class PodmanBuilder(EnvironmentBuilder):
    def __init__(self, cfg: Config):
        assert cfg.environment is not None
        self.env = cfg.environment
        self.cli = openshell_cli(cfg)

    async def _step(self, log: list[str], argv: list[str], timeout: float = 600,
                    probe: bool = False) -> int:
        log.append("$ " + " ".join(argv))
        rc, out = await _run(argv, timeout)
        log.append(out.rstrip()[-20000:])
        if rc != 0 and not probe:
            raise BuildError(f"{' '.join(argv[:3])} failed ({rc}): {out.strip()[-1500:]}\n"
                             + "\n".join(log)[-4000:])
        return rc

    async def ensure_image(self, recipe):
        if not recipe.base:
            raise BuildError("environment images are built FROM sandbox.image (agents) and "
                             "sandbox.verifier_image (verifier); set them in the config")
        log: list[str] = []
        pod = self.env.podman_bin
        if await self._step(log, [pod, "image", "exists", recipe.tag], probe=True) == 0:
            log.append(f"{recipe.tag} exists; reused")
            return recipe.tag, "\n".join(log)
        with tempfile.TemporaryDirectory(prefix="legion-env-") as ctx:
            for name, text in recipe.files.items():
                (Path(ctx) / name).write_text(text)
            await self._step(log, [pod, "build", "--pull=newer", "-t", recipe.tag, ctx],
                             timeout=self.env.build_timeout_s)
        if self.env.registry:
            await self._step(log, [pod, "push", recipe.tag], timeout=self.env.build_timeout_s)
        return recipe.tag, "\n".join(log)

    async def ensure_provider(self, provider):
        log: list[str] = []
        pid = provider.id
        if await self._step(log, [*self.cli, "profile", "export", pid, "-o", "json"], probe=True) != 0:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                yaml.safe_dump(provider.profile(), f, sort_keys=False)
            try:
                await self._step(log, [*self.cli, "profile", "import", "-f", f.name])
            finally:
                Path(f.name).unlink(missing_ok=True)
        else:
            log.append(f"profile {pid} exists; kept (content-addressed)")
        if await self._step(log, [*self.cli, "provider", "get", pid], probe=True) != 0:
            await self._step(log, [*self.cli, "provider", "create", "--name", pid, "--type", pid])
        else:
            log.append(f"provider {pid} exists; kept")
        return "\n".join(log)


def make_builder(cfg: Config) -> EnvironmentBuilder:
    return PodmanBuilder(cfg) if cfg.sandbox.runner == "openshell" and cfg.environment else NoBuilder()
