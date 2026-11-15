"""Command line: ``legion-orch serve -c orchestrator.yaml``.

Local commands (``serve``, ``check``, ``setup-openshell``) work on the config; the others talk to a
running controller's operator API through :class:`OperatorClient`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import Config, load_config


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="legion-orch")
    ap.add_argument("-c", "--config", default="orchestrator.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the controller, MCP endpoint, git remote and web UI")
    sub.add_parser("check", help="validate config and the assets folder")
    so = sub.add_parser("setup-openshell", help="one-time workspace setup: import the "
                        "legion-controller provider profile and create the provider")
    so.add_argument("--out", default="legion-provider-profile.yaml")
    so.add_argument("--apply", action="store_true", help="run the openshell commands (else print)")
    st = sub.add_parser("start", help="start a change on a running controller")
    st.add_argument("goal")
    sub.add_parser("changes", help="list changes on a running controller")
    act = sub.add_parser("act", help="perform a human action on a running controller")
    act.add_argument("change")
    act.add_argument("node")
    act.add_argument("action")
    gov = sub.add_parser("govern", help="change-level governance: attest | override | "
                                        "approve_protected_paths | approve_surface")
    gov.add_argument("change")
    gov.add_argument("action")
    for p in (act, gov):
        p.add_argument("--approver", required=True)
        p.add_argument("--payload", default="{}", type=json.loads, help="JSON payload")
    return ap


class OperatorError(Exception):
    pass


class OperatorClient:
    """The operator API of a running controller (sends the operator token when configured)."""

    def __init__(self, cfg: Config):
        self.base = f"http://127.0.0.1:{cfg.server.port}"
        self.headers = {"Content-Type": "application/json"}
        if cfg.server.operator_token:
            self.headers["X-Operator-Token"] = cfg.server.operator_token

    def _call(self, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=self.headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise OperatorError(e.read().decode()) from None

    def changes(self) -> list[dict]:
        return self._call("/api/changes")

    def start(self, goal: str) -> dict:
        return self._call("/api/changes", {"goal": goal})

    def node_action(self, change: str, node: str, action: str, approver: str, payload: dict) -> dict:
        return self._call(f"/api/changes/{change}/nodes/{node}/action",
                          {"action": action, "approver": approver, "payload": payload})

    def change_action(self, change: str, action: str, approver: str, payload: dict) -> dict:
        return self._call(f"/api/changes/{change}/action",
                          {"action": action, "approver": approver, "payload": payload})


# ---------------------------------------------------------------------------- commands
def cmd_check(cfg: Config, args) -> int:
    from .workflow.assets import Assets
    a = Assets(cfg.assets_dir)
    print(f"assets {cfg.assets_dir} {a.sha}: roles={sorted(a.roles)} souls={a.souls()}")
    return 0


def cmd_serve(cfg: Config, args) -> int:
    import asyncio

    from .server import serve
    from .workflow.engine import Controller
    asyncio.run(serve(Controller(cfg)))
    return 0


def cmd_setup_openshell(cfg: Config, args) -> int:
    import subprocess
    from pathlib import Path

    import yaml

    from .sandbox import provider_profile, setup_commands, setup_workspace
    from .server import MCP_TOOLS
    profile = provider_profile(cfg, list(MCP_TOOLS))
    if args.apply:       # import or update the profile; create the provider if missing
        run = lambda cmd, probe: subprocess.run(cmd, capture_output=probe, text=True)
        return setup_workspace(cfg, profile, Path(args.out), run)
    with open(args.out, "w") as f:
        yaml.safe_dump(profile, f, sort_keys=False)
    print(f"wrote {args.out}")
    for cmd in setup_commands(cfg, args.out):
        print("$", " ".join(cmd))
    print("(re-run with --apply to execute; it updates an existing profile and keeps an existing "
          "provider, so it is safe to re-run after a change)")
    return 0


def cmd_changes(cfg: Config, args) -> int:
    for c in OperatorClient(cfg).changes():
        print(f"{c['id']}  {c['status']:<9} {c['tier'] or '--'}  "
              f"{c['accepted']}/{c['nodes']} accepted  {c['goal'][:60]}")
    return 0


def cmd_start(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).start(args.goal))


def cmd_act(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).node_action(args.change, args.node, args.action,
                                                 args.approver, args.payload))


def cmd_govern(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).change_action(args.change, args.action, args.approver,
                                                   args.payload))


def _show(result: Any) -> int:
    print(json.dumps(result, indent=2))
    return 0


COMMANDS = {"check": cmd_check, "serve": cmd_serve, "setup-openshell": cmd_setup_openshell,
            "changes": cmd_changes, "start": cmd_start, "act": cmd_act, "govern": cmd_govern}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        return COMMANDS[args.cmd](cfg, args)
    except OperatorError as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
