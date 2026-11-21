"""Out-of-process driver for the telemetry acceptance tests (protected support).

Reads ONE JSON scenario on stdin, prints ONE JSON result on stdout. It never imports legion_orch:
it starts the controller (``python -m legion_orch -c <cfg> serve``, local runner, the scripted
``tests/fake_agent.py``) as a separate process, drives it only through the operator HTTP API and
observes what it sends to a collector this driver runs (OTLP/HTTP protobuf on ``/v1/traces``).

Scenario fields (all optional except ``scenario``):
  scenario     full | sendback | reject | restart | wire | blocked
  telemetry    object written as the config's ``telemetry:`` section (``${COLLECTOR}`` is replaced
               by the collector's base URL); null = no section
  env          extra environment of the controller (``${COLLECTOR}`` replaced)
  collector    ok | refuse | hang | error          (default ok)
  goal, operator_token, project, needles, deadline_s (default 420)
  stop_at      wire only: "span" (wait for the finished `step workspace` span) | "h1" (change at H-1)
  block_sdk    blocked only: the controller's Python cannot import opentelemetry.sdk
  expect_exit  blocked only: wait for the controller to exit by itself (else it must serve)

Result: {"final": {"status", "nodes", "cycles"}, "requests": [{"path", "content_type", "bytes"}],
"resources": [...], "spans": [...], "raw_hits": {needle: bool}, "stderr_tail", "exit_code",
"exit_codes", "stop_s", "meta_ok", "decode_error", "cid", "error"?}.
"""
import gzip
import http.server
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPAN_WAIT_S = 90          # how long `wire` waits for a finished span before stopping anyway
ROOT_WAIT_S = 25          # how long a finished change is given to deliver its root span


class Failure(Exception):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------------------- collector
def _value(v):
    kind = v.WhichOneof("value")
    if kind is None:
        return None
    if kind in ("array_value", "kvlist_value", "bytes_value"):
        return str(getattr(v, kind))
    return getattr(v, kind)


def _attrs(kvs) -> dict:
    return {kv.key: _value(kv.value) for kv in kvs}


class Collector:
    """Stands in for otel-front's receiver on :4318."""

    def __init__(self, behaviour: str, needles: list[str]):
        self.behaviour, self.needles = behaviour, needles
        self.lock = threading.Lock()
        self.requests: list[dict] = []
        self.resources: list[dict] = []
        self.spans: list[dict] = []
        self.hits = {n: False for n in needles}
        self.process = 1
        self.last_request = 0.0
        self.decode_error = None
        self.closed = threading.Event()
        self.server = None
        if behaviour == "refuse":
            self.port = free_port()           # nothing listens there
            return
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.record(self.path, self.headers.get("Content-Type", ""), raw,
                             self.headers.get("Content-Encoding", ""))
                if outer.behaviour == "hang":
                    outer.closed.wait(30)
                    return
                if outer.behaviour == "error":
                    self.send_response(500)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                code = 200 if self.path == "/v1/traces" else 404
                self.send_response(code)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.closed.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def record(self, path, ctype, raw, encoding):
        body = raw
        try:
            if encoding == "gzip":
                body = gzip.decompress(raw)
        except Exception:
            body = raw
        with self.lock:
            self.last_request = time.monotonic()
            self.requests.append({"path": path, "content_type": ctype, "bytes": len(raw)})
            for needle in self.needles:
                nb = needle.encode()
                if nb in raw or nb in body:
                    self.hits[needle] = True
            try:
                from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
                    ExportTraceServiceRequest,
                )
                req = ExportTraceServiceRequest()
                req.ParseFromString(body)
            except Exception as e:                  # missing package, or not protobuf
                self.decode_error = f"{type(e).__name__}: {e}"
                return
            for rs in req.resource_spans:
                self.resources.append(_attrs(rs.resource.attributes))
                for ss in rs.scope_spans:
                    for sp in ss.spans:
                        self.spans.append({
                            "name": sp.name,
                            "trace_id": sp.trace_id.hex(),
                            "span_id": sp.span_id.hex(),
                            "parent_span_id": sp.parent_span_id.hex(),
                            "start": sp.start_time_unix_nano,
                            "end": sp.end_time_unix_nano,
                            "attributes": _attrs(sp.attributes),
                            "events": [{"name": e.name, "attributes": _attrs(e.attributes),
                                        "time": e.time_unix_nano} for e in sp.events],
                            "status_code": {0: "UNSET", 1: "OK", 2: "ERROR"}.get(sp.status.code,
                                                                                 str(sp.status.code)),
                            "status_description": sp.status.message,
                            "process": self.process,
                        })

    def span_names(self) -> list[str]:
        with self.lock:
            return [s["name"] for s in self.spans]

    def has(self, name_prefix: str) -> bool:
        return any(n.startswith(name_prefix) for n in self.span_names())

    def root_done(self) -> bool:
        """This process's change root arrived with the change's own end (not a shutdown)."""
        with self.lock:
            return any(s["name"].startswith("change ") and s["process"] == self.process
                       and s["attributes"].get("legion.outcome") in ("completed", "rejected", "abandoned")
                       for s in self.spans)

    def quiet(self, quiet_s=2.0, at_most=10.0):
        """Wait until no request has arrived for ``quiet_s`` (at most ``at_most``)."""
        end = time.monotonic() + at_most
        while time.monotonic() < end:
            with self.lock:
                last = self.last_request
            if last and time.monotonic() - last >= quiet_s:
                return
            if not last and time.monotonic() > end - at_most + quiet_s:
                return
            time.sleep(0.2)


# ------------------------------------------------------------------------------- the system
class Driver:
    def __init__(self, sc: dict):
        self.sc = sc
        self.deadline = time.monotonic() + float(sc.get("deadline_s", 420))
        self.tmp = Path(tempfile.mkdtemp(prefix="otel-driver-"))
        self.procs: list[subprocess.Popen] = []
        self.logs: list[Path] = []
        self.collector = Collector(sc.get("collector", "ok"), list(sc.get("needles", [])))
        self.token = sc.get("operator_token")
        self.port = free_port()
        self.cid = None
        self.final: dict = {}
        self.exit_codes: list[int] = []
        self.stop_s = None
        self.meta_ok = None

    # -- setup
    def sub(self, text):
        return text.replace("${COLLECTOR}", self.collector.url)

    def make_repo(self):
        path = self.tmp / "src-repo"
        path.mkdir()
        run = lambda *a: subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)
        run("init", "-q", "-b", "main")
        run("config", "user.email", "t@t")
        run("config", "user.name", "t")
        (path / "src").mkdir()
        (path / "src" / "greet.txt").write_text("hi\n")
        (path / "README.md").write_text("demo\n")
        run("add", "-A")
        run("commit", "-q", "-m", "init")

    def write_config(self) -> Path:
        fake = self.tmp / "claude"
        fake.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests/fake_agent.py'}\n")
        fake.chmod(0o755)
        server_extra = f", operator_token: {json.dumps(self.token)}" if self.token else ""
        text = textwrap.dedent(f"""
            project: {self.sc.get('project', 'otel-acceptance')}
            workspaces: [{{name: app, source: {self.tmp / 'src-repo'}, base_branch: main}}]
            decision_owners: [miles, ana]
            state_dir: {self.tmp / 'state'}
            assets_dir: {ROOT / 'assets'}
            workflow_doc: {ROOT / 'WORKFLOW.md'}
            server: {{host: 127.0.0.1, port: {self.port}, agent_host: 127.0.0.1, public_host: 127.0.0.1,
                      mcp_port: {free_port()}, git_port: {free_port()},
                      token_port: {free_port()}{server_extra}}}
            limits: {{max_parallel: 4, review_max_cycles: 3, critique_max_cycles: 2, run_timeout_s: 120}}
            retrospective: {{auto: false}}
            sandbox:
              runner: local
              templates_dir: {ROOT / 'policies'}
              agent_bin: "{fake}"
        """)
        if self.sc.get("telemetry") is not None:
            text += "telemetry: " + self.sub(json.dumps(self.sc["telemetry"])) + "\n"
        path = self.tmp / "orchestrator.yaml"
        path.write_text(text)
        return path

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
        env["PYTHONUNBUFFERED"] = "1"
        for k, v in (self.sc.get("env") or {}).items():
            env[k] = self.sub(v)
        return env

    def start_process(self, cfg: Path) -> subprocess.Popen:
        if self.sc.get("block_sdk"):
            code = ("import sys; sys.modules['opentelemetry.sdk'] = None; "
                    f"sys.argv = ['legion-orch', '-c', {str(cfg)!r}, 'serve']; "
                    "import legion_orch.cli as c; sys.exit(c.main())")
            cmd = [sys.executable, "-c", code]
        else:
            cmd = [sys.executable, "-m", "legion_orch", "-c", str(cfg), "serve"]
        log = self.tmp / f"controller-{len(self.procs) + 1}.log"
        self.logs.append(log)
        p = subprocess.Popen(cmd, cwd=ROOT, env=self.env(), stdout=log.open("w"),
                             stderr=subprocess.STDOUT, start_new_session=True)
        self.procs.append(p)
        return p

    def stderr_tail(self, n=4000) -> str:
        out = ""
        for log in self.logs:
            try:
                out += log.read_text(errors="replace")[-n:]
            except OSError:
                pass
        return out[-n:]

    # -- waiting and the operator API
    def check_time(self):
        if time.monotonic() > self.deadline:
            raise Failure("deadline")

    def wait(self, pred, what, proc=None, timeout=None):
        end = time.monotonic() + timeout if timeout else None
        while True:
            self.check_time()
            try:
                got = pred()
            except (urllib.error.URLError, ConnectionError, OSError, KeyError, ValueError):
                got = None
            if got:
                return got
            if proc is not None and proc.poll() is not None:
                raise Failure(f"controller exited ({proc.returncode}) while waiting for {what}")
            if end and time.monotonic() > end:
                return None
            time.sleep(0.4)

    def api(self, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Operator-Token"] = self.token
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise Failure(f"{path}: HTTP {e.code} {e.read().decode(errors='replace')[:300]}") from None

    def state(self) -> dict:
        return self.api(f"/api/changes/{self.cid}/state")

    def node_status(self, node) -> str | None:
        for n in self.state()["nodes"]:
            if n["id"] == node:
                return n["status"]
        return None

    def action(self, node, action, approver, payload=None):
        return self.api(f"/api/changes/{self.cid}/nodes/{node}/action",
                        {"action": action, "approver": approver, "payload": payload or {}})

    def await_serving(self, proc):
        self.wait(lambda: self.api("/api/meta"), "the operator API", proc)

    def snapshot_final(self):
        st = self.state()
        self.final = {"status": (st.get("change") or {}).get("status"),
                      "nodes": {n["id"]: n["status"] for n in st["nodes"]},
                      "cycles": {n["id"]: n.get("cycle") for n in st["nodes"]}}

    def stop(self, proc):
        """SIGTERM (a graceful stop), then the exit code; the collector goes quiet after."""
        if proc.poll() is None:
            t0 = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            self.stop_s = round(time.monotonic() - t0, 2)
        self.exit_codes.append(proc.returncode)
        if self.sc.get("collector", "ok") == "ok":
            self.collector.quiet()

    # -- the workflow, as tests/test_e2e.py::_drive does it
    def drive(self, proc, *, to="ready", send_back=False, reject=False, start_at="H-1"):
        wait = lambda node, status, **kw: self.wait(lambda: self.node_status(node) == status,
                                                    f"{node} {status}", proc, **kw)
        if start_at == "H-1":
            wait("H-1", "AWAITING_HUMAN")
            if to == "h1":
                return
            self.action("H-1", "accept", "miles")
        wait("H-E1", "AWAITING_HUMAN")
        if send_back:
            self.action("H-E1", "send_back", "miles",
                        {"stage": "plan", "feedback": "please expand the plan once more"})
            wait("H-E1", "AWAITING_HUMAN")
        self.action("H-E1", "approve", "miles", {"subjects": ["plan", "REQ-1"]})
        self.action("H-E1", "approve", "ana", {"subjects": ["REQ-2"]})
        wait("H-2", "AWAITING_HUMAN")
        self.action("H-2", "approve", "ana", {"subjects": ["oracle"]})
        wait("H-3", "AWAITING_HUMAN")
        if reject:
            self.action("H-3", "reject", "miles", {"reason": "not good enough", "retrospective": False})
            self.wait(lambda: self.state()["change"]["status"] == "ABANDONED", "the change abandoned", proc)
        else:
            self.action("H-3", "accept", "miles")
            self.wait(lambda: self.state()["change"]["status"] == "READY", "the change READY", proc)
        self.snapshot_final()
        # the root span is exported when the change ends: give it a moment before the stop
        if self.sc.get("collector", "ok") == "ok" and self.sc.get("telemetry") is not None:
            self.wait(self.collector.root_done, "the root span", proc, timeout=ROOT_WAIT_S)

    # -- scenarios
    def run(self):
        sc = self.sc["scenario"]
        self.make_repo()
        cfg = self.write_config()
        proc = self.start_process(cfg)
        if sc == "blocked":
            return self.blocked(proc)
        self.await_serving(proc)
        goal = self.sc.get("goal") or "Say hello and goodbye"
        self.cid = self.api("/api/changes", {"goal": goal, "workspace": "app"})["change"]
        if sc == "wire":
            if self.sc.get("stop_at", "span") == "h1":
                self.drive(proc, to="h1")
            else:
                self.wait(lambda: self.collector.has("step workspace"), "the finished step workspace span",
                          proc, timeout=SPAN_WAIT_S)
            self.stop(proc)
        elif sc in ("full", "sendback", "reject"):
            self.drive(proc, send_back=sc == "sendback", reject=sc == "reject")
            self.stop(proc)
        elif sc == "restart":
            self.drive(proc, to="h1")
            self.stop(proc)
            self.collector.process = 2
            proc2 = self.start_process(cfg)
            self.await_serving(proc2)
            self.drive(proc2, start_at="H-1")
            self.stop(proc2)
        else:
            raise Failure(f"unknown scenario {sc}")

    def blocked(self, proc):
        if self.sc.get("expect_exit"):
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                raise Failure("the controller kept running") from None
            self.exit_codes.append(proc.returncode)
            return
        self.await_serving(proc)
        self.meta_ok = True
        time.sleep(float(self.sc.get("settle_s", 4)))
        self.stop(proc)

    def result(self, error=None) -> dict:
        c = self.collector
        with c.lock:
            out = {"final": self.final, "requests": list(c.requests), "resources": list(c.resources),
                   "spans": list(c.spans), "raw_hits": dict(c.hits), "stderr_tail": self.stderr_tail(),
                   "exit_code": self.exit_codes[-1] if self.exit_codes else None,
                   "exit_codes": self.exit_codes, "stop_s": self.stop_s, "meta_ok": self.meta_ok,
                   "decode_error": c.decode_error, "cid": self.cid}
        if error:
            out["error"] = error
        return out

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                p.wait()
        self.collector.close()
        subprocess.run(["rm", "-rf", str(self.tmp)], check=False)


def main() -> int:
    sc = json.loads(sys.stdin.read())
    d = Driver(sc)
    error = None
    try:
        d.run()
    except Failure as e:
        error = str(e)
    except Exception as e:                      # a driver bug is reported, never swallowed
        error = f"driver error: {type(e).__name__}: {e}"
    finally:
        result = d.result(error)
        d.cleanup()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
