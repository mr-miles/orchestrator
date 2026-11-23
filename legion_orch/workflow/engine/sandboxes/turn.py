"""One more turn of an agent's recorded session, in its own sandbox: what a reflection (before
the sandbox is deleted) and an answer to another agent (A2A, while the step has no live run)
are made of. The turn gets only the Skill tool and no MCP server: it answers from the session,
its prompt and its skills, and changes nothing."""
from __future__ import annotations

import asyncio
import json
import shlex
import shutil
from typing import TYPE_CHECKING

from ....config import agent_command
from ....sandbox import RUN_AGENT_SH
from ....util import stop_process
from ...model import Run
from ..base.core import ChangeCore

if TYPE_CHECKING:
    from .sandboxes import Sandboxes


async def resumed_turn(c: ChangeCore, sandboxes: Sandboxes, sandbox: str, run: Run, turn_id: str,
                       prompt: str, *, timeout: float, wake: bool) -> tuple[int, str]:
    """Resume ``run``'s session in ``sandbox`` for one turn on ``prompt`` (``wake``: start the
    stopped sandbox first). Returns the agent's exit code and log; raises on a timeout or a
    sandbox that cannot be used. The payload is removed and the process stopped however it ends."""
    soul = c.assets.soul(run.soul) if run.soul in c.assets.souls_ else ""
    d = c.run_dir(turn_id)
    payload, log_file = d / "payload", d / "agent.log"
    payload.mkdir(parents=True, exist_ok=True)
    (payload / "prompt.md").write_text(prompt)
    (payload / "soul.md").write_text(soul)
    (payload / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
    # No skills in this payload: the sandbox still has those of its last run, and run-agent.sh
    # keeps installed skills when a payload brings none.
    env = {"LEGION_RUN_ID": turn_id, "LEGION_ROLE": run.role, "LEGION_NEEDS_REPO": "0",
           "LEGION_AGENT_COMMAND": agent_command(c.cfg.sandbox, tools=False),
           "LEGION_RESUME_SESSION": run.session_id, "LEGION_REQUIRE_SESSION": "1"}
    (payload / "run.env").write_text("".join(f"{k}={shlex.quote(str(v))}\n"
                                             for k, v in env.items()))
    (payload / "run-agent.sh").write_text(RUN_AGENT_SH)
    proc = None
    try:
        if wake:
            await sandboxes.wake(sandbox)
        await sandboxes.upload(sandbox, payload)
        proc = await sandboxes.start_agent(sandbox, log_file)
        rc = await asyncio.wait_for(proc.wait(), timeout)
        return rc, log_file.read_text(errors="replace") if log_file.exists() else ""
    finally:                                    # also when cancelled (at shutdown)
        if proc is not None:
            await stop_process(proc)
        shutil.rmtree(d, ignore_errors=True)
