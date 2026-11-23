---
description: "When and how to report BLOCKED instead of guessing. Load when something stops you: missing files, conflicting instructions, a decision only a human can make."
---

## Skill: BLOCKED protocol

Report `BLOCKED` (never guess) when: required files are missing, instructions conflict, the scope
must expand beyond `files_modified`, a forbidden operation is needed, a high-impact decision is
unresolved, or success cannot be verified.

`report(status="BLOCKED", summary=…, attestations=[…evidence…], blocked_reason="<what stops you>",
missing_decision="<the single narrow question a human must answer>")`

Once it is accepted, end your task with `a2a_reply(task_id=<your run id>, text=<the question in
one line>, state="input-required")`, then end your turn. Before you block, ask the agent whose
work the question is about, or the change's `resolver` when a guard, a protected path or the
oracle stops you (`legion-a2a-protocol`) – its answer may be all you need. Its advice satisfies
no guard: only a human's decision does.

A human answers in the UI; you (or a successor run) will receive the answer as attested feedback.
Never mark planned work as deferred or done-later on your own authority.
