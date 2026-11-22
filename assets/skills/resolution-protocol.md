---
description: "How the resolver diagnoses an escalation and writes a proposal a human can apply. Load before you answer an escalation."
---

## Skill: Resolution protocol

1. **Read the escalation** in the message: `kind`, `reason`, `log_tail`, the step and run, and the
   `actions` a human may take with each action's payload fields.
2. **Look at the step**: `step_status(node)` (cycle, budget, verification, panel verdicts) and
   `workflow_context()` – for a build task, its contract, the criteria it serves and its
   neighbours. Read the documents and evidence the escalation cites (`docs_read`).
3. **Ask** the agents involved over A2A when their intent matters: the stuck agent
   (`<role>@<step>`), the judges whose findings blocked it, the oracle (`oracle@oracle`) about a
   protected test, the planner about the plan. Keep questions short and self-contained.
4. **Classify** what went wrong:
   * the agent misread its task or a finding → `retry` (or `answer`) with guidance;
   * the task contract is too narrow (files, scope) → an answer that widens it, or a plan
     revision;
   * a protected test or the oracle is wrong → `approve_protected_paths` for exactly the paths,
     with the evidence that the test (not the code) is wrong; or send the oracle back;
   * the environment or the verifier image lacks something → `retry` after the fix, saying what
     the operator must change;
   * a real decision only a human can take → `answer` with the options and your recommendation.
5. **Propose** exactly one of the listed `actions`, its `payload` as the action expects, a short
   `rationale`, and `attestations` for the evidence you relied on – the fenced JSON block of your
   prompt, at the end of your answer. A proposal that names an action not offered, or a payload
   the action rejects, is returned to you with the reason.

You never apply anything: a human reads your proposal on the escalation and applies, edits or
ignores it.
