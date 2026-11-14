"""Command line: ``legion-orch serve -c orchestrator.yaml``."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.request

from .assets import Assets
from .config import load_config


def main(argv: list[str] | None = None) -> int:
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
    act.add_argument("--approver", required=True)
    act.add_argument("--payload", default="{}", help="JSON payload")
    gov = sub.add_parser("govern", help="change-level governance: attest | override | "
                                        "approve_protected_paths | approve_surface")
    gov.add_argument("change")
    gov.add_argument("action")
    gov.add_argument("--approver", required=True)
    gov.add_argument("--payload", default="{}", help="JSON payload")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "check":
        a = Assets(cfg.assets_dir)
        print(f"assets {cfg.assets_dir} {a.sha}: roles={sorted(a.roles)} souls={a.souls()}")
        return 0
    if args.cmd == "serve":
        import asyncio
        from .engine import Controller
        from .server import serve
        asyncio.run(serve(Controller(cfg)))
        return 0
    if args.cmd == "setup-openshell":
        return setup_openshell(cfg, args)

    base = f"http://127.0.0.1:{cfg.server.port}"
    headers = {"Content-Type": "application/json"}
    if cfg.server.operator_token:
        headers["X-Operator-Token"] = cfg.server.operator_token
    if args.cmd == "changes":
        with urllib.request.urlopen(f"{base}/api/changes") as r:
            for c in json.loads(r.read()):
                print(f"{c['id']}  {c['status']:<9} {c['tier'] or '--'}  "
                      f"{c['accepted']}/{c['nodes']} accepted  {c['goal'][:60]}")
        return 0
    if args.cmd == "start":
        url, body = f"{base}/api/changes", {"goal": args.goal}
    elif args.cmd == "govern":
        url, body = f"{base}/api/changes/{args.change}/action", {
            "action": args.action, "approver": args.approver, "payload": json.loads(args.payload)}
    else:
        url, body = f"{base}/api/changes/{args.change}/nodes/{args.node}/action", {
            "action": args.action, "approver": args.approver, "payload": json.loads(args.payload)}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            print(r.read().decode())
            return 0
    except urllib.error.HTTPError as e:
        print(e.read().decode(), file=sys.stderr)
        return 1


def setup_openshell(cfg, args) -> int:
    import subprocess
    import yaml
    from .sandbox import provider_profile, setup_commands
    from .server import MCP_TOOLS
    with open(args.out, "w") as f:
        yaml.safe_dump(provider_profile(cfg, list(MCP_TOOLS)), f, sort_keys=False)
    print(f"wrote {args.out}")
    for cmd in setup_commands(cfg, args.out):
        print("$", " ".join(cmd))
        if args.apply:
            r = subprocess.run(cmd)
            if r.returncode != 0:
                return r.returncode
    if not args.apply:
        print("(re-run with --apply to execute; this is done once per workspace, not per sandbox)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
