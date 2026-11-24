"""Out-of-process driver for the design-review acceptance tests (protected support).

Reads ONE JSON scenario on stdin, prints ONE JSON result on stdout. It never imports legion_orch:
it starts the controller (``python -m legion_orch -c <cfg> serve``, local runner, an agent that
does nothing) as a separate process and drives it only through the operator HTTP API.

Scenario: {"creates": [{"workflow": "design-review"} | {} | {"workflow": "no-such"}, ...],
           "restart": bool, "deadline_s": 120}

Result: {"meta", "creates": [{"body", "http", "change", "tickets", "workflow", "state_workflow"}],
"changes_before", "changes_after", "restarted": [{"change", "tickets", "workflow"}], "error"?}
``tickets`` is the ordered list of ticket ids of the change's state.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


class Failure(Exception):
    pass


def free_ports(n: int) -> list[int]:
    socks = [socket.socket() for _ in range(n)]
    try:
        for s in socks:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


class Driver:
    def __init__(self, sc: dict):
        self.sc = sc
        self.deadline = time.monotonic() + float(sc.get("deadline_s", 120))
        self.tmp = Path(tempfile.mkdtemp(prefix="design-driver-"))
        self.procs: list[subprocess.Popen] = []
        self.logs: list[Path] = []
        self.port = None

    def make_repo(self):
        path = self.tmp / "src-repo"
        path.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (path / "README.md").write_text("demo\n")
        run("add", "-A")
        run("commit", "-q", "-m", "init")

    def write_config(self) -> Path:
        idle = self.tmp / "claude"
        idle.write_text("#!/bin/sh\nexec sleep 600\n")        # an agent that never reports
        idle.chmod(0o755)
        self.port, mcp, git, token, a2a = free_ports(5)
        text = textwrap.dedent(f"""
            project: design-acceptance
            workspaces: [{{name: app, source: {self.tmp / 'src-repo'}, base_branch: main}}]
            decision_owners: [miles]
            state_dir: {self.tmp / 'state'}
            assets_dir: {ROOT / 'assets'}
            workflow_doc: {ROOT / 'WORKFLOW.md'}
            server: {{host: 127.0.0.1, port: {self.port}, agent_host: 127.0.0.1, public_host: 127.0.0.1,
                      mcp_port: {mcp}, git_port: {git}, token_port: {token}, a2a_port: {a2a}}}
            limits: {{max_parallel: 4, run_timeout_s: 120}}
            retrospective: {{auto: false}}
            sandbox:
              runner: local
              templates_dir: {ROOT / 'policies'}
              agent_bin: "{idle}"
        """)
        path = self.tmp / "orchestrator.yaml"
        path.write_text(text)
        return path

    def start_process(self, cfg: Path) -> subprocess.Popen:
        log = self.tmp / f"controller-{len(self.procs) + 1}.log"
        self.logs.append(log)
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        p = subprocess.Popen([sys.executable, "-m", "legion_orch", "-c", str(cfg), "serve"], cwd=ROOT,
                             env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def tail(self, n=3000) -> str:
        out = ""
        for log in self.logs:
            try:
                out += log.read_text(errors="replace")[-n:]
            except OSError:
                pass
        return out[-n:]

    def api(self, path, body=None, want_error=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=(
            "POST" if body is not None else "GET"), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read().decode(errors="replace")
            if not want_error:
                raise Failure(f"{path}: HTTP {e.code} {raw[:300]}") from None
            return e.code, raw

    def await_serving(self, proc):
        end = time.monotonic() + 60
        while time.monotonic() < end and time.monotonic() < self.deadline:
            if proc.poll() is not None:
                raise Failure(f"controller exited ({proc.returncode}): {self.tail()}")
            try:
                self.api("/api/meta")
                return
            except (urllib.error.URLError, ConnectionError, OSError, Failure):
                time.sleep(0.3)
        raise Failure("the controller did not serve")

    def serve(self):
        for _ in range(3):
            cfg = self.write_config()
            proc = self.start_process(cfg)
            try:
                self.await_serving(proc)
                return cfg, proc
            except Failure:
                if "address already in use" not in self.tail().lower():
                    raise
        raise Failure("no free ports")

    def stop(self, proc):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                proc.wait()

    def change_ids(self) -> list[str]:
        _, rows = self.api("/api/changes")
        rows = rows.get("changes", rows) if isinstance(rows, dict) else rows
        return sorted(r.get("id") or r.get("change") for r in rows)

    def snapshot(self, cid) -> dict:
        _, st = self.api(f"/api/changes/{cid}/state")
        ch = st.get("change") or {}
        return {"tickets": [t["id"] for t in st["tickets"]], "workflow": ch.get("workflow")}

    def run(self) -> dict:
        self.make_repo()
        cfg, proc = self.serve()
        out: dict = {"creates": []}
        _, out["meta"] = self.api("/api/meta")
        out["changes_before"] = self.change_ids()
        for body in self.sc.get("creates", []):
            payload = {"goal": "Review the design", "workspace": "app", **body}
            code, res = self.api("/api/changes", payload, want_error=True)
            item = {"body": body, "http": code}
            if code == 200 and isinstance(res, dict):
                item["change"] = res["change"]
                item |= self.snapshot(res["change"])
            out["creates"].append(item)
        out["changes_after"] = self.change_ids()
        if self.sc.get("restart"):
            self.stop(proc)
            proc = self.start_process(cfg)
            self.await_serving(proc)
            out["restarted"] = [{"change": c["change"], **self.snapshot(c["change"])}
                                for c in out["creates"] if c.get("change")]
        self.stop(proc)
        return out

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.wait()
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)


def main() -> int:
    d = Driver(json.loads(sys.stdin.read()))
    result: dict = {}
    try:
        result = d.run()
    except Failure as e:
        result = {"error": str(e)}
    except Exception as e:
        result = {"error": f"driver error: {type(e).__name__}: {e}"}
    finally:
        d.cleanup()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
