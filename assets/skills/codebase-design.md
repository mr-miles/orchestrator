---
description: "Review a codebase's technical design: find shallow modules, leaky seams and tangled dependencies, and record concrete, ranked design findings. Load when you review a codebase's architecture or design."
---

> Adapted from https://github.com/mattpocock/skills/tree/main/skills/engineering/codebase-design (licence not verified; the upstream text was not available when this file was written). Summary only: upstream's licence does not allow copying.

## Skill: Codebase design review

A short summary, in our own words, of the idea behind upstream's codebase-design skill: a codebase is
easier to change when its modules hide complexity behind small, stable interfaces. Use it to review
the technical design of an existing codebase and to record findings. It does not change code.

### Approach

1. **Map the modules.** List the main modules or packages, what each is responsible for, and who
   calls whom. Note the public surface of each (functions, classes, files, commands).
2. **Judge depth.** A deep module offers a small interface over substantial behaviour. A shallow
   module has an interface about as large as its implementation, so it adds a hop without hiding
   anything. Flag shallow modules, pass-through layers and thin wrappers.
3. **Check the seams.** Look at where modules meet. Flag leaked internals (callers that know
   another module's data layout or ordering rules), cyclic dependencies, shared mutable state,
   and one change that forces edits in many places.
4. **Check testability.** A good design can be tested through its interface. Flag logic that can
   only be reached through slow or external dependencies, and tests that mirror the implementation.
5. **Check naming and vocabulary.** Names should come from the domain and be used consistently.
   Flag two names for one concept and one name for two concepts.

### Recording findings

For each finding give: a title, the evidence (file paths and what you saw), why it hurts change
or testing, a proposed direction (a sketch, not a patch), a rough size (small, medium or large)
and a priority (high, medium or low). Rank findings by value against cost. Report only what you
verified in the code; mark anything else as a guess.

### Limits

Prefer a few well-evidenced findings over a long list. Do not propose rewrites for their own sake,
and respect the conventions already in the repository.
