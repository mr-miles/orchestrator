"""Acceptance: verification, the review panel's decision, rework, merge and ship readiness."""
from __future__ import annotations

import contextlib
import shlex
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass

from ...sandbox import (
    VERIFY_SETUP_SH,
    Runner,
    SandboxError,
    SandboxSpec,
    verifier_sandbox_name,
)
from ..model import (
    ACCEPTED,
    BUILD,
    INTEGRATION,
    MERGING,
    R_REPORTED,
    R_SUPERSEDED,
    REVIEWING,
    REWORK,
    SHIP,
    VERIFYING,
    ChangeStatus,
    CommandResult,
    Feedback,
    Node,
    Resume,
    Verdict,
    Verification,
)
from ..ship import evaluate
from .auth import VerifierGrant
from .common import _slug
from .core import ChangeCore
from .runs import RunLifecycle


@dataclass
class VerifierSandbox:
    """A live verifier sandbox with the candidate checked out in /sandbox/work."""
    name: str
    runner: Runner
    timeout_s: int

    async def run(self, cmd: str) -> CommandResult:
        rc, out = await self.runner.exec(self.name, ["bash", "-lc", cmd], workdir="/sandbox/work",
                                         timeout=self.timeout_s)
        return CommandResult(command=cmd, exit_code=rc, output=out[-4000:])


class Acceptance:
    """How a candidate becomes accepted work: deterministic verification (G-V1), the review
    panel's decision, rework within the cycle budget, the merge (G-A1, G-A2) – and, for the
    change as a whole, the ship readiness check."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle):
        self.c, self.lifecycle = core, lifecycle

    def start_verification(self, node_id: str) -> None:
        """Run the node's deterministic verification in the background (node is VERIFYING)."""
        self.c.spawn(self.verify_node(node_id))

    async def _verify(self, label: str, sha: str, commands: list[str]) -> Verification:
        """G-V1: run commands at ``sha`` in a fresh verifier sandbox. Never raises."""
        name = verifier_sandbox_name()
        result = Verification(sha=sha, sandbox=name)
        if not commands:
            result.infra_error = "no commands to verify"
            return result
        try:
            async with self._verifier_sandbox(name, label, sha) as box:
                for cmd in commands:
                    result.results.append(await box.run(cmd))
            result.ok = all(r.exit_code == 0 for r in result.results)
        except Exception as e:
            result.infra_error = str(e)
        return result

    @contextlib.asynccontextmanager
    async def _verifier_sandbox(self, name: str, label: str,
                                sha: str) -> AsyncIterator[VerifierSandbox]:
        """A non-agent sandbox with a read-only git grant, checked out at ``sha``. On exit the
        grant is revoked and the sandbox and its payload are deleted, however the block ends."""
        base = self.c.svc.tmp / "verify" / name
        payload = base / "payload"
        payload.mkdir(parents=True, exist_ok=True)
        (payload / "run.env").write_text(f"LEGION_GIT_URL={shlex.quote(self.c.git_url)}\n")
        (payload / "verify-setup.sh").write_text(VERIFY_SETUP_SH)
        ref = None
        try:
            policy = self.c.svc.write_policy(base / "policy.yaml", agent=False, push=False)
            ref = await self.c.runner.create(SandboxSpec(
                name=name, payload_dir=payload, policy_file=policy, agent=False,
                change=self.c.cid, task=label, role="verifier", run=name))
            self.c.svc.auth.register_verifier(
                ref, VerifierGrant(id=f"verifier:{name}", change=self.c.cid, task=label))
            rc, out = await self.c.runner.exec(name, ["bash", "/sandbox/payload/verify-setup.sh", sha],
                                               timeout=600)
            if rc != 0:
                raise SandboxError(f"verifier setup failed ({rc}): {out[-1500:]}")
            yield VerifierSandbox(name, self.c.runner, self.c.cfg.limits.verify_timeout_s)
        finally:
            if ref:
                self.c.svc.auth.unregister_verifier(ref)
            shutil.rmtree(base, ignore_errors=True)
            if ref:
                try:
                    await self.c.runner.delete(name)
                except Exception as e:
                    self.c.event("sandbox", f"verifier delete failed: {e}")

    async def verify_node(self, node_id: str) -> None:
        """Verify the node's candidate; then review (or merge), rework, or escalate."""
        c, node = self.c, self.c.nodes[node_id]
        commands = node.task.verification if node.kind == BUILD else c.oracle_commands()
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({len(commands)} command(s))", node_id)
        res = await self._verify(node_id, node.candidate_sha, commands)
        async with c.lock:
            if node.status != VERIFYING:
                return
            att = c.write_record(f"tasks/{node.task.id}/verification.json" if node.kind == BUILD
                                 else f"control/verification/{_slug(node_id)}.json", res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            key = "integration" if node.kind == INTEGRATION else str(node.cycle)
            node.verification[key] = res.model_copy(update={
                "attestation": att, "sandbox": None,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            fails = [r for r in res.results if r.exit_code != 0]
            if res.infra_error:
                c.escalate(node, "verify_infra", res.infra_error, resume=Resume.VERIFYING)
            elif not res.ok:
                if node.kind == BUILD:
                    self.rework(node, [Feedback(cycle=node.cycle, source="verification",
                                                text=f"BLOCKER: `{r.command}` exited {r.exit_code}: "
                                                     f"{r.output[-800:]}", attestations=[att])
                                       for r in fails], "verification")
                else:
                    c.escalate(node, "integration_failed",
                               f"G-V1: {len(fails)} oracle command(s) failed at head",
                               resume=Resume.READY)
            elif node.kind == BUILD:
                if c.change.panel:
                    node.status = REVIEWING
                    c.event("review", f"verification passed; panel {c.change.panel}", node_id)
                else:
                    node.status = MERGING
                    c.spawn(self.merge_node(node_id))
            else:  # integration
                node.status = ACCEPTED
                self.lifecycle.accept_runs(node)
            c.save()

    def panel_complete(self, node: Node) -> None:
        """Once every panel member has reported: merge on a unanimous PASS at the candidate,
        otherwise rework with the HIGH findings (or escalate a stale loop, G-RV5)."""
        c = self.c
        results = [node.review(node.cycle, r) for r in c.change.panel]
        if any(r is None for r in results):
            return
        if all(r.verdict == Verdict.PASS and r.sha == node.candidate_sha for r in results):
            node.status = MERGING
            c.spawn(self.merge_node(node.id))
            return
        high = [f for r in results for f in r.findings if f.high]
        fps = sorted({f.fingerprint for f in high})
        node.finding_history.append(fps)
        fb = [Feedback(cycle=node.cycle, source="review",
                       text=f"{f.severity} ({f.dimension or '?'}): {f.title} – fix: {f.fix[:500]}",
                       attestations=f.attestations) for f in high]
        fb.append(Feedback(cycle=node.cycle, source="review", text="full review documents",
                           attestations=[r.doc for r in results if r.doc]))
        if len(node.finding_history) >= 2 and fps and node.finding_history[-1] == node.finding_history[-2]:
            node.feedback += fb
            c.escalate(node, "stale_loop", "G-RV5: identical HIGH findings in two consecutive "
                       "review cycles", resume=Resume.REWORK_CYCLE)
            return
        self.rework(node, fb, "review")

    def rework(self, node: Node, feedback: list[Feedback], source: str) -> None:
        """Evidence-driven rework within the cycle budget (G-RV5)."""
        c = self.c
        node.feedback += feedback
        limit = c.cfg.limits.review_max_cycles + node.extra_cycles
        cur = c.runs.get(node.current_run) if node.current_run else None
        if cur and cur.status == R_REPORTED:
            cur.status = R_SUPERSEDED
        if node.cycle >= limit:
            c.escalate(node, "cycle_budget",
                       f"G-RV5: {node.cycle} cycle(s) used without acceptance ({source})",
                       resume=Resume.REWORK_CYCLE)
            return
        node.cycle += 1
        node.candidate_sha = None
        node.status = REWORK
        c.event("rework", f"{source}: rework cycle {node.cycle}", node.id)

    async def merge_node(self, node_id: str) -> None:
        """G-A1 (verification and every panel PASS at the candidate, unless overridden), then
        merge into the integration branch; a conflict is rework (G-A2)."""
        c = self.c
        async with c.lock:
            node = c.nodes[node_id]
            sha = node.candidate_sha
            ver = node.current_verification()
            v = []
            if not ver or not ver.ok or ver.sha != sha:
                v.append("G-A1: no passing deterministic verification at candidate")
            if not node.override:
                for role in c.change.panel:
                    r = node.review(node.cycle, role)
                    if not r or r.verdict != Verdict.PASS or r.sha != sha:
                        v.append(f"G-A1: no {role} PASS at candidate")
            if v:
                c.escalate(node, "accept_guard", "; ".join(v), resume=Resume.REWORK)
                c.save()
                return
            ok, out = await c.git.merge(c.cid, sha, f"legion {c.change.id}: {node.title} "
                                                    f"(cycle {node.cycle})")
            if not ok:
                head = await c.git.integration_head(c.change.id)
                self.rework(node, [Feedback(
                    cycle=node.cycle, source="merge",
                    text=f"G-A2: merge conflict with integration; rebase onto {head} "
                         f"and force-push your branch. Details: {out[-600:]}",
                    attestations=[f"git:{c.cfg.repo.name}@{head}"])], "merge")
                if node.status == REWORK:
                    node.base_sha = head
                c.save()
                return
            c.change.integration_head = out
            node.accepted_sha = sha
            node.status = ACCEPTED
            c.event("accepted", f"merged {sha[:12]} -> integration {out[:12]}", node_id)
            self.lifecycle.accept_runs(node)  # deletes the implementer sandbox (principle 8)
            c.save()

    async def ship(self) -> None:
        """The ship readiness check (workflow/ship.py); its verdict is the change's status."""
        result = await evaluate(self.c)
        c = self.c
        async with c.lock:
            result.attestation = c.write_record("control/ship/readiness.json", result).attestation
            c.change.ship = result
            c.change.status = ChangeStatus(result.verdict)
            c.nodes[SHIP].status = ACCEPTED
            c.event("ship", f"readiness verdict: {result.verdict}", SHIP)
            c.save()
