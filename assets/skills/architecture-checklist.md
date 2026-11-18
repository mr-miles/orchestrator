---
description: "Checklist for reviewing a diff's architecture against the approved delta specs. Load when you review code for architecture."
---

## Skill: Architecture checklist

For `git diff {{base_sha}}..{{candidate_sha}}`, against the approved delta spec(s):

1. **Contracts:** every public function, endpoint, schema or CLI surface matches the spec exactly
   (names, types, status codes, error shapes). Breaking changes are BLOCKERs unless the spec says so.
2. **Boundaries:** new imports respect existing layering; no new dependency cycles; no reaching into
   another module's internals.
3. **Flow:** happy, null, empty and error paths each handled where the spec defines them.
4. **Coupling and reuse:** existing helpers reused rather than duplicated; no premature abstraction.
5. **Operability:** configuration, logging and failure behaviour consistent with the rest of the system.

List what you compared in `data.checked` (files, spec versions).
