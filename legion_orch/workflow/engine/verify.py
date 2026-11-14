"""Deterministic verification, accept and merge.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import secrets
import shlex
import shutil
from typing import Any

from ...sandbox import (
    VERIFY_SETUP_SH,
    SandboxError,
    SandboxSpec,
)
from ...util import now_iso
from ..model import (
    ACCEPTED,
    BUILD,
    INTEGRATION,
    MERGING,
    READY,
    REVIEWING,
    REWORK,
    VERIFYING,
)
from .common import _slug


class VerifyMixin:
    async def _verify(self, label: str, sha: str, commands: list[str]) -> dict:
        """G-V1: run commands at ``sha`` in a fresh sandbox. Never raises."""
        name = f"lg-{self.change.id}-v{secrets.token_hex(3)}".lower()
        base = self.ctl.tmp / "verify" / name
        payload = base / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "run.env").write_text(
            f"LEGION_GIT_URL={shlex.quote(self.git_url)}\n")
        (payload / "verify-setup.sh").write_text(VERIFY_SETUP_SH)
        result: dict[str, Any] = {"sha": sha, "sandbox": name, "results": [], "ok": False,
                                  "infra_error": None, "at": now_iso()}
        created, ref = False, None
        try:
            policy = self._policy(base / "policy.yaml", agent=False, push=False)
            ref = await self.runner.create(SandboxSpec(
                name=name, payload_dir=payload, policy_file=policy, agent=False,
                change=self.cid, task=label, role="verifier", run=name))
            self.ctl.verifier_refs[ref] = {"id": f"verifier:{name}", "change": self.cid,
                                           "task": label}         # read-only git grant
            created = True
            rc, out = await self.runner.exec(name, ["bash", "/sandbox/payload/verify-setup.sh", sha],
                                             timeout=600)
            if rc != 0:
                raise SandboxError(f"verifier setup failed ({rc}): {out[-1500:]}")
            ok = True
            for cmd in commands:
                rc, out = await self.runner.exec(name, ["bash", "-lc", cmd], workdir="/sandbox/work",
                                                 timeout=self.cfg.limits.verify_timeout_s)
                result["results"].append({"command": cmd, "exit_code": rc, "output": out[-4000:]})
                ok = ok and rc == 0
            result["ok"] = ok and bool(commands)
            if not commands:
                result["infra_error"] = "no commands to verify"
        except Exception as e:
            result["infra_error"] = str(e)
        finally:
            if ref:
                self.ctl.verifier_refs.pop(ref, None)
            shutil.rmtree(base, ignore_errors=True)
            if created:
                try:
                    await self.runner.delete(name)
                except Exception as e:
                    self._event("sandbox", f"verifier delete failed: {e}")
        return result

    async def _verify_node(self, node_id: str) -> None:
        node = self.nodes[node_id]
        if node.kind == BUILD:
            commands = node.task["verification"]
        else:
            commands = self._oracle_commands()
        self._event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                    f"({len(commands)} command(s))", node_id)
        res = await self._verify(node_id, node.candidate_sha, commands)
        async with self.lock:
            if node.status != VERIFYING:
                return
            att = self.write_record(f"tasks/{node.task['id']}/verification.json" if node.kind == BUILD
                              else f"control/verification/{_slug(node_id)}.json", res).attestation
            res["attestation"] = att
            key = "integration" if node.kind == INTEGRATION else str(node.cycle)
            node.verification[key] = {k: res[k] for k in ("ok", "sha", "attestation", "infra_error")} \
                | {"results": [{"command": r["command"], "exit_code": r["exit_code"]}
                               for r in res["results"]]}
            if res["infra_error"]:
                self._escalate(node, "verify_infra", res["infra_error"], resume=VERIFYING)
            elif not res["ok"]:
                fails = [r for r in res["results"] if r["exit_code"] != 0]
                if node.kind == BUILD:
                    self._rework(node, [{"cycle": node.cycle, "source": "verification",
                                         "text": f"BLOCKER: `{r['command']}` exited {r['exit_code']}: "
                                                 f"{r['output'][-800:]}", "attestations": [att]}
                                        for r in fails], "verification")
                else:
                    self._escalate(node, "integration_failed",
                                   f"G-V1: {len(fails)} oracle command(s) failed at head", resume=READY)
            elif node.kind == BUILD:
                if self.change.panel:
                    node.status = REVIEWING
                    self._event("review", f"verification passed; panel {self.change.panel}", node_id)
                else:
                    node.status = MERGING
                    self._spawn(self._merge_node(node_id))
            else:  # integration
                node.status = ACCEPTED
                self._accept_runs(node)
            self._save()

    async def _merge_node(self, node_id: str) -> None:
        async with self.lock:
            node = self.nodes[node_id]
            sha = node.candidate_sha
            ver = node.verification.get(str(node.cycle))
            override = (node.human or {}).get("override")
            v = []
            if not ver or not ver["ok"] or ver["sha"] != sha:
                v.append("G-A1: no passing deterministic verification at candidate")
            if not override:
                for role in self.change.panel:
                    r = node.reviews.get(f"{node.cycle}:{role}")
                    if not r or r["verdict"] != "PASS" or r["sha"] != sha:
                        v.append(f"G-A1: no {role} PASS at candidate")
            if v:
                self._escalate(node, "accept_guard", "; ".join(v), resume=REWORK)
                self._save()
                return
            ok, out = await self.git.merge(self.cid, sha, f"legion {self.change.id}: {node.title} "
                                                f"(cycle {node.cycle})")
            if not ok:
                head = await self.git.integration_head(self.change.id)
                self._rework(node, [{"cycle": node.cycle, "source": "merge",
                                     "text": f"G-A2: merge conflict with integration; rebase onto {head} "
                                             f"and force-push your branch. Details: {out[-600:]}",
                                     "attestations": [f"git:{self.cfg.repo.name}@{head}"]}], "merge")
                if node.status == REWORK:
                    node.base_sha = head
                self._save()
                return
            self.change.integration_head = out
            node.accepted_sha = sha
            node.status = ACCEPTED
            self._event("accepted", f"merged {sha[:12]} -> integration {out[:12]}", node_id)
            self._accept_runs(node)  # deletes the implementer sandbox (principle 8)
            self._save()
