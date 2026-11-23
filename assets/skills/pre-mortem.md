---
description: "How to critique a plan: failure stories, assumptions, completeness, parallel safety, oracle integrity, sandbox environment. Load when you critique a plan."
---

## Skill: Pre-mortem and assumption hunting

Imagine the plan shipped and failed. For each plausible cause, check the plan:

1. **Pre-mortem:** list the 3–5 most likely failure stories (integration seams, data shape drift,
   missing error paths, ordering/parallelism hazards, untestable acceptance criteria).
2. **Assumptions:** list every assumption the plan makes about existing code, APIs, environment
   or data. Verify each against the repository (cite `git:` attestations). Unverified high-impact
   assumptions are BLOCKERs. A contract that relies on how a pinned tool behaves – a config key,
   a flag – that you could not run is at least a WARNING, unless the scaffold probes it or
   the plan uses a form known to work (a documented CLI flag rather than a config key).
3. **Completeness:** every requirement covered, every build (and the scaffold) decision-complete (no "choose an
   approach"), verification commands that actually exercise the acceptance criteria – and that can
   exit 0 from the build's `files_modified` alone (type-checker includes of other builds' or
   protected files, coverage thresholds on files the build may not touch). Every build has the
   criteria its tier requires, and the scaffold is decided: given its work (required in a blank
   repository) or cancelled.
4. **Parallel safety:** builds in one group run in parallel: they must not touch the same files
   or rely on each other's outputs – a build that needs another's code belongs in a later group.
5. **Oracle integrity:** acceptance tests are black-box and out of process (they never import the
   code under test), and every runner config/fixture they rely on is in `protected_support`.
   Otherwise an implementer could make the oracle pass without the behaviour – a BLOCKER.
   They must also run under the repository's real test configuration: global setup or preload
   files that replace runtime globals can break them for reasons unrelated to the code.

6. **Sandbox environment** (the design's `environment` output): every
   verification, acceptance and integration command can run in the profile of the role that runs
   it (the `verifier` profile for builds' verification commands) – each tool it calls is declared;
   network policies are the least each role needs (judges and the verifier normally need none);
   every proposed library entry and non-default resource is justified in `rationale`. A command
   that cannot run in its sandbox is a BLOCKER.

Verdict `REWORK` if any HIGH-confidence BLOCKER or WARNING exists, else `PASS`.
