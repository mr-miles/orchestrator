"""Work: a sandbox session and the commands executed in it, in order (plan: state-model.md).

An agent's run and a verification are the same thing with different commands: open a sandbox
session (a fresh sandbox, or a node's kept one), execute the commands one after another, close
the session (delete, pause or keep the sandbox). The dispatcher runs work in a slot and never
looks inside (:meth:`Work.execute`); what the results mean is decided above it – the Verify
phase judges a verification's command results, the role handler an agent's report.

A command's ``kind`` says what it is for its reader (preparing the working copy, the
repository's setup, a verification command, the agent's turn); ``required``: a failure stops
the commands after it.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from ...model import CommandResult

PREPARE, SETUP, COMMAND, AGENT = "prepare", "setup", "command", "agent"
OUTPUT_TAIL = 4000


class Session(Protocol):
    """A sandbox, open while the commands execute."""
    name: str                               # the sandbox's (it may change while opening)
    workdir: str                            # the working copy, inside the sandbox
    payload: str                            # the payload directory, inside the sandbox

    async def open(self) -> None: ...

    async def exec(self, argv: list[str], *, timeout: int,
                   workdir: str | None) -> tuple[int, str]: ...

    async def close(self) -> None: ...


@dataclass
class Command:
    label: str
    kind: str = COMMAND
    required: bool = False

    async def run(self, session: Session) -> CommandResult:
        raise NotImplementedError

    def result(self, rc: int, out: str) -> CommandResult:
        return CommandResult(command=self.label, exit_code=rc, output=out[-OUTPUT_TAIL:],
                             setup=self.kind == SETUP)


@dataclass
class Script(Command):
    """One of the payload's scripts (clone, checkout, branch, the repository's setup)."""
    script: str = ""
    args: tuple[str, ...] = ()
    timeout: int = 600
    in_work: bool = True                    # run in the working copy (not the clone itself)

    async def run(self, session: Session) -> CommandResult:
        rc, out = await session.exec(["bash", f"{session.payload}/{self.script}", *self.args],
                                     timeout=self.timeout,
                                     workdir=session.workdir if self.in_work else None)
        return self.result(rc, out)


@dataclass
class Shell(Command):
    """A shell command in the working copy (a verification command)."""
    timeout: int = 600

    async def run(self, session: Session) -> CommandResult:
        rc, out = await session.exec(["bash", "-lc", self.label], timeout=self.timeout,
                                     workdir=session.workdir)
        return self.result(rc, out)


@dataclass
class Step:
    command: Command
    result: CommandResult


@dataclass
class Outcome:
    """What executing the work did: each command's result in order, or why it stopped."""
    sandbox: str = ""
    steps: list[Step] = field(default_factory=list)
    error: str | None = None                # the session failed (opening, or a command raised)
    skipped: bool = False                   # its preparation found it no longer wanted

    def results(self, *kinds: str) -> list[CommandResult]:
        return [s.result for s in self.steps if not kinds or s.command.kind in kinds]

    @property
    def stopped(self) -> Step | None:
        """The required command that failed (the ones after it did not run)."""
        return next((s for s in self.steps if s.command.required and s.result.exit_code != 0),
                    None)


@dataclass
class Work:
    """A session and its commands, executed in order. ``ready``: whether it can start now (its
    sandbox's environment is not being built again) – the dispatcher gives it a slot only then.
    ``prepare`` (in the work's slot, before the session opens) may fill them in from the state
    at that moment; False: the work is no longer wanted and nothing runs. ``begun``: told as
    each command starts; ``log``: told of each command's result."""
    session: Session | None = None
    commands: list[Command] = field(default_factory=list)
    prepare: Callable[[Work], Awaitable[bool]] | None = None
    log: Callable[[Command, CommandResult], None] | None = None
    begun: Callable[[Command], None] | None = None
    ready: Callable[[], bool] | None = None

    def can_start(self) -> bool:
        return self.ready is None or self.ready()

    async def execute(self) -> Outcome:
        if self.prepare is not None and not await self.prepare(self):
            return Outcome(skipped=True)
        assert self.session is not None
        out = Outcome()
        try:
            await self.session.open()
            out.sandbox = self.session.name
            for cmd in self.commands:
                if self.begun:
                    self.begun(cmd)
                r = await cmd.run(self.session)
                out.steps.append(Step(cmd, r))
                if self.log:
                    self.log(cmd, r)
                if cmd.required and r.exit_code != 0:
                    break
        except Exception as e:
            out.error = str(e) or type(e).__name__
        finally:
            out.sandbox = self.session.name
            try:
                await self.session.close()
            except Exception as e:
                out.error = out.error or f"closing the session failed: {e}"
        return out
