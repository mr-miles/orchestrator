"""Command line: ``legion-orch serve -c orchestrator.yaml``.

Local commands (``serve``, ``check``, ``setup-openshell``, ``library``) work on the config (and,
for ``library promote``, read the controller's store); the others talk to a
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
    lib = sub.add_parser("library", help="the environment library: list | promote an approved "
                                         "proposal into it")
    lsub = lib.add_subparsers(dest="library_cmd", required=True)
    lsub.add_parser("list", help="list the library's tools and network policies")
    pr = lsub.add_parser("promote", help="copy a tool or network policy proposed in a change's "
                                         "approved environment spec into the library")
    pr.add_argument("change")
    pr.add_argument("kind", choices=["tool", "policy"])
    pr.add_argument("name")
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
    if cfg.environment is not None:
        from .workflow.environment import load_library
        lib = load_library(cfg.resolve(cfg.environment.library_dir),
                           a.schemas.get("environment").body)
        print(f"environment library {lib.root}: tools={sorted(lib.tools)} "
              f"policies={sorted(lib.policies)}")
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


def _library_schema(cfg: Config) -> dict:
    from .workflow.assets import Assets
    return Assets(cfg.assets_dir).schemas.get("environment").body


def cmd_library(cfg: Config, args) -> int:
    from .workflow.environment import load_library
    if cfg.environment is None:
        print("no environment: section in the config", file=sys.stderr)
        return 1
    root = cfg.resolve(cfg.environment.library_dir)
    schema = _library_schema(cfg)
    if args.library_cmd == "list":
        print(load_library(root, schema).catalogue())
        return 0
    return promote(cfg, root, schema, args.change, args.kind, args.name)


def promote(cfg: Config, root, schema: dict, change: str, kind: str, name: str) -> int:
    """Copy a proposal from the change's *approved* environment spec (the version H-E1 pinned)
    into the library. Reads the controller's store; never overwrites a library entry."""
    import sqlite3
    import threading

    from .store import DocStore
    from .workflow.assets import Assets
    from .workflow.environment import entry_yaml, load_library, proposal
    db = cfg.state_dir / "state.db"
    if not db.is_file():
        print(f"no controller store at {db}", file=sys.stderr)
        return 1
    docs = DocStore(sqlite3.connect(f"file:{db}?mode=ro", uri=True), threading.RLock(),
                    Assets(cfg.assets_dir).schemas)
    rec = f"changes/{change}/control/change.json"
    if docs.latest_version(rec) is None:
        print(f"unknown change {change}", file=sys.stderr)
        return 1
    appr = (json.loads(docs.read(rec).content).get("approvals") or {}).get("H-E1")
    pins = (appr or {}).get("pins") or {}
    path = f"changes/{change}/plan/environment.json"
    if path not in pins:
        print(f"{change}: the environment spec is not approved (H-E1)", file=sys.stderr)
        return 1
    spec = json.loads(docs.read(path, pins[path]).content)
    plural = "tools" if kind == "tool" else "policies"
    body = proposal(spec, plural, name)
    if body is None:
        print(f"file:{path}@{pins[path]} proposes no {kind} {name!r}", file=sys.stderr)
        return 1
    lib = load_library(root, schema)
    if name in lib.entries(plural):
        print(f"the library already has {kind} {name!r}; not overwritten", file=sys.stderr)
        return 1
    dest = root / plural / f"{name}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(f"# Promoted from file:{path}@{pins[path]} (approved by {appr['approver']})\n"
                    + entry_yaml(body))
    load_library(root, schema)          # still valid as a whole
    print(f"wrote {dest}")
    return 0


def _show(result: Any) -> int:
    print(json.dumps(result, indent=2))
    return 0


COMMANDS = {"check": cmd_check, "serve": cmd_serve, "setup-openshell": cmd_setup_openshell,
            "changes": cmd_changes, "start": cmd_start, "act": cmd_act, "govern": cmd_govern,
            "library": cmd_library}


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
