"""Acceptance: verification, the review panel's decision, rework, merge and ship readiness."""
from __future__ import annotations

from ...sandbox import (
    VERIFIER_PREFIX,
    is_name_clash,
)
from .. import governance
from ..model import (
    ACCEPTED,
    BASELINE,
    BUILD,
    FAILED,
    INTEGRATION,
    MERGING,
    ORACLE,
    PENDING,
    PREFLIGHT,
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
    Oracle,
    ReadinessVerdict,
    Resume,
    Verdict,
    Verification,
)
from ..ship import evaluate
from . import cleanup
from .common import _slug
from .core import ChangeCore
from .runs import RunLifecycle
from .sandboxes import Sandboxes

EXIT_NOT_FOUND = 127   # bash: command not found (126, not executable, is often a fixable repo file)


class Acceptance:
    """How a candidate becomes accepted work: deterministic verification (G-V1), the review
    panel's decision, rework within the cycle budget, the merge (G-A1, G-A2) – and, for the
    change as a whole, the ship readiness check."""

    def __init__(self, core: ChangeCore, lifecycle: RunLifecycle, sandboxes: Sandboxes):
        self.c, self.lifecycle, self.sandboxes = core, lifecycle, sandboxes

    def start_verification(self, node_id: str) -> None:
        """Run the node's deterministic verification in the background (node is VERIFYING)."""
        self.c.spawn(self.verify_node(node_id))

    async def _verify(self, label: str, sha: str, commands: list[str]) -> Verification:
        """G-V1: run commands at ``sha`` in a fresh verifier sandbox – after the repository's
        setup (§5.3c), which stops the run if it fails (G-V3). Never raises. A name the gateway
        already has (another controller) is replaced and the sandbox created again."""
        result = Verification(sha=sha, sandbox=self.c.host.new_sandbox_name(VERIFIER_PREFIX))
        if not commands and not self.c.change.setup_pin:
            result.infra_error = "no commands to verify"
            return result
        for attempt in range(3):
            try:
                async with self.sandboxes.verifier(result.sandbox, label, sha) as box:
                    pin = self.c.change.setup_pin
                    if pin:                    # §5.3c, G-V3: setup first; a failure stops here
                        r = await box.setup(pin.blob, self.c.cfg.limits.setup_timeout_s)
                        result.results.append(r)
                    if not pin or result.results[-1].exit_code == 0:
                        for cmd in commands:
                            result.results.append(await box.run(cmd))
                result.ok = all(r.exit_code == 0 for r in result.results)
            except Exception as e:
                if is_name_clash(e) and not result.results and attempt < 2:
                    result.sandbox = self.c.host.new_sandbox_name(VERIFIER_PREFIX)
                    continue
                result.infra_error = str(e)
            return result
        return result

    async def verify_node(self, node_id: str) -> None:
        """Verify the node's candidate; then review (or merge), rework, or escalate."""
        c, node = self.c, self.c.nodes[node_id]
        commands = (node.task.verification if node.kind == BUILD
                    else c.baseline_commands() if node.kind == BASELINE
                    else list((c.change.oracle or Oracle()).acceptance_commands)
                    if node.kind == PREFLIGHT else c.oracle_commands())
        c.event("verify", f"deterministic verification of {node.candidate_sha[:12]} "
                f"({'setup and ' if c.change.setup_pin else ''}{len(commands)} command(s))",
                node_id)
        res = await self._verify(node_id, node.candidate_sha, commands)
        async with c.lock:
            if node.status != VERIFYING:
                return
            att = c.write_record(f"tasks/{node.task.id}/verification.json" if node.kind == BUILD
                                 else f"control/verification/{_slug(node_id)}.json", res).attestation
            # The node keeps a compact copy (the stored record has the command output).
            key = {INTEGRATION: "integration", BASELINE: "baseline",
                   PREFLIGHT: "preflight"}.get(node.kind, str(node.cycle))
            node.verification[key] = res.model_copy(update={
                "attestation": att,
                "results": [r.model_copy(update={"output": ""}) for r in res.results]})
            fails = [r for r in res.results if r.exit_code != 0]
            unrunnable = [r for r in fails if r.exit_code == EXIT_NOT_FOUND and not r.setup]
            setup_failed = next((r for r in fails if r.setup), None)
            if res.infra_error:
                c.escalate(node, "verify_infra", res.infra_error, resume=Resume.VERIFYING)
            elif unrunnable:
                # G-V2: the verifier image lacks a command – the agent cannot fix that, so no
                # rework cycle is spent; a human fixes the image and retries (re-verifies).
                c.escalate(node, "verify_unrunnable",
                           "G-V2: command(s) not found in the verifier sandbox: "
                           + ", ".join(f"`{r.command}`" for r in unrunnable),
                           resume=Resume.VERIFYING,
                           log_tail="\n".join(r.output[-800:] for r in unrunnable))
            elif setup_failed and node.kind in (PREFLIGHT, BASELINE):   # before any build
                self._setup_failed(node, setup_failed, att)
            elif node.kind == PREFLIGHT:
                # G-PF1: the commands can be run here; before the build they are expected to
                # fail, so an acceptance command that already passes is only noted
                node.status = ACCEPTED
                for r in res.results:
                    if not r.setup and r.exit_code == 0:
                        c.event("preflight", f"acceptance command `{r.command}` already passes "
                                "before any build: it may not test the change", node_id)
            elif node.kind == BASELINE:
                # before any build, failures are expected: they are recorded, not reworked
                # (G-BL2 flags at H-2 what already passes or differs from the agent's results)
                node.status = ACCEPTED
                self.lifecycle.accept_runs(node)
                for w in governance.baseline_warnings(c):
                    c.event("baseline", w, node_id)
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

    def _setup_failed(self, node: Node, r: CommandResult, att: str) -> None:
        """G-V3: the repository's setup failed, so nothing after it ran. At preflight the oracle's
        author is sent the output (one oracle cycle; fixing it may need the plan's environment,
        which it then reports BLOCKED); at the baseline a human decides. (In a build or at integration the
        tasks may have caused it: it fails like any command there.)"""
        c = self.c
        what = (f"the repository's setup `{r.command}` exited {r.exit_code} in a fresh verifier "
                "sandbox, so no acceptance command ran")
        oracle = c.nodes.get(ORACLE)
        limit = c.cfg.limits.review_max_cycles + (oracle.extra_cycles if oracle else 0)
        if node.kind == PREFLIGHT and oracle is not None and oracle.cycle < limit:
            oracle.cycle += 1
            oracle.feedback.append(Feedback(
                cycle=oracle.cycle, source="verification", attestations=[att],
                text=f"BLOCKER (preflight): {what}. Output: {r.output[-800:]}. The acceptance "
                     "commands must run in a fresh clone after only the repository's setup "
                     "(mise.toml's setup task). Fix your tests or support files; if the fix is in "
                     "the setup (the plan's environment) or plan/oracle.json, report BLOCKED with "
                     "it as missing_decision."))
            oracle.candidate_sha = None
            oracle.status = REWORK
            node.status = PENDING        # runs again once the oracle is accepted
            c.event("rework", f"preflight: {what}: oracle rework cycle {oracle.cycle}", ORACLE)
            return
        c.escalate(node, "setup_failed", f"G-V3: {what}", resume=Resume.VERIFYING,
                   log_tail=r.output[-800:])

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
            if moved := c.head_moved(await c.git.integration_head(c.change.id)):
                c.escalate(node, "head_moved", moved, resume=Resume.MERGING)
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
            run = c.runs.get(node.current_run) if node.current_run else None
            await cleanup.delete_run_branch(c, run.branch if run else None, node_id)
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
            # the node shows the verdict: NOT_READY waits for a human (attest, approve, re-run)
            c.nodes[SHIP].status = ACCEPTED if result.verdict == ReadinessVerdict.READY else FAILED
            c.event("ship", f"readiness verdict: {result.verdict}", SHIP)
            c.save()
