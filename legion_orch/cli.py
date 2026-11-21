"""Command line: ``legion-orch serve -c orchestrator.yaml``.

Local commands (``serve``, ``check``, ``setup-openshell``, ``library list``) work on the config;
the others (including ``library promote``) talk to a
running controller's operator API through :class:`OperatorClient`.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Config, load_config


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="legion-orch")
    ap.add_argument("-c", "--config", default="orchestrator.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the controller, MCP endpoint, git remote and web UI")
    sub.add_parser("check", help="validate config and the assets folder")
    so = sub.add_parser("setup-openshell", help="one-time workspace setup: import the "
                        "controller's provider profiles (legion-mcp, legion-git, legion-git-push) "
                        "and create their providers")
    so.add_argument("--out", default="legion-provider-profiles",
                    help="directory for the rendered profiles (<id>.yaml)")
    so.add_argument("--apply", action="store_true", help="run the openshell commands (else print)")
    st = sub.add_parser("start", help="start a change on a running controller")
    st.add_argument("goal")
    st.add_argument("--workspace", "-w", required=True,
                    help="the workspace (repository) to make it in")
    sub.add_parser("changes", help="list changes on a running controller")
    sub.add_parser("attention", help="what needs a decision (gates, escalations) and the inbox "
                                     "(ended changes, retrospective reports)")
    dg = sub.add_parser("digest", help="what happened on a running controller since a time")
    dg.add_argument("--since", default="8h",
                    help="ISO timestamp or a duration back from now: 30m, 8h, 2d (default 8h)")
    tr = sub.add_parser("trace", help="a change's traceability matrix: requirements, tasks, "
                        "acceptance criteria and their results")
    tr.add_argument("change")
    tr.add_argument("--format", choices=["md", "json"], default="md")
    act = sub.add_parser("act", help="perform a human action on a running controller")
    act.add_argument("change")
    act.add_argument("node")
    act.add_argument("action")
    gov = sub.add_parser("govern", help="change-level governance: attest | override | "
                                        "approve_protected_paths | approve_surface")
    gov.add_argument("change")
    gov.add_argument("action")
    pn = sub.add_parser("prune", help="an ended change, once its integration branch is taken: "
                        "remove that branch and the change's evidence refs from the hub")
    pn.add_argument("change")
    pn.add_argument("--approver", required=True)
    lib = sub.add_parser("library", help="the environment library: list | promote an approved "
                                         "proposal into it")
    lsub = lib.add_subparsers(dest="library_cmd", required=True)
    lsub.add_parser("list", help="list the library's tools and network policies")
    pr = lsub.add_parser("promote", help="copy a tool or network policy proposed in a change's "
                                         "approved environment spec into the library")
    pr.add_argument("change")
    pr.add_argument("kind", choices=["tool", "policy"])
    pr.add_argument("name")
    pr.add_argument("--approver", required=True)
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

    def attention(self) -> dict:
        return self._call("/api/attention")

    def digest(self, since: str) -> dict:
        return self._call("/api/digest?" + urllib.parse.urlencode({"since": since}))

    def trace(self, change: str) -> dict:
        return self._call(f"/api/changes/{urllib.parse.quote(change)}/trace")

    def start(self, goal: str, workspace: str) -> dict:
        return self._call("/api/changes", {"goal": goal, "workspace": workspace})

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
        from .workflow.environment import Library
        lib = Library.for_config(cfg, a.schemas)
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

    from .sandbox import provider_profiles, setup_commands, setup_workspace
    from .sandbox.profile import profile_file
    from .server import MCP_TOOLS
    profiles = provider_profiles(cfg, list(MCP_TOOLS))
    out = Path(args.out)
    if args.apply:       # import or update each profile; create its provider if missing
        run = lambda cmd, probe: subprocess.run(cmd, capture_output=probe, text=True)
        return setup_workspace(cfg, profiles, out, run)
    out.mkdir(parents=True, exist_ok=True)
    for pid, profile in profiles.items():
        profile_file(out, pid).write_text(yaml.safe_dump(profile, sort_keys=False))
        print(f"wrote {profile_file(out, pid)}")
    for cmd in setup_commands(cfg, out):
        print("$", " ".join(cmd))
    print("(re-run with --apply to execute; it updates existing profiles and keeps existing "
          "providers, so it is safe to re-run after a change)")
    return 0


def cmd_changes(cfg: Config, args) -> int:
    for c in OperatorClient(cfg).changes():
        print(f"{c['id']}  {c['status']:<9} {c['tier'] or '--'}  "
              f"{c['accepted']}/{c['nodes']} accepted  {c['goal'][:60]}")
    return 0


def _print_items(title: str, items: list[dict], ui_url: str | None) -> None:
    print(f"{title} ({len(items)})")
    for i in items:
        link = f"  {ui_url.rstrip('/')}/#{i['change']}/overview/{i['node']}" \
            if ui_url and i.get("node") else ""
        print(f"  {i['since'][:16]}  {i['change']}  {i['title']}{link}")
        if i.get("reason"):
            print(f"      {i['reason']}")


def cmd_attention(cfg: Config, args) -> int:
    a = OperatorClient(cfg).attention()
    _print_items("Needs you", a["needs_you"], cfg.notify.ui_url)
    _print_items("Inbox", a["inbox"], cfg.notify.ui_url)
    return 0


def _since(raw: str) -> str:
    """An ISO timestamp as given, or ``<n>m|h|d`` back from now."""
    if raw and raw[:-1].isdigit() and raw[-1] in "mhd":
        delta = timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[raw[-1]]: int(raw[:-1])})
        return (datetime.now(UTC) - delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return raw


def cmd_digest(cfg: Config, args) -> int:
    d = OperatorClient(cfg).digest(_since(args.since))
    print(f"Since {d['since']}")
    for c in d["changes"]:
        counts = ", ".join(f"{n} {label}" for label, n in c["counts"].items()) or "no notable events"
        print(f"  {c['id']}  {c['status']:<9} {counts}  {c['goal'][:50]}")
    _print_items("Needs you", d["needs_you"], cfg.notify.ui_url)
    _print_items("New in the inbox", d["inbox"], cfg.notify.ui_url)
    return 0


def cmd_trace(cfg: Config, args) -> int:
    t = OperatorClient(cfg).trace(args.change)
    if args.format == "json":
        return _show(t)
    from .workflow.traceability import to_markdown
    print(to_markdown(t, args.change), end="")
    return 0


def cmd_start(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).start(args.goal, args.workspace))


def cmd_act(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).node_action(args.change, args.node, args.action,
                                                 args.approver, args.payload))


def cmd_govern(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).change_action(args.change, args.action, args.approver,
                                                   args.payload))


def cmd_prune(cfg: Config, args) -> int:
    return _show(OperatorClient(cfg).change_action(args.change, "prune", args.approver, {}))


def cmd_library(cfg: Config, args) -> int:
    if cfg.environment is None:
        print("no environment: section in the config", file=sys.stderr)
        return 1
    if args.library_cmd == "list":
        from .workflow.assets import Assets
        from .workflow.environment import Library
        print(Library.for_config(cfg, Assets(cfg.assets_dir).schemas).catalogue())
        return 0
    return _show(OperatorClient(cfg).change_action(
        args.change, "promote_proposal", args.approver, {"kind": args.kind, "name": args.name}))


def _show(result: Any) -> int:
    print(json.dumps(result, indent=2))
    return 0


COMMANDS = {"check": cmd_check, "serve": cmd_serve, "setup-openshell": cmd_setup_openshell,
            "changes": cmd_changes, "attention": cmd_attention, "digest": cmd_digest,
            "trace": cmd_trace, "prune": cmd_prune, "start": cmd_start, "act": cmd_act, "govern": cmd_govern,
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
