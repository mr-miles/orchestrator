---
description: "How to read and write documents in the versioned document store (paths, versions, schemas, dry runs). Load before your first docs_write."
---

## Skill: Versioned document store

The document store is immutable and versioned. Every write that changes a document creates a new
version; nothing is ever overwritten or deleted. Writing content identical to the latest version
creates nothing: you get that version back with `unchanged: true`, and it counts as yours when you
attest it in your report (so a rework need not change a document that is still right).

- Paths are relative to your change (`plan/spec.md` means `changes/{{change_id}}/plan/spec.md`).
  To read another change's documents use the full `changes/<id>/...` path.
- `docs_read(path)` returns the latest version with its `version` and `attestation`.
- To create a new document: `docs_write(path, content)` (no `base_version`).
- To revise a document: `docs_write(path, content, base_version=<the version you read>)`.
  If someone wrote in between, the write is refused – re-read, merge, retry.
- Writes outside your write scope are refused. `control/**` belongs to the controller.
- `docs_write` returns the fully qualified attestation `file:changes/<id>/<path>@<version>`;
  cite it as returned in your report.
- Every path is bound to a schema and every write is validated against it. Call
  `docs_schema(path)` before writing a structured document; a refused write lists the errors.
  `docs_write(..., dry_run=true)` makes every check and writes nothing: it returns the
  attestation the write would produce, or why it would be refused.
  Your report's `data` must match your role's `report_schema` (`schema_get(name)`).
- Prefer markdown for prose and JSON for anything the controller parses.
