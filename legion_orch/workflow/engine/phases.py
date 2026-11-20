"""Orchestrator phases (plan: stages-and-steps.md §2): the controller's work around an agent's, as
small objects that each end in one result – ok, rework (with feedback for the producer) or
escalate (a human decides). A phase does its work and says what it found; :func:`apply` turns a
rework or escalation into the node's state, and what an ``Ok`` means is up to the step.

``AgentRun`` and ``Panel`` are event-driven: an agent's run ends with a report (see
:mod:`.reports`), so ``AgentRun`` starts the run and ``Panel`` decides once every judge reported.
The others are awaited."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ...sandbox import VERIFIER_PREFIX, BuildError, ImageRecipe, is_name_clash
from ..model import (
    R_REPORTED,
    R_SUPERSEDED,
    REWORK,
    CommandResult,
    Feedback,
    Node,
    Readiness,
    Resume,
    Run,
    Verdict,
    Verification,
)
from ..ship import evaluate
from .core import ChangeCore

if TYPE_CHECKING:
    from .dispatch import Dispatcher
    from .sandboxes import Sandboxes

EXIT_NOT_FOUND = 127   # bash: command not found (126, not executable, is often a fixable repo file)


# ---------------------------------------------------------------------------- results
@dataclass(frozen=True)
class Ok:
    """The phase passed; ``value`` is what it produced (a merge's new head, a readiness
    result, built images)."""
    value: Any = None


@dataclass(frozen=True)
class Rework:
    """The producer has to do its work again, with this feedback (within its cycle budget).
    ``base``: the commit its next cycle starts from, if that moved (a merge conflict)."""
    feedback: list[Feedback]
    source: str
    base: str | None = None


@dataclass(frozen=True)
class Escalate:
    """A human decides; ``resume`` says where the node continues once they have. ``feedback``
    is kept on the node for the producer's next cycle."""
    kind: str
    question: str
    resume: Resume
    log_tail: str | None = None
    feedback: list[Feedback] = field(default_factory=list)


PhaseResult = Ok | Rework | Escalate


@dataclass
class PhaseContext:
    """What phases work with: the change, its sandboxes, and (for agent runs) the dispatcher."""
    c: ChangeCore
    sandboxes: Sandboxes
    dispatcher: Dispatcher | None = None

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

    def apply(self, node: Node, result: Rework | Escalate) -> None:
        """Under the lock: a rework or an escalation becomes the node's state."""
        if isinstance(result, Rework):
            self.rework(node, result.feedback, result.source)
            if result.base and node.status == REWORK:
                node.base_sha = result.base
        else:
            node.feedback += result.feedback
            self.c.escalate(node, result.kind, result.question, resume=result.resume,
                            log_tail=result.log_tail)


# ---------------------------------------------------------------------------- agent work
@dataclass(frozen=True)
class AgentRun:
    """Dispatch the role's agent for the node (guards G-D1..G-D4, G-B1); the run ends with a
    report the role's handler judges. Returns the prepared run (launched outside the lock), or
    None if a guard stopped it."""
    role: str

    def start(self, ctx: PhaseContext, node: Node, judged_sha: str | None = None) -> Run | None:
        assert ctx.dispatcher is not None
        return ctx.dispatcher.prepare(node, self.role, judged_sha=judged_sha)


@dataclass(frozen=True)
class Panel:
    """The review panel: one judge run per role at the candidate. Unanimous PASS at the candidate
    is ok; otherwise rework with the HIGH findings – or, when the same HIGH findings came back
    two cycles running, escalate (G-RV5)."""
    roles: tuple[str, ...]

    def missing(self, node: Node) -> list[str]:
        return [r for r in self.roles if node.review(node.cycle, r) is None]

    def decide(self, node: Node) -> PhaseResult | None:
        """None until every judge has reported (records the cycle's finding fingerprints)."""
        results = [node.review(node.cycle, r) for r in self.roles]
        if any(r is None for r in results):
            return None
        if all(r.verdict == Verdict.PASS and r.sha == node.candidate_sha for r in results):
            return Ok()
        high = [f for r in results for f in r.findings if f.high]
        fps = sorted({f.fingerprint for f in high})
        node.finding_history.append(fps)
        fb = [Feedback(cycle=node.cycle, source="review",
                       text=f"{f.severity} ({f.dimension or '?'}): {f.title} – fix: {f.fix[:500]}",
                       attestations=f.attestations) for f in high]
        fb.append(Feedback(cycle=node.cycle, source="review", text="full review documents",
                           attestations=[r.doc for r in results if r.doc]))
        if len(node.finding_history) >= 2 and fps and node.finding_history[-1] == node.finding_history[-2]:
            return Escalate("stale_loop", "G-RV5: identical HIGH findings in two consecutive "
                            "review cycles", Resume.REWORK_CYCLE, feedback=fb)
        return Rework(fb, "review")


# ---------------------------------------------------------------------------- verification
@dataclass(frozen=True)
class Verify:
    """G-V1: the commands at a commit in a fresh verifier sandbox, after the repository's setup
    (§5.3c), which stops the run if it fails (G-V3). ``expect_failures``: before any build the
    commands are expected to fail, so failures are recorded, not reworked."""
    commands: tuple[str, ...]
    expect_failures: bool = False

    async def verification(self, ctx: PhaseContext, label: str, sha: str) -> Verification:
        """Run it. Never raises. A name the gateway already has (another controller) is
        replaced and the sandbox created again."""
        c = ctx.c
        result = Verification(sha=sha, sandbox=c.host.new_sandbox_name(VERIFIER_PREFIX))
        if not self.commands and not c.change.setup_pin:
            result.infra_error = "no commands to verify"
            return result
        for attempt in range(3):
            try:
                async with ctx.sandboxes.verifier(result.sandbox, label, sha) as box:
                    pin = c.change.setup_pin
                    if pin:                    # §5.3c, G-V3: setup first; a failure stops here
                        r = await box.setup(pin.blob, c.cfg.limits.setup_timeout_s)
                        result.results.append(r)
                    if not pin or result.results[-1].exit_code == 0:
                        for cmd in self.commands:
                            result.results.append(await box.run(cmd))
                result.ok = all(r.exit_code == 0 for r in result.results)
            except Exception as e:
                if is_name_clash(e) and not result.results and attempt < 2:
                    result.sandbox = c.host.new_sandbox_name(VERIFIER_PREFIX)
                    continue
                result.infra_error = str(e)
            return result
        return result

    def judge(self, res: Verification, cycle: int, att: str | None) -> PhaseResult:
        """What the recorded verification (``att``) means. A setup failure is escalated as
        ``setup_failed`` when failures are expected (nothing was built yet that could have
        caused it); otherwise it fails like any command."""
        fails = [r for r in res.results if r.exit_code != 0]
        unrunnable = [r for r in fails if r.exit_code == EXIT_NOT_FOUND and not r.setup]
        setup = setup_failure(res)
        if res.infra_error:
            return Escalate("verify_infra", res.infra_error, Resume.VERIFYING)
        if unrunnable:
            # G-V2: the verifier image lacks a command – the agent cannot fix that, so no
            # rework cycle is spent; a human fixes the image and retries (re-verifies).
            return Escalate("verify_unrunnable",
                            "G-V2: command(s) not found in the verifier sandbox: "
                            + ", ".join(f"`{r.command}`" for r in unrunnable), Resume.VERIFYING,
                            log_tail="\n".join(r.output[-800:] for r in unrunnable))
        if setup and self.expect_failures:
            return Escalate("setup_failed", f"G-V3: {setup_failed_text(setup)}", Resume.VERIFYING,
                            log_tail=setup.output[-800:])
        if self.expect_failures or res.ok:
            return Ok(res)
        return Rework([Feedback(cycle=cycle, source="verification",
                                text=f"BLOCKER: `{r.command}` exited {r.exit_code}: "
                                     f"{r.output[-800:]}", attestations=[att] if att else [])
                       for r in fails], "verification")


def setup_failure(res: Verification) -> CommandResult | None:
    return next((r for r in res.results if r.setup and r.exit_code != 0), None)


def setup_failed_text(r: CommandResult) -> str:
    return (f"the repository's setup `{r.command}` exited {r.exit_code} in a fresh verifier "
            "sandbox, so no acceptance command ran")


# ---------------------------------------------------------------------------- merge
@dataclass(frozen=True)
class Merge:
    """G-A1 (verification and every panel PASS at the candidate, unless overridden), G-H1 (the
    integration head is where the controller left it), then the merge into the integration
    branch; a conflict is rework onto the new head (G-A2). Under the lock. Ok: the new head."""
    panel: tuple[str, ...]

    async def run(self, ctx: PhaseContext, node: Node) -> PhaseResult:
        c = ctx.c
        sha = node.candidate_sha
        ver = node.current_verification()
        v = []
        if not ver or not ver.ok or ver.sha != sha:
            v.append("G-A1: no passing deterministic verification at candidate")
        if not node.override:
            for role in self.panel:
                r = node.review(node.cycle, role)
                if not r or r.verdict != Verdict.PASS or r.sha != sha:
                    v.append(f"G-A1: no {role} PASS at candidate")
        if v:
            return Escalate("accept_guard", "; ".join(v), Resume.REWORK)
        if moved := c.head_moved(await c.git.integration_head(c.change.id)):
            return Escalate("head_moved", moved, Resume.MERGING)
        ok, out = await c.git.merge(c.cid, sha, f"legion {c.change.id}: {node.title} "
                                                f"(cycle {node.cycle})")
        if not ok:
            head = await c.git.integration_head(c.change.id)
            return Rework([Feedback(
                cycle=node.cycle, source="merge",
                text=f"G-A2: merge conflict with integration; rebase onto {head} "
                     f"and force-push your branch. Details: {out[-600:]}",
                attestations=[f"git:{c.cfg.repo.name}@{head}"])], "merge", base=head)
        c.change.integration_head = out
        return Ok(out)


# ---------------------------------------------------------------------------- environment
@dataclass
class BuildImages:
    """Build (or find) the image of each recipe. Ok: recipe tag -> image reference. ``log``
    keeps what was built, for the provisioning record."""
    recipes: dict[str, ImageRecipe]
    log: list[str] = field(default_factory=list)

    async def run(self, ctx: PhaseContext) -> PhaseResult:
        images: dict[str, str | None] = {}
        try:
            for tag, recipe in self.recipes.items():
                self.log.append(f"## image {tag}\n\n{recipe.files['Dockerfile']}")
                images[tag], out = await ctx.c.svc.builder.ensure_image(recipe)
                self.log.append(out)
        except (BuildError, OSError) as e:
            return Escalate("provision_failed", f"G-PV1: {e}"[:2000], Resume.READY,
                            log_tail="\n".join(self.log)[-4000:] or None)
        return Ok(images)


# ---------------------------------------------------------------------------- checks
@dataclass(frozen=True)
class Evaluate:
    """Deterministic checks, no sandbox: the ship gates (workflow/ship.py). Always Ok with the
    readiness: a NOT_READY verdict is a finding for the gate that reads it, not a failure."""

    async def run(self, ctx: PhaseContext) -> Ok:
        result: Readiness = await evaluate(ctx.c)
        return Ok(result)
