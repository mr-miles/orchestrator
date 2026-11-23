"""``controller:verify``: the ticket's commands at the commit it judges, by the verification
service (a fresh verifier sandbox, the repository's setup first – G-V1, G-V3).

What a recorded verification means (:func:`verification_outcome`, today's ``Verify._interpret``):

* the verifier's infrastructure failed: a fault, retried with backoff;
* a command is not in the verifier image (exit 127, G-V2): a fault no retry and no agent can fix
  – a human fixes the image, then retries;
* every command passed: pass; otherwise fail, each failing command a blocking finding (the setup
  included: a setup that fails is the commit's fault, like any command)."""
from __future__ import annotations

from typing import Protocol

from ...model import Verification
from .base import Backoff, Fault, Judged, Outcome, Work, attempt

EXIT_NOT_FOUND = 127        # bash: command not found (126, not executable, is a fixable repo file)
TAIL = 800


class VerifyPort(Protocol):
    """The verification service, for tickets: which commands a ticket runs, and a verification
    of them at a commit (one per commit and commands: a known result is not run again)."""

    def commands(self, work: Work) -> tuple[str, ...]: ...

    async def verify(self, work: Work, sha: str, commands: tuple[str, ...]) -> Verification: ...

    async def stop(self, ticket: str) -> None: ...


def verification_outcome(res: Verification) -> Outcome:
    if res.infra_error:
        return Fault(f"verification infrastructure: {res.infra_error}")
    fails = [r for r in res.results if r.exit_code != 0]
    if unrunnable := [r for r in fails if r.exit_code == EXIT_NOT_FOUND and not r.setup]:
        return Fault("G-V2: command(s) not found in the verifier sandbox: "
                     + ", ".join(f"`{r.command}`" for r in unrunnable), transient=False)
    if res.ok and not fails:
        return Judged(True)
    return Judged(False, tuple(
        f"BLOCKER: {'the setup ' if r.setup else ''}`{r.command}` exited {r.exit_code}: "
        f"{r.output[-TAIL:]}" for r in fails) or ("BLOCKER: the verification did not pass",))


class VerifyExecutor:
    def __init__(self, port: VerifyPort, backoff: Backoff = Backoff()):
        self.port, self.backoff = port, backoff

    async def run(self, work: Work) -> Outcome:
        sha = work.judged or work.base
        if sha is None:
            return Fault("nothing to verify: no commit to judge", transient=False)
        commands = self.port.commands(work)
        if not commands:
            return Fault("nothing to verify: the ticket has no commands", transient=False)

        async def once() -> Outcome:
            return verification_outcome(await self.port.verify(work, sha, commands))
        return await attempt(once, self.backoff)

    async def stop(self, ticket: str) -> None:
        await self.port.stop(ticket)
