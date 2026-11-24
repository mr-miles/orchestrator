"""One more turn of an agent's recorded session, in its own sandbox: what a reflection (before
the sandbox is deleted) and an answer to another agent (A2A, while the step has no live run)
are made of. The turn gets only the Skill tool and no MCP server: it answers from the session,
its prompt and its skills, and changes nothing."""
from __future__ import annotations

import asyncio
import json
import shutil
from typing import TYPE_CHECKING

from ....config import agent_command
from ....sandbox.scripts import write_payload
from ....util import stop_process
from ...model import Run
from ..base.common import model_env
from ..base.core import ChangeCore

if TYPE_CHECKING:
    from .sandboxes import Sandboxes


async def resumed_turn(c: ChangeCore, sandboxes: Sandboxes, sandbox: str, run: Run, turn_id: str,
                       prompt: str, *, timeout: float, wake: bool) -> tuple[int, str]:
    """Resume ``run``'s session in ``sandbox`` for one turn on ``prompt`` (``wake``: start the
    stopped sandbox first). Returns the agent's exit code and log; raises on a timeout or a
    sandbox that cannot be used. The payload is removed and the process stopped however it ends."""
    d = c.run_dir(turn_id)
    log_file = d / "agent.log"
    # No skills in this payload: the sandbox still has those of its last run, and run-agent.sh
    # keeps installed skills when a payload brings none.
    payload = write_payload(d / "payload", {
        "LEGION_RUN_ID": turn_id, "LEGION_ROLE": run.role, "LEGION_NEEDS_REPO": "0",
        "LEGION_AGENT_COMMAND": agent_command(c.cfg.sandbox, tools=False),
        "LEGION_RESUME_SESSION": run.session_id, "LEGION_REQUIRE_SESSION": "1"} | model_env(run),
        prompt=prompt, soul=c.assets.soul_text(run.soul), mcp=json.dumps({"mcpServers": {}}))
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
