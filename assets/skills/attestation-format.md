---
description: "How to cite evidence as file: and git: attestations in documents and reports. Load before writing a report or a finding."
---

## Skill: Attestations

Every claim you make must be backed by an attestation the controller can resolve:

- `file:<path>@<version>` – a document version, e.g. `file:tickets/design/spec.md@3`
- `git:<repo>/<path>@<sha>` – a file at a commit, e.g.
  `git:app/src/api/users.py@4f2c9e1b07d3a5c86e2f19b04d7a3e5c1f08b92d`
- `git:<repo>@<sha>` – a whole commit

Always write the **full 40-character sha** (what `git rev-parse <commit>` prints), never the short
form logs and messages show (`4f2c9e1b07d3`): the controller expands an abbreviation only when
exactly one commit starts with it, and rejects an ambiguous or unknown one – say the full sha
instead. Use `verify_attestation(ref)` when unsure. An attestation that does
not resolve fails guard G-R3 and your report is rejected. Cite the *exact* versions you read, not
"latest".
