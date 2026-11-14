"""Sandbox names. The OpenShell gateway accepts a name only if it is a DNS label of at most 19
characters without ``--`` (MAX_ROUTABLE_NAME_LEN; NVIDIA/OpenShell#2651), and names are unique
per gateway, so they must not collide across changes or controller state directories."""
from __future__ import annotations

import hashlib
import re
import secrets

MAX_NAME = 19
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def check_name(name: str) -> str:
    """Refuse a name the gateway would refuse – here, before anything is created."""
    if len(name) > MAX_NAME or not _LABEL.match(name) or "--" in name:
        raise ValueError(f"sandbox name {name!r} is not a DNS label of at most {MAX_NAME} "
                           "characters without '--' (OpenShell gateway limit)")
    return name


def agent_sandbox_name(change_id: str, run_id: str) -> str:
    """``lg-<run>-<hash>``: the run stays readable; the hash of change and run makes it unique."""
    room = MAX_NAME - len("lg--") - len(run_id)
    if room < 6:
        raise ValueError(f"run id {run_id!r} too long for a sandbox name")
    digest = hashlib.sha256(f"{change_id}/{run_id}".encode()).hexdigest()[:room]
    return check_name(f"lg-{run_id.lower()}-{digest}")


def verifier_sandbox_name() -> str:
    """``lg-v<14 hex>``: a fresh name for every verification."""
    return check_name(f"lg-v{secrets.token_hex(7)}")
