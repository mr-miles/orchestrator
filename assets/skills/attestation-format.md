---
description: "How to cite evidence as file: and git: attestations in documents and reports. Load before writing a report or a finding."
---

## Skill: Attestations

Every claim you make must be backed by an attestation the controller can resolve:

- `file:<path>@<version>` – a document version, e.g. `file:tickets/design/spec.md@3`
- `git:<repo>/<path>@<sha>` – a file at a commit, e.g. `git:app/src/api/users.py@4f2c9e1…`
- `git:<repo>@<sha>` – a whole commit

Use full 40-character shas. Use `verify_attestation(ref)` when unsure. An attestation that does
not resolve fails guard G-R3 and your report is rejected. Cite the *exact* versions you read, not
"latest".
