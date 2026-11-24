---
description: "How the resolver diagnoses a waiting ticket and proposes a remedy a human can apply. Load before you answer an escalation."
---

## Skill: Resolution protocol

1. **Read the message**: the ticket, why it waits (`waiting`), its attempt and limit, its findings,
   its criteria and their verdicts, its last run (and its failure), and the `actions` a human may
   choose – its remedies:
   * `retry` – the same ticket again, same attempt count; `payload: {"answer": "…"}` is what its
     next round is told (for `blocked`: the answer to its question). Offered when it waits on
     something outside the agent (`system`, `budget`, `blocked`);
   * `waive:<criterion>` – a rejected, waivable criterion counts as passed; `payload:
     {"reason": "…"}` (required). Verification, preflight, can-merge and sign-offs cannot be
     waived;
   * `restart` – the build starts afresh: its branch and its runs' sandboxes are deleted, then its
     attempts, findings and runs are cleared and it starts at attempt 1 (run records and evidence
     stay). Only for a build ticket with work of its own – never a grouping ticket, a criterion, a
     ticket with subtasks or one whose merge is owed. Destructive: prefer `retry`, `waive` or
     `replan` unless its work (branch, sandbox) is unsalvageable;
   * `replan` – the constraints must change; `payload: {"plan": <a replan document>}` (plan
     format skill: `add` after the completed groups, `cancel` or `update` tickets not started,
     `fail` work that cannot be finished). The plan either **replaces** the ticket – it lists it
     in `fail` and adds what takes its place – or **repairs** it – it adds under it (fix builds
     into `delivery` when its integration failed: its criteria then judge the new head again).
     A human applying it applies the plan in the same decision.
2. **Look at the ticket**: `ticket_status(ticket)` and `workflow_context()` – for a build, its work
   (contract) and the tickets around it. Read the documents and evidence it cites (`docs_read`).
3. **Ask** the agents involved over A2A when their intent matters: the stuck agent
   (`<role>@<ticket>`), the judges whose findings blocked it, the oracle (`oracle@oracle`) about a
   protected test, the planner (`planner@design`) about the plan. Keep questions short and
   self-contained.
4. **Classify** what went wrong:
   * the agent misread its contract or a finding, or needs a decision → `retry` with the answer;
   * a judge's finding is wrong or not worth another attempt → `waive:<criterion>` with why;
   * the contract is too narrow (files, scope), a protected test or the oracle is wrong, an
     integration failed → `replan`: new tickets that fix it (with the evidence that the test,
     not the code, is wrong, when it is the oracle);
   * the environment or the verifier image lacks something → `retry` after the fix, saying what
     the operator must change.
5. **Propose** exactly one of the listed `actions`, its `payload`, a short `rationale`, and
   `attestations` for the evidence you relied on – the fenced JSON block of your prompt, at the
   end of your answer. A proposal that names an action not offered is returned to you with the
   reason.

You never apply anything: a human reads your proposal on the escalation and applies, edits or
ignores it.
