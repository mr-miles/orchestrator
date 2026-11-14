"""Deterministic verification, accept and merge.

Mixin of :class:`ChangeEngine`; relies on its state and helpers."""
from __future__ import annotations

import secrets
import shlex
import shutil

from ...sandbox import (
    VERIFY_SETUP_SH,
    SandboxError,
    SandboxSpec,
)
from ..model import (
    ACCEPTED,
    BUILD,
    INTEGRATION,
    MERGING,
    REVIEWING,
    REWORK,
    VERIFYING,
    CommandResult,
    Feedback,
    Resume,
    Verdict,
    Verification,
)
from .auth import VerifierGrant
from .common import _slug


class VerifyMixin:
    def start_verification(self, node_id: str) -> None:
        """Run the node's deterministic verification in the background (node is VERIFYING)."""
        self._spawn(self._verify_node(node_id))

    async def _verify(self, label: str, sha: str, commands: list[str]) -> Verification:
        """G-V1: run commands at ``sha`` in a fresh sandbox. Never raises."""
        name = f"lg-{self.change.id}-v{secrets.token_hex(3)}".lower()
        base = self.svc.tmp / "verify" / name
        payload = base / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "run.env").write_text(
            f"LEGION_GIT_URL={shlex.quote(self.git_url)}\n")
        (payload / "verify-setup.sh").write_text(VERIFY_SETUP_SH)
        result = Verification(sha=sha, sandbox=name)
        created, ref = False, None
        try:
            policy = self._policy(base / "policy.yaml", agent=False, push=False)
            ref = await self.runner.create(SandboxSpec(
                name=name, payload_dir=payload, policy_file=policy, agent=False,
                change=self.cid, task=label, role="verifier", run=name))
            self.svc.auth.register_verifier(      # read-only git grant while it lives
                ref, VerifierGrant(id=f"verifier:{name}", change=self.cid, task=label))
            created = True
            rc, out = await self.runner.exec(name, ["bash", "/sandbox/payload/verify-setup.sh", sha],
                                             timeout=600)
            if rc != 0:
                raise SandboxError(f"verifier setup failed ({rc}): {out[-1500:]}")
            ok = True
            for cmd in commands:
                rc, out = await self.runner.exec(name, ["bash", "-lc", cmd], workdir="/sandbox/work",
                                                 timeout=self.cfg.limits.verify_timeout_s)
                result.results.append(CommandResult(command=cmd, exit_code=rc, output=out[-4000:]))
                ok = ok and rc == 0
            result.ok = ok and bool(commands)
            if not commands:
                result.infra_error = "no commands to verify"
        except Exception as e:
            result.infra_error = str(e)
        finally:
            if ref:
                self.svc.auth.unregister_verifier(ref)
            shutil.rmtree(base, ignore_errors=True)
            if created:
                try:
                    await self.runner.delete(name)
                except Exception as e:
                    self.event("sandbox", f"verifier delete failed: {e}")
        return result

    async def _verify_node(self, node_id: str) -> None:
        node = self.nodes[node_id]
        if node.kind == BUILD:
            commands = node.task.verification
        else:
            commands = self.oracle_commands()
        self.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                    f"({len(commands)} command(s))", node_id)
        res = await self._verify(node_id, node.candidate_sha, commands)
        async with self.lock:
            if node.status != VERIFYING:
                return
            att = self.write_record(f"tasks/{node.task.id}/verification.json" if node.kind == BUILD
                                    else f"control/verification/{_slug(node_id)}.json", res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            key = "integration" if node.kind == INTEGRATION else str(node.cycle)
            node.verification[key] = res.model_copy(update={
                "attestation": att, "sandbox": None,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            fails = [r for r in res.results if r.exit_code != 0]
            if res.infra_error:
                self.escalate(node, "verify_infra", res.infra_error, resume=Resume.VERIFYING)
            elif not res.ok:
                if node.kind == BUILD:
                    self._rework(node, [Feedback(cycle=node.cycle, source="verification",
                                                 text=f"BLOCKER: `{r.command}` exited {r.exit_code}: "
                                                      f"{r.output[-800:]}", attestations=[att])
                                        for r in fails], "verification")
                else:
                    self.escalate(node, "integration_failed",
                                   f"G-V1: {len(fails)} oracle command(s) failed at head",
                                   resume=Resume.READY)
            elif node.kind == BUILD:
                if self.change.panel:
                    node.status = REVIEWING
                    self.event("review", f"verification passed; panel {self.change.panel}", node_id)
                else:
                    node.status = MERGING
                    self._spawn(self._merge_node(node_id))
            else:  # integration
                node.status = ACCEPTED
                self.accept_runs(node)
            self._save()

    async def _merge_node(self, node_id: str) -> None:
        async with self.lock:
            node = self.nodes[node_id]
            sha = node.candidate_sha
            ver = node.current_verification()
            v = []
            if not ver or not ver.ok or ver.sha != sha:
                v.append("G-A1: no passing deterministic verification at candidate")
            if not node.override:
                for role in self.change.panel:
                    r = node.review(node.cycle, role)
                    if not r or r.verdict != Verdict.PASS or r.sha != sha:
                        v.append(f"G-A1: no {role} PASS at candidate")
            if v:
                self.escalate(node, "accept_guard", "; ".join(v), resume=Resume.REWORK)
                self._save()
                return
            ok, out = await self.git.merge(self.cid, sha, f"legion {self.change.id}: {node.title} "
                                                f"(cycle {node.cycle})")
            if not ok:
                head = await self.git.integration_head(self.change.id)
                self._rework(node, [Feedback(
                    cycle=node.cycle, source="merge",
                    text=f"G-A2: merge conflict with integration; rebase onto {head} "
                         f"and force-push your branch. Details: {out[-600:]}",
                    attestations=[f"git:{self.cfg.repo.name}@{head}"])], "merge")
                if node.status == REWORK:
                    node.base_sha = head
                self._save()
                return
            self.change.integration_head = out
            node.accepted_sha = sha
            node.status = ACCEPTED
            self.event("accepted", f"merged {sha[:12]} -> integration {out[:12]}", node_id)
            self.accept_runs(node)  # deletes the implementer sandbox (principle 8)
            self._save()
